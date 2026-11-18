"""Accounts, sessions, CSRF, login throttling and server-side permission checks."""
from __future__ import annotations

import functools
import hashlib
import hmac
import json
import re
import secrets
import time

from flask import g, jsonify, redirect, render_template, request, session, url_for
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

from . import db as D
from . import engine as E

USERNAME_RX = re.compile(r'^[A-Za-z0-9._-]{3,32}$')
PUBLIC = {'auth.login', 'auth.register', 'auth.setup', 'static'}
SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS'}
LOCAL_HOSTS = {'127.0.0.1', 'localhost', '[::1]'}
FAIL_LIMIT, IP_LIMIT, WINDOW = 5, 40, 15 * 60          # failed sign-ins per username / per address, within 15 minutes
CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; base-uri 'self'; "
       "form-action 'self'; frame-ancestors 'none'")


# ---------- passwords ----------
def hash_pw(pw: str) -> str:
    try:
        return generate_password_hash(pw)
    except (ValueError, AttributeError):                # Python builds without scrypt
        return generate_password_hash(pw, method='pbkdf2:sha256:600000')


_DUMMY = hash_pw('placeholder-password')


def check_pw(pw_hash, pw: str) -> bool:
    """Always does one hash check, so an unknown username takes as long as a wrong password."""
    ok = check_password_hash(pw_hash or _DUMMY, pw)
    return bool(pw_hash) and ok


def pw_problem(username: str, pw: str):
    if len(pw) < 8:
        return 'Use at least 8 characters for the password.'
    if len(pw) > 128:
        return 'The password is too long (128 characters at most).'
    if pw.lower() == username.lower():
        return 'The password must differ from the username.'
    return None


def user_problem(con, username: str, name: str, pw: str):
    if not USERNAME_RX.match(username):
        return 'Username: 3 to 32 letters, digits, dot, dash or underscore.'
    if not 2 <= len(name) <= 60:
        return 'Enter your full name (2 to 60 characters).'
    if con.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone():
        return 'That username is already taken.'
    return pw_problem(username, pw)


def make_user(con, username, name, pw, role, status, must_change=0) -> int:
    now = D.utcnow()
    cur = con.execute('INSERT INTO users(username,name,pw_hash,role,status,must_change,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                      (username, name, hash_pw(pw), role, status, must_change, now, now))
    return cur.lastrowid


def has_users(con) -> bool:
    return con.execute('SELECT 1 FROM users LIMIT 1').fetchone() is not None


def active_supers(con) -> int:
    return con.execute("SELECT COUNT(*) c FROM users WHERE role=? AND status='active'", (E.SUPER,)).fetchone()['c']


# ---------- sessions and permissions ----------
def fingerprint(pw_hash: str) -> str:
    return hashlib.sha256(pw_hash.encode()).hexdigest()[:16]     # a password change ends every other session


def sign_in(user) -> None:
    session.clear()
    session.permanent = True
    session['uid'], session['sv'], session['csrf'] = user['id'], fingerprint(user['pw_hash']), secrets.token_urlsafe(32)


def perms_for(con, role):
    if role == E.SUPER:
        return frozenset(E.ALL_PERMS)
    row = con.execute('SELECT perms FROM roles WHERE name=?', (role,)).fetchone()
    return frozenset(json.loads(row['perms'])) & frozenset(E.ALL_PERMS) if row else frozenset()


def can(perm: str) -> bool:
    return perm in getattr(g, 'perms', ())


def is_super() -> bool:
    return bool(getattr(g, 'user', None)) and g.user['role'] == E.SUPER


def wants_json() -> bool:
    return request.path.startswith('/api/') or request.is_json


def fail(msg: str, code: int):
    if wants_json():
        return jsonify(ok=False, error=msg), code
    return render_template('error.html', code=code, msg=msg), code


def needs(*perms):
    """The user must hold at least one of these permissions."""
    def deco(fn):
        @functools.wraps(fn)
        def wrap(*a, **k):
            if not any(can(p) for p in perms):
                return fail('Your role does not allow this.', 403)
            return fn(*a, **k)
        return wrap
    return deco


def super_only(fn):
    @functools.wraps(fn)
    def wrap(*a, **k):
        if not is_super():
            return fail('Only a Super User can do this.', 403)
        return fn(*a, **k)
    return wrap


# ---------- CSRF ----------
def csrf_token() -> str:
    if 'csrf' not in session:
        session['csrf'] = secrets.token_urlsafe(32)
    return session['csrf']


def csrf_input() -> Markup:
    return Markup('<input type="hidden" name="csrf_token" value="%s">') % csrf_token()


# ---------- login throttling ----------
def locked_out(con, keys) -> bool:
    cutoff = time.time() - WINDOW
    return any(con.execute('SELECT COUNT(*) c FROM login_attempts WHERE k=? AND ts>?', (k, cutoff)).fetchone()['c'] >= limit for k, limit in keys)


def note_failure(con, keys) -> None:
    now = time.time()
    con.execute('DELETE FROM login_attempts WHERE ts<?', (now - WINDOW,))
    for k in keys:
        con.execute('INSERT INTO login_attempts(k, ts) VALUES(?,?)', (k, now))


def clear_failures(con, key: str) -> None:
    con.execute('DELETE FROM login_attempts WHERE k=?', (key,))


# ---------- request hooks ----------
def _host_check():
    host = (request.host or '').lower()
    host = host[:host.index(']') + 1] if host.startswith('[') else host.split(':')[0]
    if host not in LOCAL_HOSTS:
        return fail('This app only answers on this computer (127.0.0.1).', 403)


def _load_user():
    g.user, g.perms = None, frozenset()
    uid = session.get('uid')
    if uid:
        con = D.get_db()
        u = con.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
        if u and u['status'] == 'active' and session.get('sv') == fingerprint(u['pw_hash']):
            g.user, g.perms = u, perms_for(con, u['role'])
        else:
            session.clear()


def _csrf_check():
    if request.method in SAFE_METHODS:
        return None
    sent = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token') or ''
    want = session.get('csrf') or ''
    if not want or not hmac.compare_digest(sent.encode(), want.encode()):
        return fail('Your page has expired. Reload it and try again.', 400)


def _gate():
    ep = request.endpoint
    if ep is None or ep == 'static':
        return None
    con = D.get_db()
    if not has_users(con):
        return None if ep == 'auth.setup' else redirect(url_for('auth.setup'))
    if ep == 'auth.setup':
        return redirect(url_for('auth.login'))
    if ep in PUBLIC:
        return None
    if g.user is None:
        if wants_json():
            return jsonify(ok=False, error='Please sign in again.'), 401
        return redirect(url_for('auth.login', next=request.full_path.rstrip('?') if request.method == 'GET' else None))
    if g.user['must_change'] and ep not in ('main.account', 'auth.logout'):
        return redirect(url_for('main.account'))
    return None


def _headers(resp):
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'DENY'
    resp.headers['Referrer-Policy'] = 'same-origin'
    resp.headers['Content-Security-Policy'] = CSP
    if request.endpoint != 'static':
        resp.headers['Cache-Control'] = 'no-store'
    return resp


def init_app(app) -> None:
    app.before_request(_host_check)
    app.before_request(_load_user)
    app.before_request(_csrf_check)
    app.before_request(_gate)
    app.after_request(_headers)

    @app.errorhandler(404)
    def _404(_e):
        return fail('That page does not exist.', 404)

    @app.errorhandler(413)
    def _413(_e):
        return fail('That file is too large (25 MB at most).', 413)

    @app.errorhandler(500)
    def _500(_e):
        return fail('Something went wrong on the server. Nothing was changed. Try again, or ask the person who set this up.', 500)
