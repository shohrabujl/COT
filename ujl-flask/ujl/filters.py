"""Template helpers: number and date formats, colours, and what every page needs (menu, permissions, as-of date)."""
from __future__ import annotations

import math
from datetime import datetime, timezone

from flask import g, request

from . import auth
from . import data as DT
from . import db as D
from . import engine as E

TYPE_CLS = {'High variation': 'bad', 'Process change': 'warn', 'Same process': 'ok', 'Line start': 'info', 'Master data missing': ''}
STC = {'Data Blocked': '#dc2626', 'Planned': '#64748b', 'Ready': '#3b82f6', 'Running': '#10b981', 'Done': '#8b5cf6'}
MIXC = {'Line start': '#94a3b8', 'Same process': '#10b981', 'Process change': '#f59e0b', 'High variation': '#dc2626', 'Master data missing': '#64748b'}
NAV = [('main.dashboard', 'Dashboard', '▦', ('dashboard.view',)), ('main.kanban', 'Kanban', '☰', ('kanban.view',)),
       ('main.plan', 'Plan', '≣', ('plan.view',)), ('main.health', 'Health', '✓', ('plan.view',)),
       ('main.linewise', 'Line-wise', '◷', ('linewise.view',)), ('main.master', 'PF master', '⚙', ('plan.view',)),
       ('admin.index', 'Admin', '★', ('users.manage', 'audit.view', 'settings.edit'))]


def fmt(x, default=''):
    if x is None or x == '':
        return default
    try:
        return f'{int(math.floor(float(x) + 0.5)):,}'
    except (TypeError, ValueError, OverflowError):
        return str(x)


def pct1(x):
    return '' if x is None else f'{x * 100:.1f}%'


def local_time(s):
    """A stored UTC time shown in Bangladesh time."""
    try:
        t = datetime.strptime(s, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).astimezone(D.DHAKA)
        return t.strftime('%d-%b-%y %H:%M')
    except (TypeError, ValueError):
        return s or ''


def status_cls(status):
    return 'ok' if status == 'Matched' else 'warn' if status == 'Incomplete' else 'bad'


def first_allowed():
    """Endpoint of the first menu item this user may open, or None."""
    return next((ep for ep, _l, _i, perms in NAV if any(auth.can(p) for p in perms)), None)


def init_app(app) -> None:
    app.jinja_env.filters.update(fmt=fmt, dstr=E.dstr, mstr=E.mstr, iso=E.iso, pct1=pct1, local_time=local_time)
    app.jinja_env.globals.update(can=auth.can, csrf_input=auth.csrf_input, csrf_token=auth.csrf_token, TYPE_CLS=TYPE_CLS, STC=STC, MIXC=MIXC,
                                 STAGES=E.STAGES, PRIORITIES=DT.PRIORITIES, AREAS=DT.AREAS, OPS=E.OPS, VARIANTS=E.DEFAULT_VARIANTS,
                                 PERMISSIONS=E.PERMISSIONS, SUPER=E.SUPER, status_cls=status_cls)

    @app.context_processor
    def page_context():
        user = getattr(g, 'user', None)
        if not user:
            return {'nav': [], 'asof': ''}
        ep = request.endpoint or ''
        nav = [{'ep': e, 'label': label, 'icon': icon, 'on': ep == e or (e == 'admin.index' and ep.startswith('admin.'))}
               for e, label, icon, perms in NAV if any(auth.can(p) for p in perms)]
        return {'nav': nav, 'asof': E.dstr(D.as_of_serial(D.get_settings(D.get_db())))}
