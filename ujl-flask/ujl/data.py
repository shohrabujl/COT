"""Turns what the database holds (plan, master, saved cards, line-wise rows, settings) into screen-ready numbers, using the engine."""
from __future__ import annotations

import json
import math
from datetime import date

from . import db as D
from . import engine as E

PRIORITIES = ['High', 'Medium', 'Low']
AREAS = ['', 'OUTPUT', '1ST OUTPUT', '2ND OUTPUT', 'LAST OUTPUT', 'SELVEDGE', 'FRONT PREPARATION', 'FRONT OUTPUT', 'BK OUTPUT', 'ASSEMBLY']
NUM_FIELDS = ('manpower', 'smv', 'total_input', 'total_output', 'ready')


def current_batch(con, kind: str):
    bid = D.get_meta(con).get(f'{kind}_id')
    return con.execute('SELECT * FROM batches WHERE id=?', (bid,)).fetchone() if bid else None


def overlay(con) -> dict:
    return {r['key']: {'stage': r['stage'], 'priority': r['priority'], 'owner': r['owner'], 'notes': r['notes'], 'locked': bool(r['locked'])}
            for r in con.execute('SELECT * FROM cards')}


def put_card(con, key: str, o: dict, user) -> None:
    con.execute('INSERT INTO cards(key,stage,priority,owner,notes,locked,by_user,ts) VALUES(?,?,?,?,?,?,?,?) '
                'ON CONFLICT(key) DO UPDATE SET stage=excluded.stage, priority=excluded.priority, owner=excluded.owner, notes=excluded.notes, '
                'locked=excluded.locked, by_user=excluded.by_user, ts=excluded.ts',
                (key, o['stage'], o['priority'], o.get('owner') or '', o.get('notes') or '', 1 if o.get('locked') else 0,
                 user['id'] if user else None, D.utcnow()))


def engine_settings(st: dict, as_of: int) -> E.Settings:
    return E.Settings(high_var_threshold=int(st['high_var_threshold']), ready_window_days=int(st['ready_window_days']),
                      urgent_window_days=int(st['urgent_window_days']), as_of=as_of)


def load_state(con):
    """Everything the screens need, or None while no plan and PF master have been imported."""
    pb, mb = current_batch(con, 'plan'), current_batch(con, 'master')
    if not pb or not mb:
        return None
    st = D.get_settings(con)
    as_of = D.as_of_serial(st)
    es = engine_settings(st, as_of)
    mrows = json.loads(mb['data'])
    master = E.build_master(json.loads(mb['headers']), mrows)
    plan = E.clean_plan(json.loads(pb['headers']), json.loads(pb['data']), master)
    runs = E.build_runs(plan['rows'], es)
    ov = overlay(con)
    cards, removed = E.sync_cards(runs, ov, es)
    return {'as_of': as_of, 'es': es, 'st': st, 'master': master, 'mrows': mrows, 'plan': plan, 'runs': runs, 'cards': cards,
            'removed': removed, 'overlay': ov, 'summary': E.summarise(cards), 'health': E.health_checks(plan, master),
            'reports': E.reports(plan, runs, master, as_of), 'meta': D.get_meta(con)}


def card_dict(c: E.Card) -> dict:
    r = c.run
    return {'key': c.key, 'line': r.line, 'pf': str(r.to_pf), 'buyer': r.buyer_group, 'style': r.style, 'qty': int(round(r.plan_qty)),
            'start': E.dstr(r.sew_start), 'end': E.dstr(r.sew_end), 'type': r.type, 'changes': r.changes, 'what': r.what,
            'status': r.status, 'stage': c.stage, 'priority': c.priority, 'owner': c.owner, 'notes': c.notes, 'locked': c.locked}


# ---------- line-wise ----------
def lw_input(r) -> dict:
    pf = r['pf']
    n = E.num(pf) if pf not in (None, '') else None
    return {'line': r['line'], 'pf': n if n is not None else (pf or None), 'manpower': r['manpower'], 'smv': r['smv'], 'eff': r['eff'],
            'actual_input': r['actual_input'], 'area': r['area'], 'total_input': r['total_input'], 'total_output': r['total_output'], 'ready': r['ready']}


def lw_table(con, s):
    """(rows, derived, totals). Works before any plan is imported (every row then shows as having no plan)."""
    st = D.get_settings(con)
    runs = s['runs'] if s else []
    as_of = s['as_of'] if s else D.as_of_serial(st)
    rows = [dict(r) for r in con.execute('SELECT * FROM linewise ORDER BY n, id')]
    ins = [lw_input(r) for r in rows]
    derived = [E.line_wise_row(i, runs, as_of, float(st['hours']), float(st['aging_days'])) for i in ins]
    return rows, derived, E.line_wise_totals(derived, ins), as_of, st


def _n(x) -> str:
    if x is None or x == '':
        return ''
    return f'{int(math.floor(float(x) + 0.5)):,}'


def lw_cells(d: dict) -> dict:
    """Display text and CSS class for each calculated cell of one line-wise row."""
    chip = 'ok' if d['status'].startswith('R') else 'warn' if d['status'].startswith('P') else ''
    return {'status': [d['status'], 'chip ' + chip], 'plan_qty': [_n(d['plan_qty']), ''],
            'days_since_plan': ['' if d['days_since_plan'] is None else str(d['days_since_plan']), 'h5' if d['aging'] else ''],
            'days_running': ['' if d['days_running'] is None else str(d['days_running']), ''], 'max_target': [_n(d['max_target']), ''],
            'today_target': [_n(d['today_target']), ''], 'wip': [_n(d['wip']), ''],
            'completion': ['' if d['completion'] is None else f"{d['completion'] * 100:.1f}%", ''], 'balance': [_n(d['balance']), '']}


def lw_total_cells(t: dict) -> dict:
    return {'input': _n(t['input']), 'output': _n(t['output']), 'ready': _n(t['ready']), 'plan': _n(t['plan']), 'today': _n(t['today']),
            'wip': _n(t['wip']), 'completion': '' if t['completion'] is None else f"{t['completion'] * 100:.1f}%", 'balance': _n(t['balance'])}


def parse_lw(field: str, raw, lines) -> object:
    """Validates one edited line-wise cell and returns the value to store. Raises ValueError with a plain message."""
    raw = '' if raw is None else str(raw).strip()
    if field in NUM_FIELDS or field == 'eff':
        if raw == '':
            return None
        try:
            x = float(raw)
        except ValueError:
            raise ValueError('Enter a number.')
        if not math.isfinite(x) or x < 0 or x > 1e9:
            raise ValueError('Enter a number of 0 or more.')
        if field == 'eff':
            if x > 200:
                raise ValueError('Efficiency is a percentage (for example 85).')
            return x / 100
        return x
    if field == 'actual_input':
        if raw == '':
            return None
        try:
            return (date.fromisoformat(raw) - E.EPOCH).days
        except ValueError:
            raise ValueError('Enter a valid date.')
    if field == 'pf':
        if raw == '':
            return None
        n = E.num(raw)
        return str(int(n)) if n is not None and float(n).is_integer() else raw[:30]
    if field == 'area':
        if raw not in AREAS:
            raise ValueError('Choose an area from the list.')
        return raw
    if field == 'line':
        if not raw or len(raw) > 40 or (lines and raw not in lines):
            raise ValueError('Choose a line from the list.')
        return raw
    if field == 'remarks':
        return raw[:200]
    raise ValueError('That column cannot be edited.')
