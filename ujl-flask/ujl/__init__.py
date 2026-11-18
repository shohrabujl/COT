"""UJL Plan Control Center - local Flask app (SQLite + openpyxl). Every business rule lives in ujl/engine.py."""
from __future__ import annotations

import os
import secrets
from datetime import timedelta

from flask import Flask

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _secret_key(data_dir: str) -> str:
    path = os.path.join(data_dir, 'secret.key')
    if os.path.isfile(path):
        with open(path, encoding='ascii') as f:
            key = f.read().strip()
        if len(key) >= 32:
            return key
    key = secrets.token_hex(32)
    with open(path, 'w', encoding='ascii') as f:
        f.write(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def create_app(data_dir=None) -> Flask:
    data_dir = data_dir or os.environ.get('UJL_DATA') or os.path.join(ROOT, 'data')
    os.makedirs(data_dir, exist_ok=True)
    app = Flask(__name__)
    app.config.update(DATA_DIR=data_dir, DB_PATH=os.path.join(data_dir, 'ujl.db'), APP_ROOT=ROOT, MAX_CONTENT_LENGTH=25 * 1024 * 1024,
                      SESSION_COOKIE_NAME='ujl_session', SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=12))
    app.secret_key = _secret_key(data_dir)

    from . import auth, db, filters, views_admin, views_auth, views_main
    db.init_app(app)
    auth.init_app(app)
    filters.init_app(app)
    for mod in (views_auth, views_main, views_admin):
        app.register_blueprint(mod.bp)
    return app
