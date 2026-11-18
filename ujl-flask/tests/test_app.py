"""Tests for the Flask app.  Run:  python -m unittest discover -s tests -v   (uses a temporary database; your real data is never touched)."""
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import manage  # noqa: E402
import openpyxl  # noqa: E402

from tests import sample  # noqa: E402
from ujl import auth, create_app  # noqa: E402
from ujl import data as DT  # noqa: E402
from ujl import db as D  # noqa: E402
from ujl import engine as E  # noqa: E402
from ujl import imports  # noqa: E402

PW = 'correct-horse-9'
K_PLANNED = 'UJL-01|1002|20261115'        # automatic, Planned
K_LOCKED = 'UJL-01|1003|20261110'         # saved as a manually moved card (Ready) in the sample workbook
K_BLOCKED = 'UJL-02|1004|20261111'        # PF 1004 has a blank process field


class AppCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.app = create_app(self.tmp)
        self.c = self.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ----
    def tok(self, c=None):
        with (c or self.c).session_transaction() as s:
            if 'csrf' not in s:
                s['csrf'] = 'test-token-' + os.urandom(6).hex()
            return s['csrf']

    def post(self, url, data=None, c=None, **kw):
        c = c or self.c
        return c.post(url, data=dict(data or {}, csrf_token=self.tok(c)), **kw)

    def jpost(self, url, body, c=None):
        c = c or self.c
        return c.post(url, json=body, headers={'X-CSRF-Token': self.tok(c)})

    def login(self, username, pw=PW, c=None):
        return self.post('/login', {'username': username, 'password': pw}, c=c)

    def setup_admin(self, username='boss'):
        r = self.post('/setup', {'name': 'Boss Person', 'username': username, 'password': PW, 'password2': PW})
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:400])

    def user_client(self, username, role):
        with self.app.app_context():
            with D.tx() as con:
                auth.make_user(con, username, username.title(), PW, role, 'active')
        c = self.app.test_client()
        self.assertEqual(self.login(username, c=c).status_code, 302)
        return c

    def uid(self, username):
        with self.app.app_context():
            return D.get_db().execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()['id']

    def load_sample(self, fixed_asof=True):
        path = os.path.join(self.tmp, 'sample.xlsx')
        sample.make(path)
        with self.app.app_context():
            with D.tx() as con:
                imports.seed_from_path(con, None, path)
                if fixed_asof:
                    st = D.get_settings(con)
                    st.update(as_of_mode='fixed', as_of_fixed=sample.AS_OF)
                    D.save_settings(con, st)
        return path

    def state(self):
        with self.app.app_context():
            return DT.load_state(D.get_db())

    def card(self, key):
        return next(c for c in self.state()['cards'] if c.key == key)

    def audit_actions(self):
        with self.app.app_context():
            return [r['action'] for r in D.get_db().execute('SELECT action FROM audit ORDER BY id')]

    def text(self, r):
        return r.get_data(as_text=True)


class Accounts(AppCase):
    def test_first_run_setup_makes_the_super_user_and_closes(self):
        r = self.c.get('/')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/setup', r.headers['Location'])
        bad = self.post('/setup', {'name': 'Boss Person', 'username': 'boss', 'password': PW, 'password2': 'different-1'})
        self.assertIn('do not match', self.text(bad))
        weak = self.post('/setup', {'name': 'Boss Person', 'username': 'boss', 'password': 'short', 'password2': 'short'})
        self.assertIn('at least 8', self.text(weak))
        self.setup_admin()
        self.assertEqual(self.c.get('/dashboard').status_code, 200)
        r = self.c.get('/setup')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login', r.headers['Location'])
        with self.app.app_context():
            u = D.get_db().execute('SELECT * FROM users').fetchone()
        self.assertEqual((u['role'], u['status']), (E.SUPER, 'active'))
        self.assertNotIn(PW, u['pw_hash'])                       # only a hash is stored

    def test_register_wait_approve_then_sign_in(self):
        self.setup_admin()
        anon = self.app.test_client()
        r = self.post('/register', {'name': 'Sara Khan', 'username': 'sara', 'password': PW, 'password2': PW}, c=anon)
        self.assertEqual(r.status_code, 302)
        self.assertIn('waiting for approval', self.text(self.login('sara', c=anon)))
        r = self.post(f"/admin/users/{self.uid('sara')}", {'do': 'approve', 'role': 'Viewer'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.login('sara', c=anon).status_code, 302)
        self.assertEqual(anon.get('/dashboard').status_code, 200)
        self.assertEqual(anon.get('/admin/users').status_code, 403)
        self.assertIn('user.approve', self.audit_actions())

    def test_taken_username_and_weak_password_are_refused(self):
        self.setup_admin()
        anon = self.app.test_client()
        self.assertIn('already taken', self.text(self.post('/register', {'name': 'Other Boss', 'username': 'BOSS', 'password': PW, 'password2': PW}, c=anon)))
        self.assertIn('at least 8', self.text(self.post('/register', {'name': 'Tom Lee', 'username': 'tom', 'password': 'abc', 'password2': 'abc'}, c=anon)))
        self.assertIn('Username:', self.text(self.post('/register', {'name': 'Tom Lee', 'username': 'a b', 'password': PW, 'password2': PW}, c=anon)))

    def test_lockout_after_five_wrong_passwords(self):
        self.setup_admin()
        anon = self.app.test_client()
        for _ in range(5):
            self.assertIn('Wrong username or password', self.text(self.login('boss', 'wrong-password', c=anon)))
        self.assertIn('Too many failed attempts', self.text(self.login('boss', PW, c=anon)))

    def test_rejected_and_disabled_accounts_cannot_sign_in(self):
        self.setup_admin()
        anon = self.app.test_client()
        self.post('/register', {'name': 'Rob Hill', 'username': 'rob', 'password': PW, 'password2': PW}, c=anon)
        self.post(f"/admin/users/{self.uid('rob')}", {'do': 'reject'})
        self.assertIn('not active', self.text(self.login('rob', c=anon)))
        viewer = self.user_client('vic', 'Viewer')
        self.post(f"/admin/users/{self.uid('vic')}", {'do': 'save', 'role': 'Viewer', 'status': 'disabled'})
        self.assertEqual(viewer.get('/dashboard').status_code, 302)          # an open session ends at once
        self.assertIn('not active', self.text(self.login('vic', c=self.app.test_client())))

    def test_post_without_a_valid_token_is_refused(self):
        self.setup_admin()
        self.assertEqual(self.c.post('/logout').status_code, 400)
        self.assertEqual(self.c.post('/logout', data={'csrf_token': 'nope'}).status_code, 400)
        r = self.c.post('/api/card/move', json={'key': 'x', 'stage': 'Ready'})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()['ok'])

    def test_pages_need_sign_in_and_return_to_the_page_after(self):
        self.setup_admin()
        anon = self.app.test_client()
        r = anon.get('/kanban?line=UJL-01')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login?next=', r.headers['Location'])
        r = self.post(r.headers['Location'], {'username': 'boss', 'password': PW}, c=anon)
        self.assertEqual(r.status_code, 302)
        self.assertIn('/kanban', r.headers['Location'])
        evil = self.post('/login?next=//evil.example/x', {'username': 'boss', 'password': PW}, c=self.app.test_client())
        self.assertNotIn('evil.example', evil.headers['Location'])

    def test_only_local_host_names_are_served(self):
        self.assertEqual(self.c.get('/login', headers={'Host': 'evil.example'}).status_code, 403)
        self.assertEqual(self.c.get('/login', headers={'Host': '127.0.0.1:5000'}).status_code, 302)       # no users yet -> setup

    def test_security_headers(self):
        self.setup_admin()
        h = self.c.get('/dashboard').headers
        self.assertIn("default-src 'self'", h['Content-Security-Policy'])
        self.assertEqual(h['X-Frame-Options'], 'DENY')
        self.assertEqual(h['Cache-Control'], 'no-store')

    def test_password_change_ends_other_sessions(self):
        self.setup_admin()
        other = self.app.test_client()
        self.login('boss', c=other)
        self.assertEqual(other.get('/dashboard').status_code, 200)
        self.assertIn('wrong', self.text(self.post('/account', {'current': 'nope-nope-1', 'new': 'another-pass-77', 'new2': 'another-pass-77'})))
        self.assertEqual(self.post('/account', {'current': PW, 'new': 'another-pass-77', 'new2': 'another-pass-77'}).status_code, 302)
        self.assertEqual(other.get('/dashboard').status_code, 302)
        self.assertEqual(self.c.get('/dashboard').status_code, 200)
        self.assertEqual(self.login('boss', PW, c=self.app.test_client()).status_code, 200)      # the old password is refused

    def test_added_user_must_choose_a_new_password(self):
        self.setup_admin()
        r = self.post('/admin/users/add', {'name': 'Neo Planner', 'username': 'neo', 'password': 'temp-pass-1', 'role': 'Planner'})
        self.assertEqual(r.status_code, 302)
        c = self.app.test_client()
        self.login('neo', 'temp-pass-1', c)
        r = c.get('/dashboard')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/account', r.headers['Location'])
        self.assertEqual(self.post('/account', {'current': 'temp-pass-1', 'new': 'neo-new-pass-9', 'new2': 'neo-new-pass-9'}, c=c).status_code, 302)
        self.assertEqual(c.get('/dashboard').status_code, 200)
        self.post(f"/admin/users/{self.uid('neo')}", {'do': 'reset', 'password': 'reset-pass-5'})
        self.assertEqual(c.get('/dashboard').status_code, 302)                      # old session ended
        c2 = self.app.test_client()
        self.login('neo', 'reset-pass-5', c2)
        self.assertIn('/account', c2.get('/dashboard').headers['Location'])
        self.assertIn('user.reset_password', self.audit_actions())

    def test_console_password_reset_for_a_forgotten_password(self):
        self.setup_admin()
        anon = self.app.test_client()
        for _ in range(5):
            self.login('boss', 'wrong-password', c=anon)
        self.assertIn('Too many', self.text(self.login('boss', PW, c=anon)))
        self.assertIsNone(manage.reset_password(self.app, 'boss', 'brand-new-pw-1'))
        self.assertEqual(self.login('boss', 'brand-new-pw-1', c=anon).status_code, 302)          # the lock-out is cleared too
        self.assertEqual(manage.reset_password(self.app, 'nobody', 'brand-new-pw-1'), 'There is no user with that username.')
        self.assertIn('at least 8', manage.reset_password(self.app, 'boss', 'short'))
        self.assertIn('user.reset_password', self.audit_actions())

    def test_there_is_always_one_active_super_user(self):
        self.setup_admin()
        boss = self.uid('boss')
        for form in ({'do': 'save', 'role': E.SUPER, 'status': 'disabled'}, {'do': 'save', 'role': 'Viewer', 'status': 'active'}, {'do': 'delete'}):
            r = self.post(f'/admin/users/{boss}', form, follow_redirects=True)
            self.assertRegex(self.text(r), 'always be one active Super User|Disable the user first')
        with self.app.app_context():
            u = D.get_db().execute('SELECT role,status FROM users WHERE id=?', (boss,)).fetchone()
        self.assertEqual((u['role'], u['status']), (E.SUPER, 'active'))

    def test_only_a_super_user_changes_super_users(self):
        self.setup_admin()
        with self.app.app_context():
            with D.tx() as con:
                con.execute("INSERT INTO roles(name, perms, sort) VALUES('HR', ?, 9)", (json.dumps(['dashboard.view', 'users.manage']),))
        hr = self.user_client('hrlee', 'HR')
        self.user_client('vic', 'Viewer')
        r = self.post(f"/admin/users/{self.uid('boss')}", {'do': 'save', 'role': 'Viewer', 'status': 'active'}, c=hr, follow_redirects=True)
        self.assertIn('Only a Super User can change a Super User', self.text(r))
        r = self.post(f"/admin/users/{self.uid('vic')}", {'do': 'save', 'role': E.SUPER, 'status': 'active'}, c=hr, follow_redirects=True)
        self.assertIn('Only a Super User can give the Super User role', self.text(r))
        self.assertEqual(hr.get('/admin/permissions').status_code, 403)
        self.assertEqual(self.post('/admin/permissions', {'do': 'reset'}, c=hr).status_code, 403)
        self.assertEqual(hr.get('/admin/users').status_code, 200)
        self.assertEqual(hr.get('/admin/audit').status_code, 403)

    def test_rejected_request_can_be_removed_and_name_reused(self):
        self.setup_admin()
        anon = self.app.test_client()
        self.post('/register', {'name': 'Rob Hill', 'username': 'rob', 'password': PW, 'password2': PW}, c=anon)
        uid = self.uid('rob')
        self.post(f'/admin/users/{uid}', {'do': 'reject'})
        self.assertEqual(self.post(f'/admin/users/{uid}', {'do': 'delete'}).status_code, 302)
        self.assertEqual(self.post('/register', {'name': 'Rob Hill', 'username': 'rob', 'password': PW, 'password2': PW}, c=anon).status_code, 302)

    def test_unknown_page_and_oversized_upload(self):
        self.setup_admin()
        r = self.c.get('/no/such/page')
        self.assertEqual(r.status_code, 404)
        self.assertIn('does not exist', self.text(r))
        self.app.config['MAX_CONTENT_LENGTH'] = 2000
        r = self.post('/health/import', {'file': (io.BytesIO(b'x' * 6000), 'big.xlsx')}, content_type='multipart/form-data')
        self.assertEqual(r.status_code, 413)


class Engine(AppCase):
    def test_sample_workbook_through_the_engine(self):
        path = os.path.join(self.tmp, 'w.xlsx')
        sample.make(path)
        res = imports.scan(imports.read_workbook(path))
        master = E.build_master(res['master']['headers'], res['master']['rows'])
        plan = E.clean_plan(res['plan']['headers'], res['plan']['rows'], master)
        self.assertEqual((plan['raw_count'], len(plan['rows']), plan['dropped'], plan['grand_total']), (9, 7, 2, 4300))
        self.assertEqual(sum(r.plan_qty for r in plan['rows']), 4300)
        runs = E.build_runs(plan['rows'], E.Settings(as_of=sample.AS_OF))
        self.assertEqual([r.type for r in runs], ['Line start', 'Process change', 'High variation', 'Line start', 'Master data missing', 'Master data missing'])
        self.assertEqual([r.changes for r in runs], [None, 1, 7, None, None, None])
        self.assertEqual([r.status for r in runs], ['Matched', 'Matched', 'Matched', 'Matched', 'Not in master', 'Incomplete'])
        self.assertEqual(runs[1].to_pf, 1003)                                      # "RF 1003" became 1003
        self.assertEqual({r.rank: r.run_no for r in runs if r.rank}, {1: 3, 2: 2})
        hc = {c['id']: c for c in E.health_checks(plan, master)}
        self.assertEqual([hc[i]['result'] for i in (5, 6, 8, 9, 11)], ['4,300 vs 4,300', '1 rows', '1 PFs · 400 pcs', '1 PFs', '1 lines'])
        self.assertEqual(E.build_runs(plan['rows'], E.Settings(high_var_threshold=1))[1].type, 'High variation')


class Pages(AppCase):
    URLS = ['/dashboard', '/kanban', '/plan', '/plan?mode=rows', '/health', '/linewise', '/master', '/admin/users', '/admin/permissions',
            '/admin/audit', '/admin/audit?verify=1', '/admin/settings', '/account']

    def test_every_page_renders_with_and_without_data(self):
        self.setup_admin()
        for u in self.URLS:
            self.assertEqual(self.c.get(u).status_code, 200, u)
        self.assertIn('No plan data yet', self.text(self.c.get('/dashboard')))
        self.load_sample()
        extra = ['/kanban?line=UJL-01&pri=High&q=100', '/plan?line=UJL-02&type=Master+data+missing', '/plan?mode=rows&chk=Date+conflicts',
                 '/plan?mode=rows&chk=Master+issues', '/master?q=1001']
        for u in self.URLS + extra:
            self.assertEqual(self.c.get(u).status_code, 200, u)
        for u in ('/plan.csv', '/plan.csv?mode=rows', '/master.csv', '/linewise.csv', '/admin/audit.csv'):
            r = self.c.get(u)
            self.assertEqual(r.status_code, 200, u)
            self.assertIn('text/csv', r.headers['Content-Type'])

    def test_dashboard_and_board_show_the_numbers(self):
        self.setup_admin()
        self.load_sample()
        html = self.text(self.c.get('/dashboard'))
        for needle in ('Total runs', 'UJL-01', 'UJL-02', 'PF 1005', 'Data blocked', '4,300'):
            self.assertIn(needle, html)
        board = self.text(self.c.get('/kanban'))
        self.assertIn('UJL-01 · PF 1003', board)
        self.assertIn('data-can-move="1"', board)
        self.assertIn('draggable="true"', board)

    def test_board_is_read_only_for_viewers(self):
        self.setup_admin()
        self.load_sample()
        viewer = self.user_client('vic', 'Viewer')
        board = self.text(viewer.get('/kanban'))
        self.assertIn('data-can-move="0"', board)
        self.assertNotIn('draggable="true"', board)
        self.assertNotIn('data-act="card-save"', board)
        self.assertEqual(viewer.get('/linewise.csv').status_code, 403)
        self.assertNotIn('Export CSV', self.text(viewer.get('/plan')))

    def test_home_goes_to_the_first_page_a_role_may_open(self):
        self.setup_admin()
        with self.app.app_context():
            with D.tx() as con:
                con.execute("INSERT INTO roles(name, perms, sort) VALUES('Board only', ?, 9)", (json.dumps(['kanban.view']),))
                con.execute("INSERT INTO roles(name, perms, sort) VALUES('Nothing', ?, 10)", (json.dumps([]),))
        c = self.user_client('bo', 'Board only')
        self.assertIn('/kanban', c.get('/').headers['Location'])
        self.assertEqual(c.get('/dashboard').status_code, 403)
        n = self.user_client('nobody', 'Nothing')
        r = n.get('/')
        self.assertEqual(r.status_code, 403)
        self.assertIn('no permissions yet', self.text(r))


class Kanban(AppCase):
    def test_seeded_cards_and_numbers(self):
        self.setup_admin()
        self.load_sample()
        s = self.state()
        self.assertEqual({k: v['cards'] for k, v in s['summary']['stages'].items()}, {'Data Blocked': 2, 'Planned': 1, 'Ready': 1, 'Running': 2, 'Done': 0})
        self.assertEqual((s['summary']['plan_qty'], s['summary']['high'], len(s['runs'])), (4300, 4, 6))
        c = self.card(K_LOCKED)
        self.assertEqual((c.stage, c.locked, c.owner, c.priority), ('Ready', True, 'Ali', 'High'))
        o = self.card('UJL-01|1001|20261102')
        self.assertEqual((o.priority, o.locked, o.stage), ('Medium', False, 'Running'))      # saved priority only; stage still automatic

    def test_move_locks_and_unlock_returns_to_auto(self):
        self.setup_admin()
        self.load_sample()
        r = self.jpost('/api/card/move', {'key': K_PLANNED, 'stage': 'Ready'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['card']['locked'])
        c = self.card(K_PLANNED)
        self.assertEqual((c.stage, c.locked), ('Ready', True))
        self.assertEqual(self.jpost('/api/card/unlock', {'key': K_PLANNED}).status_code, 200)
        c = self.card(K_PLANNED)
        self.assertEqual((c.stage, c.locked), ('Planned', False))
        self.assertEqual(self.jpost('/api/card/move', {'key': K_PLANNED, 'stage': 'Nope'}).status_code, 400)
        self.assertEqual(self.jpost('/api/card/move', {'key': 'missing|1|1', 'stage': 'Ready'}).status_code, 404)
        self.assertEqual(self.audit_actions().count('kanban.move'), 1)
        self.assertIn('kanban.unlock', self.audit_actions())

    def test_dropping_a_card_on_its_own_stage_does_not_lock_it(self):
        self.setup_admin()
        self.load_sample()
        self.assertEqual(self.jpost('/api/card/move', {'key': K_PLANNED, 'stage': 'Planned'}).status_code, 200)
        self.assertFalse(self.card(K_PLANNED).locked)
        self.assertNotIn('kanban.move', self.audit_actions())

    def test_card_permissions_follow_the_role(self):
        self.setup_admin()
        self.load_sample()
        viewer, sup, planner = self.user_client('vic', 'Viewer'), self.user_client('sup', 'Line Supervisor'), self.user_client('pat', 'Planner')
        move = {'key': K_PLANNED, 'stage': 'Ready'}
        self.assertEqual(self.jpost('/api/card/move', move, c=viewer).status_code, 403)
        self.assertEqual(self.jpost('/api/card/save', dict(move, priority='Low', owner='', notes=''), c=viewer).status_code, 403)
        self.assertEqual(self.jpost('/api/card/move', move, c=sup).status_code, 403)             # supervisors edit details but cannot move
        body = {'key': K_PLANNED, 'stage': 'Planned', 'priority': 'Low', 'owner': 'Rina', 'notes': 'check trims'}
        self.assertEqual(self.jpost('/api/card/save', body, c=sup).status_code, 200)
        c = self.card(K_PLANNED)
        self.assertEqual((c.priority, c.owner, c.notes, c.locked, c.stage), ('Low', 'Rina', 'check trims', False, 'Planned'))
        self.assertEqual(self.jpost('/api/card/save', dict(body, stage='Ready'), c=sup).status_code, 403)
        self.assertEqual(self.card(K_PLANNED).stage, 'Planned')
        self.assertEqual(self.jpost('/api/card/save', dict(body, stage='Ready', priority='High', owner='Pat', notes='n'), c=planner).status_code, 200)
        c = self.card(K_PLANNED)
        self.assertEqual((c.stage, c.locked, c.priority, c.owner, c.notes), ('Ready', True, 'High', 'Pat', 'n'))
        self.assertEqual(self.jpost('/api/card/unlock', {'key': K_PLANNED}, c=viewer).status_code, 403)

    def test_card_form_values_are_checked(self):
        self.setup_admin()
        self.load_sample()
        ok = {'key': K_PLANNED, 'stage': 'Planned', 'priority': 'Low', 'owner': 'x', 'notes': 'y'}
        self.assertEqual(self.jpost('/api/card/save', dict(ok, owner='o' * 81)).status_code, 400)
        self.assertEqual(self.jpost('/api/card/save', dict(ok, priority='Urgent')).status_code, 400)
        self.assertEqual(self.jpost('/api/card/save', dict(ok, stage='Moon')).status_code, 400)
        self.assertEqual(self.jpost('/api/card/save', dict(ok, key='gone|1|1')).status_code, 404)


class Import(AppCase):
    def upload(self, raw, name, c=None):
        return self.post('/health/import', {'file': (io.BytesIO(raw), name)}, c=c, content_type='multipart/form-data')

    def sample_bytes(self):
        p = os.path.join(self.tmp, 'up.xlsx')
        sample.make(p)
        with open(p, 'rb') as f:
            return f.read()

    def test_upload_preview_commit(self):
        self.setup_admin()
        r = self.upload(self.sample_bytes(), 'plan.xlsx')
        self.assertEqual(r.status_code, 302)
        page = self.c.get(r.headers['Location'])
        html = self.text(page)
        for needle in ('Commit import', '7 rows kept, 2 subtotal or blank rows dropped', 'ties to the file total', '1 PFs with blank process fields', 'Saved Kanban cards',
                       'Line-wise rows'):
            self.assertIn(needle, html)
        token = re.search(r'name="token" value="([^"]+)"', html).group(1)
        self.assertEqual(self.post('/health/import/commit', {'token': token, 'use_plan': '1', 'use_master': '1'}).status_code, 302)
        s = self.state()
        self.assertEqual((len(s['runs']), s['summary']['plan_qty']), (6, 4300))
        self.assertEqual(len(s['overlay']), 0)                                           # cards were not ticked
        self.assertIn('plan.xlsx', self.text(self.c.get('/health')))
        again = self.post('/health/import/commit', {'token': token, 'use_plan': '1'}, follow_redirects=True)
        self.assertIn('expired', self.text(again))                                       # a token works once

    def test_commit_with_cards_and_line_wise_rows(self):
        self.setup_admin()
        r = self.upload(self.sample_bytes(), 'plan.xlsx')
        token = re.search(r'imp=([^&"]+)', r.headers['Location']).group(1)
        self.post('/health/import/commit', {'token': token, 'use_plan': '1', 'use_master': '1', 'use_cards': '1', 'use_linewise': '1'})
        with self.app.app_context():
            con = D.get_db()
            self.assertEqual(con.execute('SELECT COUNT(*) FROM cards').fetchone()[0], 2)
            self.assertEqual(con.execute('SELECT COUNT(*) FROM linewise').fetchone()[0], 2)
        self.assertEqual({'import.plan', 'import.master', 'import.cards', 'import.linewise'} - set(self.audit_actions()), set())

    def test_nothing_ticked_and_bad_files(self):
        self.setup_admin()
        token = re.search(r'imp=([^&"]+)', self.upload(self.sample_bytes(), 'plan.xlsx').headers['Location']).group(1)
        self.assertIn('Tick at least one', self.text(self.post('/health/import/commit', {'token': token}, follow_redirects=True)))
        r = self.post('/health/import', {'file': (io.BytesIO(b'x'), 'old.xls')}, content_type='multipart/form-data', follow_redirects=True)
        self.assertIn('Only .xlsx', self.text(r))
        r = self.post('/health/import', {'file': (io.BytesIO(b'this is not a zip file'), 'bad.xlsx')}, content_type='multipart/form-data', follow_redirects=True)
        self.assertIn('not a normal .xlsx workbook', self.text(r))
        wb = openpyxl.Workbook()
        wb.active.append(['hello', 'world'])
        buf = io.BytesIO()
        wb.save(buf)
        r = self.post('/health/import', {'file': (io.BytesIO(buf.getvalue()), 'other.xlsx')}, content_type='multipart/form-data', follow_redirects=True)
        self.assertIn('No plan or PF master sheet was recognised', self.text(r))
        self.assertIn('Choose an Excel file first', self.text(self.post('/health/import', {}, content_type='multipart/form-data', follow_redirects=True)))

    def test_huge_unpacked_workbooks_are_refused(self):
        self.setup_admin()
        with mock.patch.object(imports, 'MAX_UNPACKED', 1000):
            r = self.post('/health/import', {'file': (io.BytesIO(self.sample_bytes()), 'big.xlsx')}, content_type='multipart/form-data', follow_redirects=True)
        self.assertIn('far too large once unpacked', self.text(r))

    def test_a_plan_with_no_usable_rows_still_renders_everywhere(self):
        self.setup_admin()
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'Update Plan'
        ws.append(E.PLAN_HEADERS)
        ws.append(['Grand Total'] + [None] * 9 + [0])
        pm = wb.create_sheet('PROCESS DATA')
        pm.append(['BUYER', 'PF'] + E.OPS + ['REMARKS'])
        pm.append(['UQ::UNIQLO', 1001, 'I/L', 'I/L', 'I/L', 'SNCS', 'S/F', 'SETTER', 'VBM', None])
        buf = io.BytesIO()
        wb.save(buf)
        token = re.search(r'imp=([^&"]+)', self.upload(buf.getvalue(), 'empty.xlsx').headers['Location']).group(1)
        self.post('/health/import/commit', {'token': token, 'use_plan': '1', 'use_master': '1'})
        self.assertEqual(len(self.state()['runs']), 0)
        for u in Pages.URLS + ['/plan.csv', '/master.csv', '/linewise.csv', '/kanban?line=x']:
            self.assertEqual(self.c.get(u).status_code, 200, u)

    def test_cancel_discards_the_stash_and_only_importers_may_import(self):
        self.setup_admin()
        token = re.search(r'imp=([^&"]+)', self.upload(self.sample_bytes(), 'plan.xlsx').headers['Location']).group(1)
        viewer = self.user_client('vic', 'Viewer')
        self.assertEqual(self.upload(self.sample_bytes(), 'plan.xlsx', c=viewer).status_code, 403)
        self.assertEqual(self.post('/health/import/commit', {'token': token, 'use_plan': '1'}, c=viewer).status_code, 403)
        self.post('/health/import/cancel', {'token': token})
        self.assertIn('expired', self.text(self.post('/health/import/commit', {'token': token, 'use_plan': '1'}, follow_redirects=True)))

    def test_another_users_import_token_does_not_work(self):
        self.setup_admin()
        token = re.search(r'imp=([^&"]+)', self.upload(self.sample_bytes(), 'plan.xlsx').headers['Location']).group(1)
        planner = self.user_client('pat', 'Planner')
        self.assertIn('expired', self.text(self.post('/health/import/commit', {'token': token, 'use_plan': '1'}, c=planner, follow_redirects=True)))

    def test_first_run_loads_the_workbook_found_in_the_folder(self):
        sample.make(os.path.join(self.tmp, imports.WORKBOOK_NAME))
        self.assertIn(imports.WORKBOOK_NAME, self.text(self.c.get('/setup')))
        self.setup_admin()
        s = self.state()
        self.assertEqual(len(s['runs']), 6)
        self.assertEqual(len(s['overlay']), 2)
        with self.app.app_context():
            self.assertEqual(D.get_db().execute('SELECT COUNT(*) FROM linewise').fetchone()[0], 2)

    def test_an_unreadable_workbook_does_not_block_setup(self):
        with open(os.path.join(self.tmp, imports.WORKBOOK_NAME), 'wb') as f:
            f.write(b'garbage')
        r = self.post('/setup', {'name': 'Boss Person', 'username': 'boss', 'password': PW, 'password2': PW}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn('could not read it', self.text(r))
        self.assertIsNone(self.state())


class LineWise(AppCase):
    def test_edit_recalculates_the_row_and_totals(self):
        self.setup_admin()
        self.load_sample()
        j = self.jpost('/api/linewise/1', {'field': 'manpower', 'value': '50'}).get_json()
        self.assertTrue(j['ok'])
        self.assertEqual((j['cells']['max_target'][0], j['cells']['today_target'][0]), ('1,020', '1,020'))     # 50 x 8 x 60 / 20 x 85%
        self.assertEqual((j['cells']['wip'][0], j['cells']['balance'][0], j['cells']['completion'][0]), ('200', '900', '40.0%'))
        self.assertTrue(j['cells']['status'][0].startswith('Running'))
        self.assertEqual(j['totals']['plan'], '1,500')
        j = self.jpost('/api/linewise/1', {'field': 'pf', 'value': '1002'}).get_json()                       # another style on the same line
        self.assertEqual(j['cells']['plan_qty'][0], '700')
        j = self.jpost('/api/linewise/1', {'field': 'eff', 'value': '90'}).get_json()
        self.assertEqual(j['value'], '90')
        j = self.jpost('/api/linewise/1', {'field': 'actual_input', 'value': '2026-11-01'}).get_json()
        self.assertEqual((j['value'], j['cells']['days_running'][0]), ('2026-11-01', '2'))
        j = self.jpost('/api/linewise/2', {'field': 'pf', 'value': ''}).get_json()
        self.assertTrue(j['cells']['status'][0].startswith('Vacant'))

    def test_bad_values_are_refused(self):
        self.setup_admin()
        self.load_sample()
        for field, value in (('manpower', '-3'), ('manpower', 'abc'), ('manpower', 'nan'), ('eff', '250'), ('area', 'MOON'), ('line', 'UJL-99'),
                             ('actual_input', '2026-13-40'), ('id', '5'), ('n', '5')):
            r = self.jpost('/api/linewise/1', {'field': field, 'value': value})
            self.assertEqual(r.status_code, 400, (field, value))
            self.assertFalse(r.get_json()['ok'])
        self.assertEqual(self.jpost('/api/linewise/999', {'field': 'manpower', 'value': '5'}).status_code, 404)
        self.assertEqual(self.jpost('/api/linewise/1', {'field': 'line', 'value': 'UJL-02'}).status_code, 200)
        self.assertEqual(self.jpost('/api/linewise/1', {'field': 'manpower', 'value': ''}).get_json()['cells']['max_target'][0], '')

    def test_permissions_add_and_delete_rows(self):
        self.setup_admin()
        self.load_sample()
        viewer, sup = self.user_client('vic', 'Viewer'), self.user_client('sup', 'Line Supervisor')
        self.assertEqual(self.jpost('/api/linewise/1', {'field': 'manpower', 'value': '5'}, c=viewer).status_code, 403)
        self.assertEqual(self.post('/linewise/add', c=viewer).status_code, 403)
        self.assertNotIn('data-k="manpower"', self.text(viewer.get('/linewise')))                    # read-only page has no inputs
        self.assertEqual(self.jpost('/api/linewise/1', {'field': 'manpower', 'value': '5'}, c=sup).status_code, 200)
        self.assertEqual(self.post('/linewise/add', c=sup).status_code, 302)
        with self.app.app_context():
            rows = D.get_db().execute('SELECT * FROM linewise ORDER BY n').fetchall()
        self.assertEqual((len(rows), rows[-1]['n'], rows[-1]['eff'], rows[-1]['area']), (3, 3, 0.85, 'OUTPUT'))
        self.assertEqual(self.post(f"/linewise/{rows[-1]['id']}/delete", c=sup).status_code, 302)
        with self.app.app_context():
            self.assertEqual(D.get_db().execute('SELECT COUNT(*) FROM linewise').fetchone()[0], 2)
        self.assertIn('linewise.delete', self.audit_actions())

    def test_nothing_to_choose_before_a_plan_exists(self):
        self.setup_admin()
        self.assertIn('Import a plan first', self.text(self.post('/linewise/add', follow_redirects=True)))


class PfMaster(AppCase):
    def test_filling_a_blank_cell_unblocks_the_card(self):
        self.setup_admin()
        self.load_sample()
        s = self.state()
        i, k = next(i for i, r in enumerate(s['mrows']) if r[1] == '1004'), 2 + E.OPS.index('BTM HEM')
        self.assertEqual(self.card(K_BLOCKED).stage, 'Data Blocked')
        j = self.jpost('/api/master/cell', {'i': i, 'k': k, 'pf': '1004', 'v': 'VBM'}).get_json()
        self.assertEqual((j['ok'], j['blank']), (True, False))
        self.assertEqual(self.card(K_BLOCKED).stage, 'Planned')
        self.assertEqual(self.jpost('/api/master/cell', {'i': i, 'k': k, 'pf': '1004', 'v': ''}).get_json()['blank'], True)
        self.assertEqual(self.card(K_BLOCKED).stage, 'Data Blocked')
        self.assertEqual(self.audit_actions().count('master.edit'), 2)

    def test_master_edits_are_checked(self):
        self.setup_admin()
        self.load_sample()
        self.assertEqual(self.jpost('/api/master/cell', {'i': 0, 'k': 2, 'pf': self.state()['mrows'][0][1], 'v': 'XYZ'}).status_code, 400)
        self.assertEqual(self.jpost('/api/master/cell', {'i': 0, 'k': 2, 'pf': '9999', 'v': 'I/L'}).status_code, 409)       # the table has changed
        self.assertEqual(self.jpost('/api/master/cell', {'i': 0, 'k': 0, 'pf': '1', 'v': ''}).status_code, 409)
        self.assertEqual(self.jpost('/api/master/cell', {'i': 99, 'k': 2, 'pf': '1', 'v': ''}).status_code, 409)
        self.assertEqual(self.jpost('/api/master/cell', {'i': 'x', 'k': 2}).status_code, 400)
        viewer = self.user_client('vic', 'Viewer')
        self.assertEqual(self.jpost('/api/master/cell', {'i': 0, 'k': 2, 'pf': '1001', 'v': 'I/L'}, c=viewer).status_code, 403)

    def test_add_pf(self):
        self.setup_admin()
        self.load_sample()
        self.assertEqual(self.state()['runs'][4].status, 'Not in master')
        self.assertIn('PF 1005 added', self.text(self.post('/master/add', {'pf': '1005'}, follow_redirects=True)))
        self.assertEqual(self.state()['runs'][4].status, 'Incomplete')                         # now in the master, process codes still blank
        self.assertIn('already exists', self.text(self.post('/master/add', {'pf': '1005'}, follow_redirects=True)))
        self.assertIn('digits only', self.text(self.post('/master/add', {'pf': 'abc'}, follow_redirects=True)))
        self.assertIn('master.add', self.audit_actions())


class Exports(AppCase):
    def test_csv_guards_against_formulas_and_follows_permissions(self):
        self.setup_admin()
        self.load_sample()
        self.jpost('/api/linewise/1', {'field': 'remarks', 'value': '=HYPERLINK("http://x.example")'})
        r = self.c.get('/linewise.csv')
        body = r.get_data(as_text=True)
        self.assertTrue(body.startswith('\ufeff'))
        self.assertIn("'=HYPERLINK", body)
        self.assertNotIn(',=HYPERLINK', body)
        self.assertIn('attachment; filename=line-wise-', r.headers['Content-Disposition'])
        self.assertEqual(self.user_client('vic', 'Viewer').get('/linewise.csv').status_code, 403)
        self.assertEqual(self.user_client('sup', 'Line Supervisor').get('/linewise.csv').status_code, 200)
        self.assertEqual(self.user_client('sup2', 'Line Supervisor').get('/admin/audit.csv').status_code, 403)
        self.assertIn('export.csv', self.audit_actions())

    def test_plan_csv_follows_the_filters(self):
        self.setup_admin()
        self.load_sample()
        runs = list(__import__('csv').reader(io.StringIO(self.text(self.c.get('/plan.csv?line=UJL-02')).lstrip('\ufeff'))))
        self.assertEqual(runs[0][:3], ['Run', 'Line', 'Seq'])
        self.assertEqual([r[1] for r in runs[1:]], ['UJL-02'] * 3)
        rows = list(__import__('csv').reader(io.StringIO(self.text(self.c.get('/plan.csv?mode=rows')).lstrip('\ufeff'))))
        self.assertEqual(len(rows), 8)


class Audit(AppCase):
    def test_chain_detects_edits_and_deletions(self):
        self.setup_admin()
        self.load_sample()
        self.jpost('/api/card/move', {'key': K_PLANNED, 'stage': 'Ready'})
        with self.app.app_context():
            v = D.verify_audit(D.get_db())
        self.assertTrue(v['ok'])
        self.assertGreaterEqual(v['count'], 5)
        self.assertIn('The chain is intact', self.text(self.c.get('/admin/audit?verify=1')))
        con = sqlite3.connect(self.app.config['DB_PATH'])
        mid = con.execute('SELECT id, detail FROM audit ORDER BY id LIMIT 1 OFFSET 2').fetchone()
        con.execute("UPDATE audit SET detail='forged' WHERE id=?", (mid[0],))
        con.commit()
        with self.app.app_context():
            v = D.verify_audit(D.get_db())
        self.assertEqual((v['ok'], v['bad_id']), (False, mid[0]))
        self.assertIn(f'broken at entry #{mid[0]}', self.text(self.c.get('/admin/audit?verify=1')))
        con.execute('UPDATE audit SET detail=? WHERE id=?', (mid[1], mid[0]))
        con.commit()
        with self.app.app_context():
            self.assertTrue(D.verify_audit(D.get_db())['ok'])
        con.execute('DELETE FROM audit WHERE id=?', (mid[0],))
        con.commit()
        con.close()
        with self.app.app_context():
            v = D.verify_audit(D.get_db())
        self.assertFalse(v['ok'])

    def test_important_actions_are_logged_with_who_did_them(self):
        self.setup_admin()
        self.load_sample()
        self.jpost('/api/linewise/1', {'field': 'manpower', 'value': '44'})
        self.post('/admin/settings', {'high_var_threshold': '2', 'hours': '8', 'aging_days': '3', 'urgent_window_days': '14', 'ready_window_days': '0',
                                      'as_of_mode': 'today', 'as_of_fixed': ''})
        self.c.post('/logout', data={'csrf_token': self.tok()})
        with self.app.app_context():
            rows = D.get_db().execute('SELECT * FROM audit ORDER BY id').fetchall()
        actions = [r['action'] for r in rows]
        for a in ('auth.setup', 'import.plan', 'linewise.edit', 'settings.edit', 'auth.logout'):
            self.assertIn(a, actions)
        edit = next(r for r in rows if r['action'] == 'linewise.edit')
        self.assertEqual((edit['username'], edit['detail']), ('boss', '44.0'))
        self.assertIn('high_var_threshold 3 → 2', next(r for r in rows if r['action'] == 'settings.edit')['detail'])

    def test_filter_and_permission(self):
        self.setup_admin()
        self.load_sample()
        html = self.text(self.c.get('/admin/audit?q=import.master'))
        self.assertIn('import.master', html)
        self.assertNotIn('import.cards', html)
        self.assertNotIn('boom', self.text(self.c.get('/admin/audit?q=100%25_%5C')))             # % _ and \ are searched literally
        self.assertEqual(self.user_client('pat', 'Planner').get('/admin/audit').status_code, 403)


class Admin(AppCase):
    def roles(self):
        with self.app.app_context():
            return {r['name']: json.loads(r['perms']) for r in D.get_db().execute('SELECT * FROM roles ORDER BY sort')}

    def test_super_user_edits_role_permissions(self):
        self.setup_admin()
        self.load_sample()
        viewer = self.user_client('vic', 'Viewer')
        self.assertEqual(self.jpost('/api/card/move', {'key': K_PLANNED, 'stage': 'Ready'}, c=viewer).status_code, 403)
        roles = self.roles()
        roles['Viewer'].append('kanban.move')
        ticks = [f'{n}|{p}' for n, ps in roles.items() for p in ps]
        self.assertEqual(self.post('/admin/permissions', {'do': 'save', 'perm': ticks}).status_code, 302)
        self.assertEqual(self.jpost('/api/card/move', {'key': K_PLANNED, 'stage': 'Ready'}, c=viewer).status_code, 200)      # applies at once
        with self.app.app_context():
            detail = D.get_db().execute("SELECT detail FROM audit WHERE action='perm.edit'").fetchone()['detail']
        self.assertEqual(detail, '+kanban.move')
        self.post('/admin/permissions', {'do': 'reset'})
        self.assertEqual(self.roles()['Viewer'], E.DEFAULT_ROLES['Viewer'])
        self.assertEqual(self.jpost('/api/card/move', {'key': K_PLANNED, 'stage': 'Planned'}, c=viewer).status_code, 403)

    def test_add_and_delete_roles(self):
        self.setup_admin()
        self.post('/admin/permissions', {'do': 'add', 'name': 'Quality'})
        self.assertEqual(self.roles()['Quality'], ['dashboard.view'])
        for bad in ('super user', 'quality', 'x', '<b>bad</b>'):
            self.post('/admin/permissions', {'do': 'add', 'name': bad})
        self.assertEqual(sorted(self.roles()), sorted(list(E.DEFAULT_ROLES) + ['Quality']))
        self.user_client('qa', 'Quality')
        self.assertIn('still have that role', self.text(self.post('/admin/permissions', {'do': 'delete', 'name': 'Quality'}, follow_redirects=True)))
        self.post('/admin/users/%d' % self.uid('qa'), {'do': 'save', 'role': 'Viewer', 'status': 'disabled'})
        self.post('/admin/permissions', {'do': 'delete', 'name': 'Quality'})
        self.assertNotIn('Quality', self.roles())
        self.assertIn('role.add', self.audit_actions())
        self.assertIn('role.delete', self.audit_actions())

    def test_settings_are_checked_and_take_effect(self):
        self.setup_admin()
        self.load_sample()
        base = {'high_var_threshold': '3', 'hours': '8', 'aging_days': '3', 'urgent_window_days': '14', 'ready_window_days': '0',
                'as_of_mode': 'fixed', 'as_of_fixed': '2026-11-03'}
        for bad in (dict(base, high_var_threshold='9'), dict(base, hours='0'), dict(base, as_of_fixed=''), dict(base, ready_window_days='99'),
                    dict(base, as_of_mode='tomorrow'), dict(base, hours='abc')):
            self.assertEqual(self.post('/admin/settings', bad).status_code, 400, bad)
        self.assertEqual(self.state()['runs'][1].type, 'Process change')
        self.assertEqual(self.post('/admin/settings', dict(base, high_var_threshold='1')).status_code, 302)
        self.assertEqual(self.state()['runs'][1].type, 'High variation')
        self.post('/admin/settings', dict(base, high_var_threshold='3', ready_window_days='12'))
        self.assertEqual(self.card(K_PLANNED).stage, 'Ready')                                    # starts within 12 days of the as-of date
        self.post('/admin/settings', dict(base, ready_window_days='0', as_of_mode='today', as_of_fixed=''))
        self.assertEqual(self.state()['as_of'], D.today_serial())
        self.assertEqual(self.user_client('pat', 'Planner').get('/admin/settings').status_code, 403)


if __name__ == '__main__':
    unittest.main()
