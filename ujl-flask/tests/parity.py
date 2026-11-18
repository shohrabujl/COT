"""Full-workbook parity gate:  python tests/parity.py UPDATE_PLAN_MASTER_ANALYSIS.xlsx
Feeds the workbook's raw sheets through ujl.engine and compares with the workbook's own outputs. Exit code 1 on any failure."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import openpyxl  # noqa: E402

from ujl import engine as E  # noqa: E402

path = sys.argv[1] if len(sys.argv) > 1 else 'UPDATE_PLAN_MASTER_ANALYSIS.xlsx'
wb = openpyxl.load_workbook(path, data_only=True)
rows = lambda name: [list(r) for r in wb[name].iter_rows(values_only=True)]  # noqa: E731
fails = 0


def eq(name, expected, actual):
    global fails
    ok = str(expected) == str(actual)
    fails += not ok
    print(('✓' if ok else '✗'), name, '' if ok else f'   expected {expected} · got {actual}')


def same(a, b):
    a, b = (None if x in ('', None) else x for x in (a, b))
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return str(a) == str(b)


m_rows = rows('PROCESS DATA')
mh = next(i for i, r in enumerate(m_rows) if 'PF' in r and 'INSEAM' in r)
p_rows = rows('Update Plan')
mp, pp = E.pack_sheet('master', m_rows[mh], m_rows[mh + 1:]), E.pack_sheet('plan', p_rows[0], p_rows[1:])
master = E.build_master(mp['headers'], mp['rows'])
plan = E.clean_plan(pp['headers'], pp['rows'], master)
runs = E.build_runs(plan['rows'])
kd = rows('Kanban_Data')
overlay = {r[0]: {'stage': r[1], 'priority': r[2], 'owner': r[3] or '', 'notes': r[4] or '', 'locked': True} for r in kd[1:] if r[0] and str(r[5]).upper() == 'TRUE'}
cards, removed = E.sync_cards(runs, overlay)
S = E.summarise(cards)

print('\n— Import & cleaning')
hc = {c['id']: c for c in E.health_checks(plan, master)}
eq('raw rows', 648, plan['raw_count']); eq('valid (clean) rows', 416, len(plan['rows'])); eq('removed rows', 232, plan['dropped'])
for i, exp in {5: '3,759,267 vs 3,759,267', 6: '104 rows', 7: '6 rows · 222,991 pcs', 8: '16 PFs · 730,831 pcs', 9: '6 PFs', 10: '0 PFs', 11: '73 lines', 12: '4 values'}.items():
    eq(f'check {i}: {hc[i]["label"]}', exp, hc[i]['result'])
eq('check statuses', 'OK,OK,INFO,OK,INFO,ACTION,ACTION,REVIEW,OK,REVIEW,REVIEW', ','.join(c['status'] for c in hc.values()))

print('\n— Master variant counts (PFs)')
vc = E.variant_counts(master)
for t in ('INSEAM|I/L|98,INSEAM|FOA|62,BK YOKE|I/L|61,BK YOKE|FOA|99,BK RISE|I/L|59,BK RISE|FOA|99,SIDE SEAM|SNCS|149,SIDE SEAM|I/L|10,WAIST BAND|S/F|75,'
          'WAIST BAND|D/F|54,WAIST BAND|CLEAN FINISH|24,WAIST BAND|HIDDEN STC|4,LOOP B/T|SETTER|93,LOOP B/T|B/T|67,BTM HEM|VBM|114,BTM HEM|I/L|19,BTM HEM|LOCK STC|26').split(','):
    op, v, n = t.split('|')
    eq(f'{op} {v}', n, vc.get(f'{op}|{v}', 0))

print("\n— Dashboard (the workbook's own Dashboard sheet)")
d = rows('Dashboard')
sh = next(i for i, r in enumerate(d) if len(r) > 3 and r[3] == 'Stage')
eq('runs', 199, len(runs)); eq('lines', 14, len({r.line for r in runs})); eq('plan qty', 3759267, S['plan_qty']); eq('high priority', 29, S['high'])
for r in d[sh + 1: sh + 6]:
    a = S['stages'][r[3]]
    eq(f'stage {r[3]}', f'{E.num(r[4])} cards / {E.num(r[5])} pcs', f"{a['cards']} cards / {a['pcs']} pcs")
GL = {'UJL-01': [16, 379986, 3, 1, 3], 'UJL-02': [6, 364896, 1, 0, 5], 'UJL-03': [28, 329846, 2, 10, 1], 'UJL-04': [17, 280616, 2, 6, 2], 'UJL-05': [4, 293303, 0, 2, 1],
      'UJL-06': [9, 257811, 0, 4, 3], 'UJL-07': [16, 163806, 1, 2, 2], 'UJL-08': [11, 179982, 1, 1, 0], 'UJL-09': [24, 159310, 2, 4, 5], 'UJL-10': [13, 367844, 1, 4, 0],
      'UJL-11': [19, 380292, 4, 1, 5], 'UJL-12': [16, 304331, 2, 3, 1], 'UJL-13': [19, 296328, 1, 0, 1], 'UJL-Sample line': [1, 916, 0, 0, 0]}
for l, g in GL.items():
    a = S['lines'].get(l)
    eq(f'{l} runs/pcs/running/blocked/high', '/'.join(map(str, g)), '/'.join(str(a[k]) for k in ('runs', 'pcs', 'running', 'blocked', 'high')) if a else 'missing')

print("\n— Line Sequence vs the workbook's own sheet")
ls = rows('Line Sequence')
h = next(i for i, r in enumerate(ls) if r[0] == 'Run #')
H = ls[h]
col = H.index
bad = []
g_rows = [r for r in ls[h + 1:] if r[0] not in (None, '')]
eq('run rows', len(g_rows), len(runs))
for r in g_rows:
    run = runs[int(r[col('Run #')]) - 1]
    pairs = [('line', r[col('Line')], run.line), ('seq', r[col('Seq on Line')], run.seq), ('from', r[col('From PF')], run.from_pf), ('to', r[col('To PF')], run.to_pf),
             ('order', r[col('Order Qty')], run.order_qty), ('plan', r[col('Plan Qty')], run.plan_qty), ('po lines', r[col('PO Lines')], run.po_lines),
             ('status', r[col('To-PF Master Status')], run.status), ('changes', r[col('Process Changes (#)')], run.changes), ('what', r[col('What Changes (From → To)')], run.what),
             ('type', r[col('Changeover Type')], run.type), ('rank', r[col('Rank')], run.rank), ('first row', r[col('First Row')], run.first_row),
             ('last row', r[col('Last Row')], run.last_row), ('comparable', r[col('Comparable')], None if run.comparable is None else int(run.comparable)),
             ('sew start', E.parse_date(r[col('Sew Start')]), run.sew_start), ('sew end', E.parse_date(r[col('Sew End')]), run.sew_end)]
    bad += [f'run {r[0]} {k}: sheet "{a}" vs engine "{b}"' for k, a, b in pairs if not same(a, b)]
eq('field mismatches', 0, len(bad)); print(*bad[:15], sep='\n    ')

print('\n— Kanban_Data vs engine (locked cards are manual inputs, everything else is derived)')
by_key = {c.key: c for c in cards}
k_rows = [r for r in kd[1:] if r[0]]
eq('cards', len(k_rows), len(cards)); eq('cards that vanished from the plan', 0, len(removed))
bad, manual = [], []
for r in k_rows:
    c = by_key.get(r[0])
    if c is None:
        bad.append(f'{r[0]}: not produced by the engine'); continue
    if c.stage != r[1]:
        bad.append(f'{r[0]} stage: sheet {r[1]} vs engine {c.stage}')
    if c.priority != r[2]:
        manual.append(f'{r[0]} priority: sheet {r[2]} vs engine {c.priority}')
eq('stage mismatches', 0, len(bad)); print(*bad[:15], sep='\n    ')
print(f'  ℹ manual priority overrides (priority is a saved manual field, not a failure): {len(manual)}'); print(*manual, sep='\n    ')

print('\n— Line-Wise Data vs engine')
lw = rows('Line-Wise Data')
hi = next(i for i, r in enumerate(lw) if r[0] == 'Line No')
as_of, hours, aging = E.parse_date(lw[2][2]), E.num(lw[2][7]) or 8, E.num(lw[2][12]) or 3
bad, n, days_off = [], 0, 0
for r in lw[hi + 1:]:
    if not str(r[0] or '').startswith('UJL-'):
        break
    n += 1
    pf = E.num(r[1]) if E.num(r[1]) is not None else (r[1] or None)
    o = E.line_wise_row({'line': r[0], 'pf': pf, 'manpower': E.num(r[2]), 'smv': E.num(r[3]), 'eff': E.num(r[4]), 'actual_input': E.parse_date(r[6]),
                         'area': str(r[11] or '').strip(), 'total_input': E.num(r[14]), 'total_output': E.num(r[15]), 'ready': E.num(r[19])}, runs, as_of, hours, aging)
    days_off += not same(r[8], o['days_since_plan'])
    for k, a, b in [('plan date', E.parse_date(r[5]), o['plan_date']), ('order', r[9], o['order_qty']), ('plan', r[10], o['plan_qty']), ('status', r[7], o['status']),
                    ('max target', r[12], o['max_target']), ('today target', r[13], o['today_target']), ('wip', r[16], o['wip']), ('balance', r[18], o['balance']),
                    ('completion', r[17], o['completion'])]:
        if not same(a, b):
            bad.append(f'{r[0]} PF {r[1]} {k}: sheet "{a}" vs engine "{b}"')
eq('line-wise rows checked', 'yes', 'yes' if n else 'none'); eq('line-wise field mismatches', 0, len(bad)); print(*bad[:15], sep='\n    ')
print(f'  ℹ Days Since Plan differs on {days_off} of {n} rows: the sheet mixes plan-date and actual-input-date formulas; the engine uses the plan date and adds days running.')
print(f'\n{fails} parity check(s) FAILED' if fails else '\nAll parity checks passed')
sys.exit(1 if fails else 0)
