"""Reading the planning workbook (.xlsx): finds the plan and PF-master sheets, plus the saved Kanban cards and Line-Wise rows."""
from __future__ import annotations

import json
import os
import re
import secrets
import time

from . import data as DT
from . import db as D
from . import engine as E

MAX_ROWS = 20000
MAX_UNPACKED = 200 * 1024 * 1024      # an .xlsx is a zip; refuse one that unpacks to more than this (protects memory)
KEEP_BATCHES = 5                      # imports kept per kind (the current one is always kept)
WORKBOOK_NAME = 'UPDATE_PLAN_MASTER_ANALYSIS.xlsx'


class WorkbookError(Exception):
    """A problem with the uploaded file that can be explained to the user in plain words."""


def read_workbook(src) -> dict:
    """Every sheet as a list of rows of plain values (formulas give their last calculated value)."""
    import zipfile

    import openpyxl
    try:
        with zipfile.ZipFile(src) as z:
            if sum(i.file_size for i in z.infolist()) > MAX_UNPACKED:
                raise WorkbookError('That workbook is far too large once unpacked. Save a smaller copy with only the plan and PF master sheets.')
    except zipfile.BadZipFile:
        raise WorkbookError('That file is not a normal .xlsx workbook. Open it in Excel and use Save As, then pick Excel Workbook (.xlsx).')
    if hasattr(src, 'seek'):
        src.seek(0)
    wb = openpyxl.load_workbook(src, data_only=True)
    try:
        return {ws.title: [list(r) for _, r in zip(range(MAX_ROWS), ws.iter_rows(values_only=True))] for ws in wb.worksheets}
    finally:
        wb.close()


def find_workbook(*folders):
    for f in folders:
        p = os.path.join(f, WORKBOOK_NAME)
        if os.path.isfile(p):
            return p
    return None


def _nv(v):
    """Number from a cell that may hold text such as '65%' or '(12)'."""
    if isinstance(v, str):
        s = v.strip()
        x = E.num(s.replace('(', '').replace(')', '').replace('%', ''))
        if x is None:
            return None
        return -x if re.fullmatch(r'\(.*\)', s) else x / 100 if s.endswith('%') else x
    return E.num(v)


def _cards(kd, runs, s) -> dict:
    rows = [r for r in kd[1:] if r and r[0]]
    cell = lambda r, i: r[i] if len(r) > i else None          # noqa: E731
    locked = {}
    for r in rows:
        if str(cell(r, 5)).upper() == 'TRUE' and cell(r, 1) in E.STAGES:
            locked[str(r[0])] = {'stage': r[1], 'priority': cell(r, 2) if cell(r, 2) in DT.PRIORITIES else 'Medium',
                                 'owner': E.cell_str(cell(r, 3)), 'notes': E.cell_str(cell(r, 4)), 'locked': True}
    by_key = {c.key: c for c in E.sync_cards(runs, locked, s)[0]}
    entries = {}
    for r in rows:
        key, c = str(r[0]), by_key.get(str(r[0]))
        if c is None:
            continue
        pr = cell(r, 2) if cell(r, 2) in DT.PRIORITIES else c.priority
        if key in locked or pr != c.priority:
            entries[key] = {'stage': locked[key]['stage'] if key in locked else c.stage, 'priority': pr,
                            'owner': E.cell_str(cell(r, 3)), 'notes': E.cell_str(cell(r, 4)), 'locked': key in locked}
    return {'entries': entries, 'locked': len(locked), 'overrides': sum(1 for k, v in entries.items() if not v['locked'])}


def _linewise(lw):
    hi = next((i for i, r in enumerate(lw) if r and E.cell_str(r[0]) == 'Line No'), None)
    if hi is None:
        return None
    out = []
    for r in lw[hi + 1:]:
        if not r or not E.cell_str(r[0]).startswith('UJL-'):
            break
        r = list(r) + [None] * (21 - len(r))
        n = E.num(r[1])
        out.append({'line': E.cell_str(r[0]), 'pf': str(int(n)) if n is not None and float(n).is_integer() else (E.cell_str(r[1]) or None),
                    'manpower': _nv(r[2]), 'smv': _nv(r[3]), 'eff': _nv(r[4]), 'actual_input': E.parse_date(r[6]), 'area': E.cell_str(r[11]),
                    'total_input': _nv(r[14]), 'total_output': _nv(r[15]), 'ready': _nv(r[19]), 'remarks': E.cell_str(r[20])})
    return {'rows': out} if out else None


def scan(sheets: dict, current=None) -> dict:
    """Looks through every sheet. `current` = {'plan': packed, 'master': packed} already stored, used when the file has only one of them."""
    res = {'plan': None, 'master': None, 'cards': None, 'linewise': None}
    for name in sorted(sheets, key=lambda n: bool(re.search('clean', n, re.I))):          # the workbook's own cleaned sheet comes last
        rows = sheets[name]
        head = [[E.cell_str(c) for c in r] for r in rows[:15]]
        pi = next((i for i, h in enumerate(head) if E.map_columns(h, E.PLAN_FIELDS)['recognised'] >= 14), -1)
        mi = next((i for i, h in enumerate(head) if 'PF' in h and 'INSEAM' in h), -1)
        if pi >= 0 and res['plan'] is None:
            res['plan'] = dict(sheet=name, **E.pack_sheet('plan', rows[pi], rows[pi + 1:]))
        if mi >= 0 and res['master'] is None:
            res['master'] = dict(sheet=name, **E.pack_sheet('master', rows[mi], rows[mi + 1:]))
    mp = res['master'] or (current or {}).get('master')
    pp = res['plan'] or (current or {}).get('plan')
    master = E.build_master(mp['headers'], mp['rows']) if mp else None
    if res['master']:
        res['master']['info'] = f"{master['incomplete']} PFs with blank process fields"
    if res['plan'] and master:
        plan = E.clean_plan(res['plan']['headers'], res['plan']['rows'], master)
        tot = sum(r.plan_qty for r in plan['rows'])
        gt = plan['grand_total']
        res['plan']['info'] = (f"{len(plan['rows'])} rows kept, {plan['dropped']} subtotal or blank rows dropped · cleaned total {tot:,.0f} "
                               + ('ties to the file total' if gt == tot else f"vs file total {'n/a' if gt is None else format(gt, ',.0f')}"))
    lw = sheets.get('Line-Wise Data')
    if lw:
        res['linewise'] = _linewise(lw)
    if sheets.get('Kanban_Data') and pp and master:
        s = E.Settings()
        as_of = E.parse_date(lw[2][2]) if lw and len(lw) > 2 and len(lw[2]) > 2 else None          # the workbook's own "as of" date
        if as_of:
            s.as_of = as_of
        runs = E.build_runs(E.clean_plan(pp['headers'], pp['rows'], master)['rows'], s)
        res['cards'] = _cards(sheets['Kanban_Data'], runs, s)
    return res


def current_packed(con) -> dict:
    out = {}
    for kind in ('plan', 'master'):
        b = DT.current_batch(con, kind)
        if b:
            out[kind] = {'headers': json.loads(b['headers']), 'rows': json.loads(b['data'])}
    return out


def commit(con, user, res: dict, use: dict, file_name: str) -> list:
    """Writes the chosen parts (call inside D.tx()). Returns short lines describing what was loaded."""
    now, done, meta = D.utcnow(), [], {}
    uid = user['id'] if user else None
    for kind in ('plan', 'master'):
        p = res.get(kind)
        if p and use.get(kind):
            cur = con.execute('INSERT INTO batches(kind,file_name,headers,data,row_count,by_user,ts) VALUES(?,?,?,?,?,?,?)',
                              (kind, f"{file_name} · {p['sheet']}", json.dumps(p['headers']), json.dumps(p['rows']), len(p['rows']), uid, now))
            meta.update({f'{kind}_id': cur.lastrowid, f'{kind}_file': file_name, f'{kind}_at': now})
            con.execute('DELETE FROM batches WHERE kind=? AND id NOT IN (SELECT id FROM batches WHERE kind=? ORDER BY id DESC LIMIT ?)',
                        (kind, kind, KEEP_BATCHES))
            D.audit(con, f'import.{kind}', file_name, f"{len(p['rows'])} rows from sheet {p['sheet']}", user)
            done.append(f"{'Plan' if kind == 'plan' else 'PF master'}: {len(p['rows'])} rows")
    if meta:
        D.set_meta(con, **meta)
    if use.get('cards') and res.get('cards'):
        con.execute('DELETE FROM cards')
        for key, o in res['cards']['entries'].items():
            DT.put_card(con, key, o, user)
        c = res['cards']
        D.audit(con, 'import.cards', file_name, f"{c['locked']} locked cards, {c['overrides']} priority overrides", user)
        done.append(f"Saved cards: {c['locked']} locked, {c['overrides']} priority overrides")
    if use.get('linewise') and res.get('linewise'):
        con.execute('DELETE FROM linewise')
        for n, r in enumerate(res['linewise']['rows'], 1):
            con.execute('INSERT INTO linewise(n,line,pf,manpower,smv,eff,actual_input,area,total_input,total_output,ready,remarks) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                        (n, r['line'], r['pf'], r['manpower'], r['smv'], r['eff'], r['actual_input'], r['area'], r['total_input'],
                         r['total_output'], r['ready'], r['remarks']))
        D.audit(con, 'import.linewise', file_name, f"{len(res['linewise']['rows'])} rows", user)
        done.append(f"Line-wise rows: {len(res['linewise']['rows'])}")
    return done


# ---------- a scanned file waits in the database until its owner presses "Commit import" ----------
def stash(con, user_id: int, res: dict, file_name: str) -> str:
    token = secrets.token_urlsafe(16)
    now = time.time()
    con.execute('DELETE FROM imports WHERE created<?', (now - 3600,))
    con.execute('INSERT INTO imports(token,user_id,payload,created) VALUES(?,?,?,?)', (token, user_id, json.dumps({'res': res, 'file': file_name}), now))
    return token


def unstash(con, user_id: int, token: str):
    row = con.execute('SELECT payload FROM imports WHERE token=? AND user_id=? AND created>?', (token or '', user_id, time.time() - 3600)).fetchone()
    return json.loads(row['payload']) if row else None


def discard(con, token: str) -> None:
    con.execute('DELETE FROM imports WHERE token=?', (token or '',))


def seed_from_path(con, user, path: str) -> list:
    """First-run load from the planning workbook: plan, PF master, saved cards and line-wise rows (call inside D.tx())."""
    res = scan(read_workbook(path))
    return commit(con, user, res, {'plan': True, 'master': True, 'cards': True, 'linewise': True}, os.path.basename(path))
