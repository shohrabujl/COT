"""Builds a small planning workbook with the same sheets as UPDATE_PLAN_MASTER_ANALYSIS.xlsx, for tests.
Run  python tests/sample.py out.xlsx  to get a practice file you can import into the app."""
import datetime as dt
import os
import sys

import openpyxl

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from ujl import engine as E  # noqa: E402

AS_OF = (dt.date(2026, 11, 3) - E.EPOCH).days


def d(day):
    return dt.date(2026, 11, day)


def make(path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Update Plan'
    ws.append(E.PLAN_HEADERS)

    def row(line, buyer, pf, style, qty, cut, sew_in, sew_end, bpo, po):
        ws.append([line, buyer, pf, style, 'Denim trouser', None, po, 'BLUE', 'JP', qty, qty, d(cut), d(cut + 1), d(sew_in), d(sew_in + 1), d(sew_end), d(bpo)])

    uq, zara = 'UQ::UNIQLO', 'ITX::ZARA MAN'
    row('UJL-01', uq, 1001, 'S-100', 1000, 1, 2, 8, 12, 'PO1')
    row('UJL-01', uq, 1001, 'S-100', 500, 2, 4, 9, 13, 'PO2')
    row('UJL-01', uq, 'RF 1003', 'S-103', 800, 6, 10, 14, 18, 'PO3')          # text PF -> 1003; 1 process differs from PF 1001
    row('UJL-01', uq, 1002, 'S-102', 700, 11, 15, 19, 18, 'PO4')              # all 7 differ from 1003; BPO before Sew End
    ws.append(['UJL-01 Total'] + [None] * 9 + [3000])                          # subtotal row, dropped
    ws.append([None] * 17)                                                     # blank row
    row('UJL-02', zara, 1001, 'Z-100', 600, 1, 2, 6, 10, 'PO5')
    row('UJL-02', zara, 1005, 'Z-105', 400, 3, 7, 10, 12, 'PO6')              # PF 1005 is not in the master
    row('UJL-02', zara, 1004, 'Z-104', 300, 8, 11, 13, 15, 'PO7')             # PF 1004 has a blank process field
    ws.append(['Grand Total'] + [None] * 9 + [4300])

    pm = wb.create_sheet('PROCESS DATA')
    pm.append(['PROCESS DATA'])
    pm.append([])
    pm.append(['BUYER', 'PF'] + E.OPS + ['REMARKS'])
    pm.append(['UQ::UNIQLO', 1001, 'I/L', 'I/L', 'I/L', 'SNCS', 'S/F', 'SETTER', 'VBM', None])
    pm.append(['UQ::UNIQLO', 1002, 'FOA', 'FOA', 'FOA', 'I/L', 'D/F', 'B/T', 'I/L', None])
    pm.append(['UQ::UNIQLO', 1003, 'I/L', 'I/L', 'I/L', 'SNCS', 'S/F', 'SETTER', 'LOCK STC', None])
    pm.append(['ITX::ZARA MAN', 1004, 'I/L', 'I/L', 'I/L', 'SNCS', 'S/F', 'SETTER', None, 'check hem'])

    kd = wb.create_sheet('Kanban_Data')
    kd.append(['Key', 'Stage', 'Priority', 'Owner', 'Notes', 'Locked'])
    kd.append(['UJL-01|1003|20261110', 'Ready', 'High', 'Ali', 'fabric in', True])      # moved by hand
    kd.append(['UJL-01|1001|20261102', 'Running', 'Medium', None, None, False])         # only the priority was changed
    kd.append(['UJL-02|1001|20261102', 'Running', 'Low', None, None, False])            # same as automatic: nothing to save

    lw = wb.create_sheet('Line-Wise Data')
    lw.append(['LINE-WISE'])
    lw.append([])
    lw.append([None, None, d(3), None, None, None, None, 8, None, None, None, None, 3])
    lw.append([])
    lw.append(['Line No', 'PF', 'Manpower', 'SMV', 'Eff', 'Plan date', 'Actual input', 'Status', 'Days', 'Order', 'Plan', 'Area', 'Max', 'Today',
               'Input', 'Output', 'WIP', 'Completion', 'Balance', 'Ready', 'Remarks'])
    lw.append(['UJL-01', 1001, 40, 20, 0.85, None, d(2), None, None, None, None, 'OUTPUT', None, None, 800, 600, None, None, None, 120, 'ok'])
    lw.append(['UJL-02', None, None, None, 0.85, None, None, None, None, None, None, 'OUTPUT', None, None, None, None, None, None, None, None, None])
    wb.save(path)


if __name__ == '__main__':
    make(sys.argv[1] if len(sys.argv) > 1 else 'sample_plan.xlsx')
    print('written')
