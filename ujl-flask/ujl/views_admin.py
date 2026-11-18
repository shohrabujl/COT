"""Admin: users, role permissions, audit log, settings."""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime

from flask import Blueprint, Response, flash, g, redirect, render_template, request, url_for

from . import auth
from . import db as D
from . import engine as E
from .auth import needs, super_only

bp = Blueprint('admin', __name__, url_prefix='/admin')
ROLE_RX = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 _&/.-]{1,39}$')


def role_names(con):
    return [r['name'] for r in con.execute('SELECT name FROM roles ORDER BY sort, name')]


@bp.get('/')
@needs('users.manage', 'audit.view', 'settings.edit')
def index():
    for perm, ep in (('users.manage', 'admin.users'), ('audit.view', 'admin.audit'), ('settings.edit', 'admin.settings')):
        if auth.can(perm):
            return redirect(url_for(ep))


# ---------- users ----------
@bp.get('/users')
@needs('users.manage')
def users():
    con = D.get_db()
    rows = con.execute('SELECT * FROM users ORDER BY created_at DESC').fetchall()
    roles = role_names(con)
    return render_template('admin_users.html', pending=[u for u in rows if u['status'] == 'pending'], others=[u for u in rows if u['status'] != 'pending'],
                           roles=roles, roles_all=roles + [E.SUPER] if auth.is_super() else roles)


def _deny(msg):
    flash(msg, 'bad')
    return redirect(url_for('admin.users'))


@bp.post('/users/add')
@needs('users.manage')
def user_add():
    f = request.form
    name, username, pw, role = f.get('name', '').strip(), f.get('username', '').strip(), f.get('password', ''), f.get('role', '')
    with D.tx() as c:
        if role not in role_names(c) + ([E.SUPER] if auth.is_super() else []):
            return _deny('Choose a role from the list.')
        err = auth.user_problem(c, username, name, pw)
        if err:
            return _deny(err)
        auth.make_user(c, username, name, pw, role, 'active', must_change=1)
        D.audit(c, 'user.add', username, f'role {role}', g.user)
    flash(f'{name} was added. They must choose a new password at first sign-in.', 'ok')
    return redirect(url_for('admin.users'))


@bp.post('/users/<int:uid>')
@needs('users.manage')
def user_action(uid):
    f = request.form
    do, role, status = f.get('do', ''), f.get('role', ''), f.get('status', '')
    with D.tx() as c:
        t = c.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
        if t is None:
            return _deny('That user no longer exists.')
        if t['role'] == E.SUPER and not auth.is_super():
            return _deny('Only a Super User can change a Super User.')
        if role == E.SUPER and not auth.is_super():
            return _deny('Only a Super User can give the Super User role.')
        valid = role_names(c) + [E.SUPER]
        last_super = t['role'] == E.SUPER and t['status'] == 'active' and auth.active_supers(c) <= 1
        now = D.utcnow()
        if do == 'approve':
            if t['status'] not in ('pending', 'rejected') or role not in valid:
                return _deny('Choose a role to approve this request.')
            c.execute("UPDATE users SET role=?, status='active', updated_at=? WHERE id=?", (role, now, uid))
            D.audit(c, 'user.approve', t['username'], f'role {role}', g.user)
            flash(f"{t['name']} can now sign in as {role}.", 'ok')
        elif do == 'reject':
            c.execute("UPDATE users SET status='rejected', role=NULL, updated_at=? WHERE id=?", (now, uid))
            D.audit(c, 'user.reject', t['username'], '', g.user)
            flash(f"{t['name']}’s request was rejected.", 'info')
        elif do == 'save':
            if role not in valid or status not in ('active', 'disabled'):
                return _deny('Choose a role and a status from the lists.')
            if last_super and (role != E.SUPER or status != 'active'):
                return _deny('There must always be one active Super User.')
            c.execute('UPDATE users SET role=?, status=?, updated_at=? WHERE id=?', (role, status, now, uid))
            D.audit(c, 'user.update', t['username'], f"role {t['role']} → {role}, status {t['status']} → {status}", g.user)
            flash('Saved.', 'ok')
        elif do == 'reset':
            pw = f.get('password', '')
            err = auth.pw_problem(t['username'], pw)
            if err:
                return _deny(err)
            c.execute('UPDATE users SET pw_hash=?, must_change=1, updated_at=? WHERE id=?', (auth.hash_pw(pw), now, uid))
            D.audit(c, 'user.reset_password', t['username'], 'Temporary password set', g.user)
            flash(f"Temporary password set for {t['name']}. They must choose a new one at next sign-in.", 'ok')
        elif do == 'delete':
            if t['status'] == 'active' or last_super:
                return _deny('Disable the user first. Active users are kept so the audit log still names them.')
            c.execute('DELETE FROM users WHERE id=?', (uid,))
            D.audit(c, 'user.delete', t['username'], f"status was {t['status']}", g.user)
            flash('Removed.', 'info')
        else:
            return _deny('Unknown action.')
    return redirect(url_for('admin.users'))


# ---------- role permissions (Super User only, so nobody can grant themselves more) ----------
@bp.route('/permissions', methods=['GET', 'POST'])
@needs('users.manage')
@super_only
def permissions():
    con = D.get_db()
    if request.method == 'POST':
        do, f = request.form.get('do', ''), request.form
        with D.tx() as c:
            names = role_names(c)
            if do == 'save':
                ticked = set(f.getlist('perm'))
                for n in names:
                    old = set(json.loads(c.execute('SELECT perms FROM roles WHERE name=?', (n,)).fetchone()['perms']))
                    new = {p for p in E.ALL_PERMS if f'{n}|{p}' in ticked}
                    if new != old:
                        c.execute('UPDATE roles SET perms=? WHERE name=?', (json.dumps([p for p in E.ALL_PERMS if p in new]), n))
                        D.audit(c, 'perm.edit', n, ' '.join([f'+{p}' for p in E.ALL_PERMS if p in new - old] + [f'-{p}' for p in E.ALL_PERMS if p in old - new]), g.user)
                flash('Permissions saved.', 'ok')
            elif do == 'add':
                n = f.get('name', '').strip()
                if not ROLE_RX.match(n) or n.lower() == E.SUPER.lower() or n.lower() in (x.lower() for x in names):
                    flash('Pick a new role name: 2 to 40 letters, digits or spaces.', 'bad')
                else:
                    c.execute('INSERT INTO roles(name, perms, sort) VALUES(?,?,(SELECT COALESCE(MAX(sort), 0) + 1 FROM roles))', (n, json.dumps(['dashboard.view'])))
                    D.audit(c, 'role.add', n, 'starts with dashboard.view', g.user)
                    flash(f'Role “{n}” added. Tick what it may do, then save.', 'ok')
            elif do == 'delete':
                n = f.get('name', '')
                if n not in names:
                    flash('That role does not exist.', 'bad')
                elif c.execute('SELECT 1 FROM users WHERE role=?', (n,)).fetchone():
                    flash('Some users still have that role. Move them to another role first.', 'bad')
                else:
                    c.execute('DELETE FROM roles WHERE name=?', (n,))
                    D.audit(c, 'role.delete', n, '', g.user)
                    flash(f'Role “{n}” deleted.', 'info')
            elif do == 'reset':
                for i, (n, perms) in enumerate(E.DEFAULT_ROLES.items()):
                    c.execute('INSERT INTO roles(name, perms, sort) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET perms=excluded.perms', (n, json.dumps(perms), i))
                D.audit(c, 'perm.reset', 'default roles', ', '.join(E.DEFAULT_ROLES), g.user)
                flash('Planner, Line Supervisor and Viewer are back to their defaults. Other roles were not touched.', 'info')
        return redirect(url_for('admin.permissions'))
    roles = [(r['name'], set(json.loads(r['perms']))) for r in con.execute('SELECT * FROM roles ORDER BY sort, name')]
    counts = {r['role']: r['n'] for r in con.execute('SELECT role, COUNT(*) n FROM users GROUP BY role')}
    return render_template('admin_perms.html', roles=roles, counts=counts)


# ---------- audit log ----------
def _audit_rows(q, limit):
    con = D.get_db()
    if not q:
        return con.execute('SELECT * FROM audit ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    like = '%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
    return con.execute("SELECT * FROM audit WHERE action LIKE ? ESCAPE '\\' OR username LIKE ? ESCAPE '\\' OR entity LIKE ? ESCAPE '\\' "
                       "OR detail LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT ?", (like, like, like, like, limit)).fetchall()


@bp.get('/audit')
@needs('audit.view')
def audit_page():
    q = request.args.get('q', '').strip()
    check = D.verify_audit(D.get_db()) if request.args.get('verify') == '1' else None
    return render_template('admin_audit.html', rows=_audit_rows(q, 500), q=q, check=check)


@bp.get('/audit.csv')
@needs('export.csv')
@needs('audit.view')
def audit_csv():
    rows = _audit_rows(request.args.get('q', '').strip(), 100000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['Time (UTC)', 'User', 'Action', 'Entity', 'Detail'])
    for r in rows:
        w.writerow([("'" + v if v[:1] in ('=', '+', '-', '@') else v) for v in (r['ts'], r['username'] or '', r['action'], r['entity'], r['detail'])])
    with D.tx() as c:
        D.audit(c, 'export.csv', 'audit', f'{len(rows)} rows', g.user)
    return Response('\ufeff' + buf.getvalue(), mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f"attachment; filename=audit-{datetime.now(D.DHAKA).strftime('%Y%m%d')}.csv"})


# ---------- settings ----------
@bp.route('/settings', methods=['GET', 'POST'])
@needs('settings.edit')
def settings():
    con = D.get_db()
    st = D.get_settings(con)
    if request.method == 'POST':
        f = request.form
        try:
            new = {'high_var_threshold': int(f['high_var_threshold']), 'hours': float(f['hours']), 'aging_days': float(f['aging_days']),
                   'urgent_window_days': int(f['urgent_window_days']), 'ready_window_days': int(f['ready_window_days']),
                   'as_of_mode': f['as_of_mode'], 'as_of_fixed': None}
            if new['as_of_mode'] == 'fixed':
                new['as_of_fixed'] = (date.fromisoformat(f['as_of_fixed']) - E.EPOCH).days
        except (KeyError, ValueError):
            new = None
        ok = (new and 1 <= new['high_var_threshold'] <= 7 and 1 <= new['hours'] <= 24 and 0 <= new['aging_days'] <= 365
              and 0 <= new['urgent_window_days'] <= 365 and 0 <= new['ready_window_days'] <= 30 and new['as_of_mode'] in ('today', 'fixed'))
        if not ok:
            flash('Check the values: threshold 1–7, hours 1–24, aging and urgent days 0–365, ready window 0–30, and a date if the as-of date is fixed.', 'bad')
            return render_template('admin_settings.html', st=st), 400
        with D.tx() as c:
            D.save_settings(c, new)
            D.audit(c, 'settings.edit', 'settings', ', '.join(f'{k} {st[k]} → {new[k]}' for k in new if st.get(k) != new[k]) or 'no change', g.user)
        flash('Settings saved.', 'ok')
        return redirect(url_for('admin.settings'))
    return render_template('admin_settings.html', st=st)
