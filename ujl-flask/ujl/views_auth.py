"""First-run setup, sign-in, registration requests and sign-out."""
from __future__ import annotations

import sqlite3

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, session, url_for

from . import auth
from . import db as D
from . import engine as E
from . import imports

bp = Blueprint('auth', __name__)


def _safe_next(target):
    return target if target and target.startswith('/') and not target.startswith('//') and '\\' not in target else None


def _form():
    f = request.form
    return f.get('name', '').strip(), f.get('username', '').strip(), f.get('password', ''), f.get('password2', '')


@bp.route('/setup', methods=['GET', 'POST'])
def setup():
    """Only works while there are no users: the first person becomes the Super User."""
    workbook = imports.find_workbook(current_app.config['APP_ROOT'], current_app.config['DATA_DIR'])
    if request.method == 'POST':
        name, username, pw, pw2 = _form()
        err = auth.user_problem(D.get_db(), username, name, pw) or (None if pw == pw2 else 'The two passwords do not match.')
        if err:
            flash(err, 'bad')
        else:
            note = None
            with D.tx() as con:
                if auth.has_users(con):
                    return redirect(url_for('auth.login'))
                uid = auth.make_user(con, username, name, pw, E.SUPER, 'active')
                user = con.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
                D.audit(con, 'auth.setup', username, 'First Super User created', user)
            if workbook:
                try:
                    with D.tx() as con:
                        done = imports.seed_from_path(con, user, workbook)
                    note = 'Loaded from ' + imports.WORKBOOK_NAME + ': ' + '; '.join(done) + '.'
                except Exception as e:                      # a bad workbook must not block the first sign-in
                    why = str(e) if isinstance(e, imports.WorkbookError) else type(e).__name__
                    note = f'Found {imports.WORKBOOK_NAME} but could not read it ({why}). Import it from the Health page.'
            auth.sign_in(user)
            flash(note or 'Welcome. Open Health to import the plan workbook.', 'ok' if note and note.startswith('Loaded') else 'info')
            return redirect(url_for('main.home'))
    return render_template('setup.html', workbook=workbook and imports.WORKBOOK_NAME)


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if g.user:
        return redirect(url_for('main.home'))
    if request.method == 'POST':
        username, pw = request.form.get('username', '').strip()[:64], request.form.get('password', '')[:200]
        con = D.get_db()
        keys = [(f'u:{username.lower()}', auth.FAIL_LIMIT), (f'ip:{request.remote_addr}', auth.IP_LIMIT)]
        if auth.locked_out(con, keys):
            flash('Too many failed attempts. Wait 15 minutes, then try again.', 'bad')
        else:
            u = con.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
            if not auth.check_pw(u['pw_hash'] if u else None, pw):
                with D.tx() as c:
                    auth.note_failure(c, [k for k, _ in keys])
                    D.audit(c, 'auth.login_failed', username, 'Wrong username or password', None, username=username)
                flash('Wrong username or password.', 'bad')
            elif u['status'] == 'pending':
                flash('Your request is waiting for approval by a Super User.', 'warn')
            elif u['status'] != 'active':
                flash('This account is not active. Ask a Super User.', 'bad')
            else:
                with D.tx() as c:
                    auth.clear_failures(c, f'u:{username.lower()}')
                    c.execute('UPDATE users SET last_login=? WHERE id=?', (D.utcnow(), u['id']))
                    D.audit(c, 'auth.login', u['username'], '', u)
                nxt = _safe_next(request.args.get('next'))
                auth.sign_in(u)
                return redirect(nxt or url_for('main.home'))
    return render_template('login.html')


@bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name, username, pw, pw2 = _form()
        con = D.get_db()
        ipkey = f'reg:{request.remote_addr}'
        err = ('Too many requests from this computer. Try again later.' if auth.locked_out(con, [(ipkey, 10)])
               else auth.user_problem(con, username, name, pw) or (None if pw == pw2 else 'The two passwords do not match.'))
        if err:
            flash(err, 'bad')
        else:
            try:
                with D.tx() as c:
                    auth.make_user(c, username, name, pw, None, 'pending')
                    auth.note_failure(c, [ipkey])
                    D.audit(c, 'auth.register', username, f'Access requested by {name}', None, username=username)
            except sqlite3.IntegrityError:
                flash('That username is already taken.', 'bad')
                return render_template('register.html')
            flash('Request sent. You can sign in once a Super User approves it.', 'ok')
            return redirect(url_for('auth.login'))
    return render_template('register.html')


@bp.post('/logout')
def logout():
    with D.tx() as c:
        D.audit(c, 'auth.logout', g.user['username'] if g.user else '', '', g.user)
    session.clear()
    flash('Signed out.', 'info')
    return redirect(url_for('auth.login'))
