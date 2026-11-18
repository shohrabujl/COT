"""The working pages: dashboard, Kanban, plan, health and import, line-wise, PF master, account, CSV exports."""
from __future__ import annotations

import csv
import io
import json
import os
import re
from collections import Counter
from datetime import datetime

from flask import Blueprint, Response, flash, g, jsonify, redirect, render_template, request, url_for

from . import auth
from . import data as DT
from . import db as D
from . import engine as E
from . import filters, imports
from .auth import needs

bp = Blueprint('main', __name__)

WHY = {1: 'Columns are found by header name, so column order may change.',
       2: 'PF, BUYER, the 7 process columns and REMARKS must be present.',
       4: 'Subtotal and grand-total lines are dropped automatically.',
       5: 'OK means the cleaned total ties to the last total row of the plan file.',
       6: 'Entries such as “RF 21916” become 21916 so they match the master.',
       7: 'Placeholder text instead of a PF. Ask Planning for the PF; these rows cannot be linked.',
       8: 'Add these PFs and their process codes to the PF master (biggest first, below).',
       9: 'The PF exists but one or more of the 7 process cells are empty.',
       10: 'A PF must appear once; the first one found is used.',
       11: 'Inspection date earlier than the sewing finish. Confirm with Planning.',
       12: 'A process code is not in the variant list; see the variant mix below.'}
LW_FIELDS = {'line', 'pf', 'manpower', 'smv', 'eff', 'actual_input', 'area', 'total_input', 'total_output', 'ready', 'remarks'}


def state():
    if '_state' not in g:
        g._state = DT.load_state(D.get_db())
    return g._state


def jerr(msg, code=400):
    return jsonify(ok=False, error=msg), code


def line_names(s):
    return list(dict.fromkeys(r.line for r in s['runs'])) if s else []


# ---------- CSV ----------
def _safe(v):
    """Stops a cell from being read as a formula when the file is opened in Excel."""
    return "'" + v if isinstance(v, str) and v and v[0] in '=+-@\t\r' else v


def csv_response(name, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow([_safe(v) for v in r])
    with D.tx() as c:
        D.audit(c, 'export.csv', name, f'{len(rows)} rows', g.user)
    stamp = datetime.now(D.DHAKA).strftime('%Y%m%d')
    return Response('\ufeff' + buf.getvalue(), mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename={name}-{stamp}.csv'})


# ---------- home, account ----------
@bp.get('/')
def home():
    ep = filters.first_allowed()
    if ep:
        return redirect(url_for(ep))
    return render_template('error.html', code=403, msg='Your account has no permissions yet. Ask a Super User to give your role some.'), 403


@bp.route('/account', methods=['GET', 'POST'])
def account():
    u = g.user
    if request.method == 'POST':
        cur, new, new2 = request.form.get('current', ''), request.form.get('new', ''), request.form.get('new2', '')
        if not auth.check_pw(u['pw_hash'], cur):
            flash('The current password is wrong.', 'bad')
        elif new != new2:
            flash('The two new passwords do not match.', 'bad')
        elif auth.pw_problem(u['username'], new):
            flash(auth.pw_problem(u['username'], new), 'bad')
        else:
            with D.tx() as c:
                c.execute('UPDATE users SET pw_hash=?, must_change=0, updated_at=? WHERE id=?', (auth.hash_pw(new), D.utcnow(), u['id']))
                D.audit(c, 'auth.password', u['username'], 'Own password changed', u)
            auth.sign_in(D.get_db().execute('SELECT * FROM users WHERE id=?', (u['id'],)).fetchone())
            flash('Password changed.', 'ok')
            return redirect(url_for('main.home'))
    return render_template('account.html')


# ---------- dashboard ----------
@bp.get('/dashboard')
@needs('dashboard.view')
def dashboard():
    return render_template('dashboard.html', s=state())


# ---------- Kanban ----------
@bp.get('/kanban')
@needs('kanban.view')
def kanban():
    s = state()
    f = {k: request.args.get(k, '').strip() for k in ('line', 'pri', 'q')}
    cols, cards = [], {}
    if s:
        q = f['q'].lower()
        keep = [c for c in s['cards'] if (not f['line'] or c.run.line == f['line']) and (not f['pri'] or c.priority == f['pri'])
                and (not q or q in f'{c.run.to_pf} {c.run.style} {c.run.buyer_group}'.lower())]
        cols = [(st, sorted((c for c in keep if c.stage == st), key=lambda c: (c.run.sew_start or 0, c.run.run_no))) for st in E.STAGES]
        cards = {c.key: DT.card_dict(c) for c in keep}
    return render_template('kanban.html', s=s, f=f, cols=cols, cards=cards, lines=line_names(s),
                           can_move=auth.can('kanban.move'), can_edit=auth.can('kanban.edit'))


def _card(s, key):
    return next((c for c in s['cards'] if c.key == key), None) if s else None


@bp.post('/api/card/move')
@needs('kanban.move')
def api_card_move():
    j = request.get_json(silent=True) or {}
    key, stage = str(j.get('key', '')), str(j.get('stage', ''))
    s = state()
    c = _card(s, key)
    if stage not in E.STAGES:
        return jerr('Unknown stage.')
    if c is None:
        return jerr('That card is no longer in the plan. Reload the page.', 404)
    if c.stage != stage:                                    # dropping a card back on its own column changes nothing
        o = E.move_card(s['overlay'], c, stage)[key]
        with D.tx() as con:
            DT.put_card(con, key, o, g.user)
            D.audit(con, 'kanban.move', key, f'{c.stage} → {stage}', g.user)
    return jsonify(ok=True, card=dict(DT.card_dict(c), stage=stage, locked=True))


@bp.post('/api/card/save')
@needs('kanban.move', 'kanban.edit')
def api_card_save():
    j = request.get_json(silent=True) or {}
    key, stage = str(j.get('key', '')), str(j.get('stage', ''))
    pri, owner, notes = str(j.get('priority', '')), str(j.get('owner', '')).strip(), str(j.get('notes', '')).strip()
    s = state()
    c = _card(s, key)
    if c is None:
        return jerr('That card is no longer in the plan. Reload the page.', 404)
    if stage not in E.STAGES or pri not in DT.PRIORITIES:
        return jerr('Choose a stage and a priority from the lists.')
    if len(owner) > 80 or len(notes) > 1000:
        return jerr('Owner: 80 characters at most. Notes: 1000 characters at most.')
    moved, edited = stage != c.stage, (pri, owner, notes) != (c.priority, c.owner, c.notes)
    if moved and not auth.can('kanban.move'):
        return jerr('Your role may not move cards.', 403)
    if edited and not auth.can('kanban.edit'):
        return jerr('Your role may not edit card details.', 403)
    if not moved and not edited:
        return jsonify(ok=True)
    patch = {'priority': pri, 'owner': owner, 'notes': notes}
    o = E.move_card(s['overlay'], c, stage, patch)[key] if moved else dict(patch, stage=c.stage, locked=c.locked)
    with D.tx() as con:
        DT.put_card(con, key, o, g.user)
        if moved:
            D.audit(con, 'kanban.move', key, f'{c.stage} → {stage}', g.user)
        if edited:
            D.audit(con, 'kanban.edit', key, 'details', g.user)
    return jsonify(ok=True)


@bp.post('/api/card/unlock')
@needs('kanban.edit')
def api_card_unlock():
    key = str((request.get_json(silent=True) or {}).get('key', ''))
    s = state()
    c = _card(s, key)
    if c is None:
        return jerr('That card is no longer in the plan. Reload the page.', 404)
    if c.locked:
        with D.tx() as con:
            DT.put_card(con, key, E.unlock_card(s['overlay'], c)[key], g.user)
            D.audit(con, 'kanban.unlock', key, '', g.user)
    return jsonify(ok=True)


# ---------- plan ----------
def _plan_list(s):
    a = request.args
    mode = 'rows' if a.get('mode') == 'rows' else 'runs'
    line, typ, chk, q = a.get('line', '').strip(), a.get('type', '').strip(), a.get('chk', '').strip(), a.get('q', '').strip().lower()
    if mode == 'runs':
        rows = [r for r in s['runs'] if (not line or r.line == line) and (not typ or r.type == typ)
                and (not q or q in f'{r.to_pf} {r.style} {r.buyer_group}'.lower())]
    else:
        rows = [r for r in s['plan']['rows'] if (not line or r.line == line) and (not chk or (r.date_check if chk == 'Date conflicts' else r.status != 'Matched'))
                and (not q or q in f'{r.pf} {r.style} {r.buyer_group}'.lower())]
    return mode, rows, {'line': line, 'type': typ, 'chk': chk, 'q': a.get('q', '').strip()}


@bp.get('/plan')
@needs('plan.view')
def plan():
    s = state()
    mode, rows, f = _plan_list(s) if s else ('runs', [], {'line': '', 'type': '', 'chk': '', 'q': ''})
    return render_template('plan.html', s=s, mode=mode, rows=rows[:600], total=len(rows), f=f, lines=line_names(s))


@bp.get('/plan.csv')
@needs('export.csv')
@needs('plan.view')
def plan_csv():
    s = state()
    if not s:
        return redirect(url_for('main.plan'))
    mode, rows, _f = _plan_list(s)
    if mode == 'runs':
        head = ['Run', 'Line', 'Seq', 'From PF', 'To PF', 'Buyer', 'Style', 'Order qty', 'Plan qty', 'PO lines', 'Sew start', 'Sew end', 'Master status',
                'Changes', 'What changes', 'Type', 'Rank']
        out = [[r.run_no, r.line, r.seq, r.from_pf, r.to_pf, r.buyer_group, r.style, r.order_qty, r.plan_qty, r.po_lines, E.dstr(r.sew_start),
                E.dstr(r.sew_end), r.status, r.changes, r.what, r.type, r.rank] for r in rows]
        return csv_response('line-sequence', head, out)
    head = ['#', 'Raw row', 'Line', 'Buyer', 'PF', 'As received', 'Style', 'Order qty', 'Plan qty', 'Sew in', 'Sew end', 'BPO', 'Date check', 'Master status', 'Run']
    out = [[r.seq, r.raw_row, r.line, r.buyer_group, r.pf, r.pf_as_received, r.style, r.order_qty, r.plan_qty, E.dstr(r.sew_in), E.dstr(r.sew_end),
            E.dstr(r.bpo), r.date_check, r.status, r.run_no] for r in rows]
    return csv_response('clean-plan', head, out)


# ---------- health and import ----------
@bp.get('/health')
@needs('plan.view')
def health():
    s, con = state(), D.get_db()
    imp, token = None, request.args.get('imp', '')
    if token and auth.can('import.run'):
        imp = imports.unstash(con, g.user['id'], token)
        if imp is None:
            flash('That import has expired. Choose the file again.', 'warn')
    fresh = {'cards': not con.execute('SELECT 1 FROM cards LIMIT 1').fetchone(), 'linewise': not con.execute('SELECT 1 FROM linewise LIMIT 1').fetchone()}
    return render_template('health.html', s=s, WHY=WHY, imp=imp['res'] if imp else None, imp_file=imp['file'] if imp else '', token=token,
                           fresh=fresh, meta=D.get_meta(con))


@bp.post('/health/import')
@needs('import.run')
def import_upload():
    f = request.files.get('file')
    name = os.path.basename((f.filename or '').replace('\\', '/')) if f else ''
    if not name:
        flash('Choose an Excel file first.', 'bad')
    elif not name.lower().endswith(('.xlsx', '.xlsm')):
        flash('Only .xlsx files can be read. In Excel, use Save As and pick “Excel Workbook (.xlsx)”.', 'bad')
    else:
        try:
            res = imports.scan(imports.read_workbook(io.BytesIO(f.read())), imports.current_packed(D.get_db()))
        except imports.WorkbookError as e:
            flash(str(e), 'bad')
            return redirect(url_for('main.health'))
        except Exception as e:                                  # an unreadable file is the user's to fix, not a crash
            flash(f'Could not read the file ({type(e).__name__}). Check that it is a normal .xlsx workbook.', 'bad')
            return redirect(url_for('main.health'))
        if not any(res[k] for k in res):
            flash('No plan or PF master sheet was recognised in this file.', 'bad')
        else:
            with D.tx() as c:
                token = imports.stash(c, g.user['id'], res, name)
            return redirect(url_for('main.health', imp=token))
    return redirect(url_for('main.health'))


@bp.post('/health/import/commit')
@needs('import.run')
def import_commit():
    token = request.form.get('token', '')
    stash = imports.unstash(D.get_db(), g.user['id'], token)
    if not stash:
        flash('That import has expired. Choose the file again.', 'warn')
        return redirect(url_for('main.health'))
    use = {k: request.form.get(f'use_{k}') == '1' for k in ('plan', 'master', 'cards', 'linewise')}
    if not any(use[k] and stash['res'].get(k) for k in use):
        flash('Tick at least one part to import.', 'warn')
        return redirect(url_for('main.health', imp=token))
    with D.tx() as c:
        done = imports.commit(c, g.user, stash['res'], use, stash['file'])
        imports.discard(c, token)
    flash('Import committed. ' + '; '.join(done) + '.', 'ok')
    return redirect(url_for('main.health'))


@bp.post('/health/import/cancel')
@needs('import.run')
def import_cancel():
    with D.tx() as c:
        imports.discard(c, request.form.get('token', ''))
    return redirect(url_for('main.health'))


# ---------- line-wise ----------
@bp.get('/linewise')
@needs('linewise.view')
def linewise():
    s = state()
    rows, derived, totals, as_of, st = DT.lw_table(D.get_db(), s)
    items = [{'r': r, 'cells': DT.lw_cells(d), 'v': {k: _lw_value(k, r[k]) for k in LW_FIELDS}, 'show': {k: _lw_show(k, r[k]) for k in LW_FIELDS}}
             for r, d in zip(rows, derived)]
    return render_template('linewise.html', s=s, items=items, totals=DT.lw_total_cells(totals), lines=line_names(s), as_of=as_of, st=st,
                           can_edit=auth.can('linewise.edit'))


def _lw_value(field, v):
    if v is None:
        return ''
    if field == 'eff':
        return format(round(v * 100, 4), 'g')
    if field == 'actual_input':
        return E.iso(v)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else str(v)
    return str(v)


def _lw_show(field, v):
    """What a read-only viewer sees in an input cell."""
    if field == 'eff':
        return f'{_lw_value(field, v)}%' if v is not None else ''
    return E.dstr(v) if field == 'actual_input' else _lw_value(field, v)


@bp.post('/api/linewise/<int:rid>')
@needs('linewise.edit')
def api_lw_edit(rid):
    j = request.get_json(silent=True) or {}
    field = str(j.get('field', ''))
    con, s = D.get_db(), state()
    row = con.execute('SELECT * FROM linewise WHERE id=?', (rid,)).fetchone()
    if field not in LW_FIELDS:
        return jerr('That column cannot be edited.')
    if row is None:
        return jerr('That row no longer exists. Reload the page.', 404)
    lines = line_names(s)
    if row['line'] and row['line'] not in lines:
        lines.append(row['line'])
    try:
        val = DT.parse_lw(field, j.get('value'), lines if field == 'line' else None)
    except ValueError as e:
        return jerr(str(e))
    with D.tx() as c:
        c.execute(f'UPDATE linewise SET {field}=? WHERE id=?', (val, rid))          # field is checked against LW_FIELDS above
        D.audit(c, 'linewise.edit', f"{row['line']} row {row['n']} · {field}", '' if val is None else str(val)[:100], g.user)
    rows, derived, totals, _a, _st = DT.lw_table(D.get_db(), s)
    i = next(k for k, r in enumerate(rows) if r['id'] == rid)
    return jsonify(ok=True, value=_lw_value(field, val), cells=DT.lw_cells(derived[i]), totals=DT.lw_total_cells(totals))


@bp.post('/linewise/add')
@needs('linewise.edit')
def lw_add():
    lines = line_names(state())
    if not lines:
        flash('Import a plan first, so there are lines to choose from.', 'warn')
        return redirect(url_for('main.linewise'))
    with D.tx() as c:
        n = c.execute('SELECT COALESCE(MAX(n), 0) + 1 FROM linewise').fetchone()[0]
        c.execute('INSERT INTO linewise(n, line, eff, area) VALUES(?,?,?,?)', (n, lines[0], 0.85, 'OUTPUT'))
        D.audit(c, 'linewise.add', f'row {n}', lines[0], g.user)
    return redirect(url_for('main.linewise'))


@bp.post('/linewise/<int:rid>/delete')
@needs('linewise.edit')
def lw_delete(rid):
    with D.tx() as c:
        row = c.execute('SELECT * FROM linewise WHERE id=?', (rid,)).fetchone()
        if row:
            c.execute('DELETE FROM linewise WHERE id=?', (rid,))
            D.audit(c, 'linewise.delete', f"{row['line']} row {row['n']}", f"PF {row['pf'] or '-'}", g.user)
    return redirect(url_for('main.linewise'))


@bp.get('/linewise.csv')
@needs('export.csv')
@needs('linewise.view')
def linewise_csv():
    rows, derived, _t, _a, _s = DT.lw_table(D.get_db(), state())
    head = ['Line', 'PF', 'Manpower', 'SMV', 'Eff %', 'Actual input', 'Area', 'Total input', 'Total output', 'Ready', 'Status', 'Plan qty', 'Days since plan',
            'Days running', 'Max target', 'Today target', 'WIP', 'Completion %', 'Balance', 'Remarks']
    out = [[r['line'], r['pf'], r['manpower'], r['smv'], None if r['eff'] is None else round(r['eff'] * 100, 2), E.dstr(r['actual_input']), r['area'],
            r['total_input'], r['total_output'], r['ready'], d['status'], d['plan_qty'], d['days_since_plan'], d['days_running'],
            None if d['max_target'] is None else round(d['max_target']), d['today_target'], d['wip'],
            None if d['completion'] is None else round(d['completion'] * 100, 1), d['balance'], r['remarks']] for r, d in zip(rows, derived)]
    return csv_response('line-wise', head, out)


# ---------- PF master ----------
@bp.get('/master')
@needs('plan.view')
def master():
    s = state()
    q = request.args.get('q', '').strip()
    rows, dup = [], set()
    if s:
        dup = {pf for pf, n in Counter(r[1] for r in s['mrows']).items() if n > 1}
        rows = [(i, r) for i, r in enumerate(s['mrows']) if not q or q.lower() in f'{r[0]} {r[1]}'.lower()]
    return render_template('master.html', s=s, rows=rows[:600], total=len(rows), q=q, dup=dup, can_edit=auth.can('master.edit'))


@bp.post('/api/master/cell')
@needs('master.edit')
def api_master_cell():
    j = request.get_json(silent=True) or {}
    try:
        i, k = int(j.get('i')), int(j.get('k'))
    except (TypeError, ValueError):
        return jerr('Bad request.')
    v = str(j.get('v', '')).strip()[:30]
    with D.tx() as c:                                   # read and write in one transaction, so two edits never overwrite each other
        b = DT.current_batch(c, 'master')
        if not b:
            return jerr('There is no PF master yet.', 404)
        rows = json.loads(b['data'])
        if not (0 <= i < len(rows)) or not (2 <= k < 2 + len(E.OPS)) or str(rows[i][1]) != str(j.get('pf')):
            return jerr('The PF master has changed. Reload the page.', 409)
        op, old = E.OPS[k - 2], rows[i][k]
        if v and v not in E.DEFAULT_VARIANTS[op] and v != old:
            return jerr('Choose a value from the list.')
        if v != old:
            rows[i][k] = v
            c.execute('UPDATE batches SET data=?, row_count=?, ts=? WHERE id=?', (json.dumps(rows), len(rows), D.utcnow(), b['id']))
            D.audit(c, 'master.edit', f'PF {rows[i][1]}', f"{op}: {old or '(blank)'} → {v or '(blank)'}", g.user)
    return jsonify(ok=True, blank=v == '')


@bp.post('/master/add')
@needs('master.edit')
def master_add():
    pf = request.form.get('pf', '').strip()
    if not re.fullmatch(r'\d{1,9}', pf):
        flash('Enter a PF number (digits only).', 'bad')
        return redirect(url_for('main.master'))
    with D.tx() as c:
        b = DT.current_batch(c, 'master')
        rows = json.loads(b['data']) if b else None
        if rows is None:
            flash('Import a PF master first.', 'warn')
        elif any(str(r[1]) == pf for r in rows):
            flash('That PF already exists.', 'bad')
        else:
            rows.insert(0, ['', pf] + [''] * (len(E.OPS) + 1))
            c.execute('UPDATE batches SET data=?, row_count=?, ts=? WHERE id=?', (json.dumps(rows), len(rows), D.utcnow(), b['id']))
            D.audit(c, 'master.add', f'PF {pf}', 'added with blank process fields', g.user)
            flash(f'PF {pf} added. Fill in its seven process codes.', 'ok')
    return redirect(url_for('main.master'))


@bp.get('/master.csv')
@needs('export.csv')
@needs('plan.view')
def master_csv():
    s = state()
    return csv_response('pf-master', E.MASTER_HEADERS, s['mrows'] if s else [])
