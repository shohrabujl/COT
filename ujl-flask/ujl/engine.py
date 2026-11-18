"""UJL Plan Control Center - every business rule, as plain Python (no web or database code in here)."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

OPS = ['INSEAM', 'BK YOKE', 'BK RISE', 'SIDE SEAM', 'WAIST BAND', 'LOOP B/T', 'BTM HEM']
STAGES = ['Data Blocked', 'Planned', 'Ready', 'Running', 'Done']
PLAN_FIELDS = {
    'line': 'Line*', 'buyer': 'Buyer*', 'pf': 'PF#*', 'style': 'Style#*', 'desc': 'Product desc*', 'poFr': 'PO rcvd*', 'po': 'PO#*',
    'color': 'Colo*r*', 'country': 'Country*', 'orderQty': 'Order Qty*', 'planQty': 'Plan Qty*', 'cutStart': 'Cutting Start*',
    'cutEnd': 'Cutting End*', 'sewIn': 'Sewing Input*', 'firstOut': '1st Output*', 'sewEnd': 'Sew End*', 'bpo': 'BPO Insp*',
}
MASTER_FIELDS = {'buyer': 'BUYER*', 'pf': 'PF*', **{op: op + '*' for op in OPS}, 'remarks': 'REMARKS*'}
PLAN_HEADERS = ['Line', 'Buyer', 'PF#', 'Style#', 'Product description', 'PO rcvd in FR', 'PO#', 'Color', 'Country', 'Order Qty', 'Plan Qty',
                'Cutting Start', 'Cutting End', 'Sewing Input', '1st Output', 'Sew End', 'BPO Inspection Date']
MASTER_HEADERS = ['BUYER', 'PF'] + OPS + ['REMARKS']
DEFAULT_BUYER_MAP = {
    'UQ::UNIQLO': 'UNIQLO', 'LI & FUNG::AMERICAN EAGLE': 'AEO', 'ITX::ZARA BASIC': 'ZARA', 'ITX::ZARA MAN': 'ZARA',
    'PVH::CALVIN KLEIN': 'CK', 'PVH::CK COSTCO': 'CK', 'C&A::THE DENIM': 'C&A',
}
DEFAULT_VARIANTS = {
    'INSEAM': ['I/L', 'FOA'], 'BK YOKE': ['I/L', 'FOA'], 'BK RISE': ['I/L', 'FOA'], 'SIDE SEAM': ['SNCS', 'I/L'],
    'WAIST BAND': ['S/F', 'D/F', 'CLEAN FINISH', 'HIDDEN STC'], 'LOOP B/T': ['SETTER', 'B/T'], 'BTM HEM': ['VBM', 'I/L', 'LOCK STC'],
}
PERMISSIONS = [
    ('dashboard.view', 'View dashboard'), ('plan.view', 'View plan, sequence, health and PF master'), ('kanban.view', 'View Kanban board'),
    ('kanban.move', 'Move cards between stages'), ('kanban.edit', 'Edit card priority, owner and notes; unlock'),
    ('import.run', 'Import plan and master files'), ('master.edit', 'Edit PF process master'), ('linewise.view', 'View line-wise tracking'),
    ('linewise.edit', 'Edit line-wise tracking'), ('export.csv', 'Export CSV'), ('audit.view', 'View the audit log'),
    ('users.manage', 'Manage users (approve, roles, disable)'), ('settings.edit', 'Change rules and settings'),
]
ALL_PERMS = [p for p, _ in PERMISSIONS]
SUPER = 'Super User'
DEFAULT_ROLES = {
    'Planner': [p for p in ALL_PERMS if p not in ('audit.view', 'users.manage', 'settings.edit')],
    'Line Supervisor': ['dashboard.view', 'plan.view', 'kanban.view', 'kanban.edit', 'linewise.view', 'linewise.edit', 'export.csv'],
    'Viewer': ['dashboard.view', 'plan.view', 'kanban.view', 'linewise.view'],
}


@dataclass
class Settings:
    high_var_threshold: int = 3        # "High variation" when >= this many of the 7 processes differ
    ready_window_days: int = 0         # 0 = Ready is set only by dragging a card by hand
    urgent_window_days: int = 14       # a blocked card seeds as High priority if it starts within this many days
    blocked_statuses: tuple = ('Not in master', 'PF not assigned', 'Incomplete')
    as_of: int = 46327                 # Excel-style day serial (01-Nov-2026 = 46327)


# ---------- primitives ----------
_NUM = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?')
MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
_MON = {m.lower(): i for i, m in enumerate(MONTHS)}
EPOCH = date(1899, 12, 30)


def num(v: Any):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        if isinstance(v, float):
            if not math.isfinite(v):
                return None
            return int(v) if v.is_integer() else v
        return v
    s = str(v).replace(',', '').strip()
    if not _NUM.fullmatch(s):
        return None
    f = float(s)
    return int(f) if f.is_integer() and abs(f) < 1e15 else f


def cell_str(v: Any) -> str:
    if v is None:
        return ''
    if isinstance(v, datetime):
        return f'{v.day:02d}-{MONTHS[v.month - 1]}-{v.year % 100:02d}'
    if isinstance(v, date):
        return f'{v.day:02d}-{MONTHS[v.month - 1]}-{v.year % 100:02d}'
    if isinstance(v, bool):
        return str(v).upper()
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() and abs(v) < 1e15 else format(v, '.15g')
    return re.sub(r'[\r\n\t\x1f]+', ' ', str(v)).strip()


def to_serial(y: int, m0: int, d: int) -> int:
    return (date(y, m0 + 1, d) - EPOCH).days


def parse_date(v: Any) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, datetime):
        return (v.date() - EPOCH).days
    if isinstance(v, date):
        return (v - EPOCH).days
    if isinstance(v, (int, float)):
        return int(math.floor(v))
    s = str(v).strip()
    if not s:
        return None
    m = re.fullmatch(r'([0-9]{1,2})-([A-Za-z]{3})-([0-9]{2}|[0-9]{4})', s)
    if not m:
        n = num(s)
        return int(n) if n is not None and n > 20000 else None
    mo = _MON.get(m.group(2).lower())
    if mo is None:
        return None
    y = int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)
    try:
        return to_serial(y, mo, int(m.group(1)))
    except ValueError:
        return None


def from_serial(s: int) -> date:
    return EPOCH + timedelta(days=int(s))


def ymd(s: Optional[int]) -> str:
    return '' if s is None else from_serial(s).strftime('%Y%m%d')


def dstr(s: Optional[int]) -> str:
    if s is None or s == '':
        return ''
    d = from_serial(s)
    return f'{d.day:02d}-{MONTHS[d.month - 1]}-{d.year % 100:02d}'


def mstr(s: int) -> str:
    d = from_serial(s)
    return f'{MONTHS[d.month - 1]} {d.year % 100:02d}'


def iso(s: Optional[int]) -> str:
    return '' if s is None or s == '' else from_serial(s).isoformat()


def map_columns(headers: List[Any], patterns: Dict[str, str]) -> dict:
    idx: Dict[str, int] = {}
    missing: List[str] = []
    hs = [cell_str(h) for h in headers]
    for k, p in patterns.items():
        rx = re.compile('^' + re.escape(p).replace(r'\*', '.*') + '$', re.I)
        i = next((i for i, h in enumerate(hs) if rx.match(h)), -1)
        if i < 0:
            missing.append(k)
        else:
            idx[k] = i
    total = len(patterns)
    return {'idx': idx, 'missing': missing, 'recognised': total - len(missing), 'total': total}


def norm_pf(raw: Any):
    s = cell_str(raw)
    if not s:
        return '(no PF)', False
    t = s.replace(',', '')
    if re.fullmatch(r'[0-9]+', t):
        return int(t), False
    parts = re.sub(r'[#-]', ' ', s).split()
    last = parts[-1] if parts else ''
    if re.fullmatch(r'[0-9]+', last):
        return int(last), True
    return s, False


def buyer_group(buyer: Any, mapping: Optional[dict] = None) -> str:
    mapping = DEFAULT_BUYER_MAP if mapping is None else mapping
    t = cell_str(buyer)
    if not t:
        return '(no buyer)'
    if t in mapping:
        return mapping[t]
    i = t.find('::')
    return t[i + 2:].strip() if i >= 0 else t


def pack_sheet(kind: str, headers: List[Any], rows: List[List[Any]]) -> dict:
    """Re-orders any recognised sheet into canonical columns of plain text (what the database stores)."""
    fields = PLAN_FIELDS if kind == 'plan' else MASTER_FIELDS
    cm = map_columns(headers, fields)
    keys = list(fields)
    out = []
    for r in rows:
        if not any(cell_str(c) != '' for c in r):
            continue
        out.append([cell_str(r[cm['idx'][k]]) if k in cm['idx'] and cm['idx'][k] < len(r) else '' for k in keys])
    return {'headers': list(PLAN_HEADERS if kind == 'plan' else MASTER_HEADERS), 'rows': out, 'recognised': cm['recognised'], 'total': cm['total']}


# ---------- master ----------
def build_master(headers: List[Any], raw: List[List[Any]]) -> dict:
    cols = map_columns(headers, MASTER_FIELDS)

    def g(r, k):
        i = cols['idx'].get(k)
        return '' if i is None or i >= len(r) else cell_str(r[i])

    rows, by_pf, dups = [], {}, 0
    for r in raw:
        pf = num(g(r, 'pf'))
        if pf is None:
            continue
        m = {'pf': pf, 'buyer': g(r, 'buyer'), 'values': [g(r, op) for op in OPS], 'remarks': g(r, 'remarks')}
        rows.append(m)
        if pf in by_pf:
            dups += 1
        else:
            by_pf[pf] = m
    return {'by_pf': by_pf, 'rows': rows, 'duplicates': dups, 'cols': cols,
            'incomplete': sum(1 for m in rows if any(v == '' for v in m['values']))}


# ---------- plan cleaning ----------
@dataclass
class CleanRow:
    seq: int
    raw_row: int
    line: str
    buyer: str
    buyer_group: str
    pf: Any
    pf_as_received: str
    pf_text_to_number: bool
    style: str
    order_qty: float
    plan_qty: float
    sew_in: Optional[int]
    sew_end: Optional[int]
    bpo: Optional[int]
    sewing_days: Optional[int]
    sewing_month: Optional[int]
    date_check: str
    status: str
    proc: List[str]
    run_no: int
    run_start: bool


def clean_plan(headers: List[Any], raw: List[List[Any]], master: dict, buyer_map: Optional[dict] = None) -> dict:
    cols = map_columns(headers, PLAN_FIELDS)

    def g(r, k):
        i = cols['idx'].get(k)
        return '' if i is None or i >= len(r) else cell_str(r[i])

    kept = [(r, i + 2) for i, r in enumerate(raw) if any(cell_str(c) != '' for c in r)]
    grand = next((n for n in (num(g(r, 'planQty')) for r, _ in reversed(kept)) if n is not None), None)
    rows: List[CleanRow] = []
    prev: Optional[CleanRow] = None
    for r, raw_row in kept:
        line = g(r, 'line')
        plan_qty = num(g(r, 'planQty')) or 0
        if not line or re.search('total', line, re.I) or not (g(r, 'pf') or g(r, 'style')) or not plan_qty > 0:
            continue                                   # subtotal / blank rows
        pf, t2n = norm_pf(g(r, 'pf'))
        m = master['by_pf'].get(pf) if isinstance(pf, int) else None
        proc = list(m['values']) if m else [''] * len(OPS)
        status = 'PF not assigned' if not isinstance(pf, int) else 'Not in master' if m is None else 'Incomplete' if any(v == '' for v in proc) else 'Matched'
        sew_in, sew_end, bpo = parse_date(g(r, 'sewIn')), parse_date(g(r, 'sewEnd')), parse_date(g(r, 'bpo'))
        run_start = prev is None or prev.line != line or prev.pf != pf
        month = None
        if sew_in is not None:
            d = from_serial(sew_in)
            month = to_serial(d.year, d.month - 1, 1)
        row = CleanRow(
            seq=len(rows) + 1, raw_row=raw_row, line=line, buyer=g(r, 'buyer'), buyer_group=buyer_group(g(r, 'buyer'), buyer_map), pf=pf,
            pf_as_received=g(r, 'pf'), pf_text_to_number=t2n, style=g(r, 'style'), order_qty=num(g(r, 'orderQty')) or 0, plan_qty=plan_qty,
            sew_in=sew_in, sew_end=sew_end, bpo=bpo, sewing_days=sew_end - sew_in + 1 if sew_in is not None and sew_end is not None else None,
            sewing_month=month,
            date_check='BPO < Sew End' if bpo is not None and sew_end is not None and bpo < sew_end
            else 'Sew End < Input' if sew_end is not None and sew_in is not None and sew_end < sew_in else '',
            status=status, proc=proc, run_no=(prev.run_no if prev else 0) + (1 if run_start else 0), run_start=run_start)
        rows.append(row)
        prev = row
    return {'rows': rows, 'raw_count': len(kept), 'dropped': len(kept) - len(rows), 'grand_total': grand, 'cols': cols}


# ---------- runs & changeovers (Line Sequence) ----------
@dataclass
class Run:
    run_no: int
    line: str
    seq: int
    from_pf: Any
    to_pf: Any
    buyer_group: str
    style: str
    status: str
    proc: List[str]
    changes: Optional[int]
    what: str
    comparable: Optional[bool]
    first_row: int
    last_row: int
    order_qty: float = 0
    plan_qty: float = 0
    po_lines: int = 0
    sew_start: Optional[int] = None
    sew_end: Optional[int] = None
    type: str = 'Line start'
    rank: Optional[int] = None


def _usable(s: str) -> bool:
    return s in ('Matched', 'Incomplete')


def classify(r: Run, s: Settings) -> str:
    if r.seq == 1:
        return 'Line start'
    if not r.comparable:
        return 'Master data missing'
    if r.changes == 0:
        return 'Same process'
    return 'High variation' if (r.changes or 0) >= s.high_var_threshold else 'Process change'


def reclassify(runs: List[Run], s: Settings) -> List[Run]:
    for r in runs:
        r.type = classify(r, s)
    return runs


def build_runs(rows: List[CleanRow], s: Optional[Settings] = None) -> List[Run]:
    s = s or Settings()
    runs: List[Run] = []
    last: Dict[str, Run] = {}
    cur: Optional[Run] = None
    for r in rows:
        if r.run_start:
            prev = last.get(r.line)
            seq = (prev.seq if prev else 0) + 1
            comparable = seq > 1 and prev is not None and _usable(r.status) and _usable(prev.status)
            diffs = [f'{op}: {a} → {b}' for op, a, b in zip(OPS, prev.proc, r.proc) if a != '' and b != '' and a != b] if comparable else []
            changes = len(diffs) if comparable else None
            cur = Run(run_no=r.run_no, line=r.line, seq=seq, from_pf=prev.to_pf if prev else '', to_pf=r.pf, buyer_group=r.buyer_group, style=r.style,
                      status=r.status, proc=r.proc, changes=changes, what='' if changes is None else 'No change' if changes == 0 else '; '.join(diffs),
                      comparable=comparable if seq > 1 else None, first_row=r.seq, last_row=r.seq)
            runs.append(cur)
            last[r.line] = cur
        if cur is None:
            continue
        cur.order_qty += r.order_qty
        cur.plan_qty += r.plan_qty
        cur.po_lines += 1
        cur.last_row = r.seq
        if r.sew_in is not None:
            cur.sew_start = r.sew_in if cur.sew_start is None else min(cur.sew_start, r.sew_in)
        if r.sew_end is not None:
            cur.sew_end = r.sew_end if cur.sew_end is None else max(cur.sew_end, r.sew_end)
    for i, r in enumerate(sorted((r for r in runs if (r.changes or 0) > 0), key=lambda r: (-r.changes, r.run_no)), 1):
        r.rank = i
    return reclassify(runs, s)


# ---------- Kanban ----------
@dataclass
class Card:
    key: str
    run: Run
    stage: str
    priority: str
    locked: bool
    owner: str = ''
    notes: str = ''


def card_key(r: Run) -> str:
    return f'{r.line}|{r.to_pf}|{ymd(r.sew_start)}'


def is_blocked(r: Run, s: Settings) -> bool:
    return r.status in s.blocked_statuses


def auto_stage(r: Run, s: Settings) -> str:
    if is_blocked(r, s):
        return 'Data Blocked'
    if r.sew_start is not None and r.sew_start <= s.as_of:
        return 'Running'
    if s.ready_window_days > 0 and r.sew_start is not None and r.sew_start <= s.as_of + s.ready_window_days:
        return 'Ready'
    return 'Planned'


def default_priority(r: Run, s: Settings) -> str:
    if is_blocked(r, s):
        return 'High' if r.sew_start is not None and r.sew_start <= s.as_of + s.urgent_window_days else 'Medium'
    return 'High' if r.type == 'High variation' else 'Medium' if r.type == 'Process change' else 'Low'


def sync_cards(runs: List[Run], overlay: Dict[str, dict], s: Optional[Settings] = None):
    """Run-derived fields refresh, manual fields survive, locked cards keep their stage."""
    s = s or Settings()
    keys, cards = set(), []
    for run in runs:
        key = card_key(run)
        keys.add(key)
        o = overlay.get(key)
        locked = bool(o and o.get('locked'))
        cards.append(Card(key=key, run=run, stage=o['stage'] if locked else auto_stage(run, s),
                          priority=o['priority'] if o and o.get('priority') else default_priority(run, s), locked=locked,
                          owner=(o or {}).get('owner') or '', notes=(o or {}).get('notes') or ''))
    return cards, [k for k in overlay if k not in keys]


def summarise(cards: List[Card]) -> dict:
    stages = {st: {'cards': 0, 'pcs': 0} for st in STAGES}
    lines: Dict[str, dict] = {}
    high = 0
    for c in cards:
        stages[c.stage]['cards'] += 1
        stages[c.stage]['pcs'] += c.run.plan_qty
        l = lines.setdefault(c.run.line, {'runs': 0, 'pcs': 0, 'running': 0, 'blocked': 0, 'high': 0, 'done': 0})
        l['runs'] += 1
        l['pcs'] += c.run.plan_qty
        l['running'] += c.stage == 'Running'
        l['blocked'] += c.stage == 'Data Blocked'
        l['done'] += c.stage == 'Done'
        if c.priority == 'High':
            l['high'] += 1
            high += 1
    return {'total_runs': len(cards), 'plan_qty': sum(c.run.plan_qty for c in cards), 'high': high, 'stages': stages, 'lines': lines}


# ---------- data-health checks ----------
def variant_counts(master: dict) -> Dict[str, int]:
    c: Dict[str, int] = {}
    for m in master['rows']:
        for op, v in zip(OPS, m['values']):
            if v:
                c[f'{op}|{v}'] = c.get(f'{op}|{v}', 0) + 1
    return c


def health_checks(plan: dict, master: dict, variants: Optional[dict] = None) -> List[dict]:
    variants = variants or DEFAULT_VARIANTS
    f = lambda n: f'{n:,}'  # noqa: E731
    rows = plan['rows']
    clean = sum(r.plan_qty for r in rows)
    no_pf = [r for r in rows if not isinstance(r.pf, int)]
    missing: Dict[int, int] = {}
    for r in rows:
        if r.status == 'Not in master':
            missing[r.pf] = missing.get(r.pf, 0) + r.plan_qty
    bad_variant = sum(1 for m in master['rows'] for op, v in zip(OPS, m['values']) if v != '' and v not in variants.get(op, []))
    bpo = sum(1 for r in rows if r.date_check == 'BPO < Sew End')
    gt = plan['grand_total']
    return [
        {'id': 1, 'label': 'Plan columns recognised', 'result': f"{plan['cols']['recognised']} / {plan['cols']['total']}", 'status': 'ACTION' if plan['cols']['missing'] else 'OK'},
        {'id': 2, 'label': 'Master columns recognised', 'result': f"{master['cols']['recognised']} / {master['cols']['total']}", 'status': 'ACTION' if master['cols']['missing'] else 'OK'},
        {'id': 4, 'label': 'Subtotal / blank rows removed', 'result': f"{plan['dropped']} of {plan['raw_count']} rows", 'status': 'INFO'},
        {'id': 5, 'label': 'Cleaned Plan Qty vs plan-file grand total', 'result': f"{f(clean)} vs {'n/a' if gt is None else f(gt)}", 'status': 'INFO' if gt is None else 'OK' if gt == clean else 'REVIEW'},
        {'id': 6, 'label': 'PF# values normalised (text → number)', 'result': f"{sum(1 for r in rows if r.pf_text_to_number)} rows", 'status': 'INFO'},
        {'id': 7, 'label': 'Plan rows with no usable PF#', 'result': f"{len(no_pf)} rows · {f(sum(r.plan_qty for r in no_pf))} pcs", 'status': 'ACTION' if no_pf else 'OK'},
        {'id': 8, 'label': 'PFs in plan not found in master', 'result': f"{len(missing)} PFs · {f(sum(missing.values()))} pcs", 'status': 'ACTION' if missing else 'OK'},
        {'id': 9, 'label': 'PFs in master with blank process fields', 'result': f"{master['incomplete']} PFs", 'status': 'REVIEW' if master['incomplete'] else 'OK'},
        {'id': 10, 'label': 'Duplicate PFs in master', 'result': f"{master['duplicates']} PFs", 'status': 'ACTION' if master['duplicates'] else 'OK'},
        {'id': 11, 'label': 'Plan lines where BPO date precedes Sew End', 'result': f'{bpo} lines', 'status': 'REVIEW' if bpo else 'OK'},
        {'id': 12, 'label': 'Master values missing from the variant list', 'result': f'{bad_variant} values', 'status': 'REVIEW' if bad_variant else 'OK'},
    ]


def reports(plan: dict, runs: List[Run], master: dict, as_of: int) -> dict:
    missing: Dict[int, dict] = {}
    for r in plan['rows']:
        if r.status == 'Not in master':
            m = missing.setdefault(r.pf, {'pf': r.pf, 'pcs': 0, 'lines': [], 'buyer': r.buyer_group, 'first': r.sew_in if r.sew_in is not None else 10 ** 9})
            m['pcs'] += r.plan_qty
            if r.line not in m['lines']:
                m['lines'].append(r.line)
            m['first'] = min(m['first'], r.sew_in if r.sew_in is not None else 10 ** 9)
    mix: Dict[str, Dict[str, int]] = {}
    for r in runs:
        mix.setdefault(r.line, {})
        mix[r.line][r.type] = mix[r.line].get(r.type, 0) + 1
    month: Dict[int, Dict[str, float]] = {}
    for r in plan['rows']:
        if r.sewing_month is not None:
            b = month.setdefault(r.sewing_month, {})
            b[r.buyer_group] = b.get(r.buyer_group, 0) + r.plan_qty
    return {
        'missing': [dict(m, lines=', '.join(m['lines'])) for m in sorted(missing.values(), key=lambda m: -m['pcs'])], 'mix': mix,
        'by_month': [{'month': k, 'buyers': b, 'pcs': sum(b.values())} for k, b in sorted(month.items())],
        'conflicts': [r for r in plan['rows'] if r.date_check], 'variants': variant_counts(master),
        'ranking': sorted((r for r in runs if r.rank is not None), key=lambda r: r.rank),
        'upcoming': sorted((r for r in runs if r.sew_start is not None and as_of < r.sew_start <= as_of + 7), key=lambda r: r.sew_start),
    }


# ---------- Line-Wise tracking ----------
def _round(x: float) -> int:
    return int(math.floor(x + 0.5))


def max_plan_target(manpower, smv, eff, hours):
    if manpower is None or not smv or eff is None:
        return None
    return (manpower * hours * 60 / smv) * eff


def today_target(area: str, M, E) -> int:
    """Any missing or zero input gives 0 (the sheet wraps everything in IFERROR(..., 0))."""
    if E is None or E == 0 or M is None:
        return 0
    if area in ('SELVEDGE', '2ND OUTPUT'):
        return _round(M / E * 0.6)
    if area in ('1ST OUTPUT', 'LAST OUTPUT'):
        return _round(M / E * 0.4)
    if area == 'OUTPUT':
        return _round(M)
    return 0


def line_wise_row(e: dict, runs: List[Run], as_of: int, hours: float, aging_days: float) -> dict:
    pf = e.get('pf')
    has_pf = pf not in (None, '')
    same = [r for r in runs if has_pf and r.line == e.get('line') and str(r.to_pf) == str(pf)]
    run = next((r for r in same if (r.sew_end or 0) >= as_of), same[-1] if same else None)
    M = max_plan_target(e.get('manpower'), e.get('smv'), e.get('eff'), hours)
    plan_date = run.sew_start if run else None
    plan_qty = run.plan_qty if run else None
    days = max(0, as_of - plan_date) if has_pf and plan_date is not None else None
    actual = e.get('actual_input')
    status = 'Vacant – No Style' if not has_pf else 'Running – Confirmed' if actual is not None else 'Pending Confirmation'
    t_in, t_out = e.get('total_input'), e.get('total_output')
    return {
        'run': run, 'status': status, 'plan_date': plan_date, 'plan_qty': plan_qty, 'order_qty': run.order_qty if run else None,
        'runs_of_pf': len(same), 'days_since_plan': days, 'days_running': max(0, as_of - actual) if actual is not None else None,
        'aging': status == 'Pending Confirmation' and days is not None and days >= aging_days,
        'max_target': M, 'today_target': today_target(e.get('area') or '', M, e.get('eff')),
        'wip': (t_in or 0) - (t_out or 0) if has_pf else None,
        'completion': t_out / plan_qty if plan_qty and plan_qty > 0 and t_out is not None else None,
        'balance': plan_qty - (t_out or 0) if plan_qty is not None else None,
    }


def line_wise_totals(derived: List[dict], inputs: List[dict]) -> dict:
    """Totals sit under their own columns (the sheet's totals row is shifted by one column)."""
    s = lambda f: sum((f(i) or 0) for i in range(len(derived)))  # noqa: E731
    planned = [i for i, d in enumerate(derived) if (d['plan_qty'] or 0) > 0]
    plan = sum(derived[i]['plan_qty'] for i in planned)
    out = sum((inputs[i].get('total_output') or 0) for i in planned)
    return {'order': s(lambda i: derived[i]['order_qty']), 'plan': s(lambda i: derived[i]['plan_qty']), 'today': s(lambda i: derived[i]['today_target']),
            'input': s(lambda i: inputs[i].get('total_input')), 'output': s(lambda i: inputs[i].get('total_output')),
            'wip': s(lambda i: derived[i]['wip']), 'balance': s(lambda i: derived[i]['balance']), 'ready': s(lambda i: inputs[i].get('ready')),
            'completion': out / plan if plan > 0 else None}


# ---------- Kanban manual changes ----------
def _nn(v, default):
    return default if v is None else v


def move_card(overlay: Dict[str, dict], card: Card, stage: str, patch: Optional[dict] = None) -> Dict[str, dict]:
    """A manual move locks the card in its new stage; priority, owner and notes may change in the same move."""
    p = patch or {}
    nxt = dict(overlay)
    nxt[card.key] = {'stage': stage, 'priority': _nn(p.get('priority'), card.priority), 'owner': _nn(p.get('owner'), card.owner),
                     'notes': _nn(p.get('notes'), card.notes), 'locked': True}
    return nxt


def unlock_card(overlay: Dict[str, dict], card: Card) -> Dict[str, dict]:
    """'Unlock (return to auto)': the stage is derived again on the next sync; priority, owner and notes stay."""
    nxt = dict(overlay)
    nxt[card.key] = {'stage': card.stage, 'priority': card.priority, 'owner': card.owner, 'notes': card.notes, 'locked': False}
    return nxt
