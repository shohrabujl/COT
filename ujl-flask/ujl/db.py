"""SQLite access: one connection per request, the schema, settings and the hash-chained audit log."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from flask import current_app, g

from . import engine as E

GENESIS = '0' * 64
DHAKA = timezone(timedelta(hours=6))          # Bangladesh time, no daylight saving
DEFAULT_SETTINGS = {'high_var_threshold': 3, 'hours': 8, 'aging_days': 3, 'urgent_window_days': 14, 'ready_window_days': 0,
                    'as_of_mode': 'today', 'as_of_fixed': None}
_LOCK = threading.RLock()                    # every write (and so every audit entry) goes through this lock

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  name TEXT NOT NULL,
  pw_hash TEXT NOT NULL,
  role TEXT,
  status TEXT NOT NULL DEFAULT 'pending',      -- pending | active | disabled | rejected
  must_change INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_login TEXT
);
CREATE TABLE IF NOT EXISTS roles(name TEXT PRIMARY KEY, perms TEXT NOT NULL, sort INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS batches(
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, file_name TEXT, headers TEXT NOT NULL, data TEXT NOT NULL,
  row_count INTEGER NOT NULL, by_user INTEGER, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cards(
  key TEXT PRIMARY KEY, stage TEXT NOT NULL, priority TEXT NOT NULL, owner TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
  locked INTEGER NOT NULL DEFAULT 0, by_user INTEGER, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS linewise(
  id INTEGER PRIMARY KEY AUTOINCREMENT, n INTEGER NOT NULL, line TEXT NOT NULL, pf TEXT, manpower REAL, smv REAL, eff REAL,
  actual_input INTEGER, area TEXT NOT NULL DEFAULT '', total_input REAL, total_output REAL, ready REAL, remarks TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, user_id INTEGER, username TEXT, action TEXT NOT NULL,
  entity TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '', prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS login_attempts(id INTEGER PRIMARY KEY AUTOINCREMENT, k TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_attempts ON login_attempts(k, ts);
CREATE TABLE IF NOT EXISTS imports(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=20, isolation_level=None)     # autocommit; tx() opens explicit transactions
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA busy_timeout=20000')
    return con


def get_db() -> sqlite3.Connection:
    if '_db' not in g:
        g._db = connect(current_app.config['DB_PATH'])
    return g._db


def close_db(_exc=None) -> None:
    con = g.pop('_db', None)
    if con is not None:
        con.close()


def init_schema(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    if not con.execute('SELECT 1 FROM roles LIMIT 1').fetchone():
        for i, (name, perms) in enumerate(E.DEFAULT_ROLES.items()):
            con.execute('INSERT INTO roles(name, perms, sort) VALUES(?,?,?)', (name, json.dumps(perms), i))
    con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('main', ?)", (json.dumps(DEFAULT_SETTINGS),))


def init_app(app) -> None:
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_schema(get_db())


@contextmanager
def tx():
    """One write transaction at a time (so the audit chain never forks). Roll back on any error."""
    con = get_db()
    with _LOCK:
        con.execute('BEGIN IMMEDIATE')
        try:
            yield con
        except BaseException:
            con.execute('ROLLBACK')
            raise
        else:
            con.execute('COMMIT')


# ---------- audit log: each entry's hash covers the one before it ----------
def _hash(prev, ts, uid, uname, action, entity, detail) -> str:
    blob = json.dumps([prev, ts, uid, uname, action, entity, detail], ensure_ascii=True, separators=(',', ':'))
    return hashlib.sha256(blob.encode('ascii')).hexdigest()


def audit(con, action: str, entity: str = '', detail: str = '', user=None, username: str = '') -> None:
    with _LOCK:
        row = con.execute('SELECT hash FROM audit ORDER BY id DESC LIMIT 1').fetchone()
        prev = row['hash'] if row else GENESIS
        ts, uid = utcnow(), (user['id'] if user else None)
        uname = user['username'] if user else username
        entity, detail = str(entity)[:200], str(detail)[:600]
        con.execute('INSERT INTO audit(ts,user_id,username,action,entity,detail,prev_hash,hash) VALUES(?,?,?,?,?,?,?,?)',
                    (ts, uid, uname, action, entity, detail, prev, _hash(prev, ts, uid, uname, action, entity, detail)))


def verify_audit(con) -> dict:
    prev, n = GENESIS, 0
    for r in con.execute('SELECT * FROM audit ORDER BY id'):
        n += 1
        if r['prev_hash'] != prev or r['hash'] != _hash(prev, r['ts'], r['user_id'], r['username'], r['action'], r['entity'], r['detail']):
            return {'ok': False, 'count': n, 'bad_id': r['id']}
        prev = r['hash']
    return {'ok': True, 'count': n, 'head': prev}


# ---------- settings, meta, dates ----------
def get_settings(con) -> dict:
    st = dict(DEFAULT_SETTINGS)
    row = con.execute("SELECT value FROM settings WHERE key='main'").fetchone()
    if row:
        try:
            st.update(json.loads(row['value']))
        except ValueError:
            pass
    return st


def save_settings(con, st: dict) -> None:
    con.execute("INSERT INTO settings(key, value) VALUES('main', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(st),))


def get_meta(con) -> dict:
    return {r['key']: r['value'] for r in con.execute('SELECT key, value FROM meta')}


def set_meta(con, **kw) -> None:
    for k, v in kw.items():
        con.execute('INSERT INTO meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (k, None if v is None else str(v)))


def today_serial() -> int:
    return (datetime.now(DHAKA).date() - E.EPOCH).days


def as_of_serial(st: dict) -> int:
    if st.get('as_of_mode') == 'fixed' and st.get('as_of_fixed') is not None:
        return int(st['as_of_fixed'])
    return today_serial()
