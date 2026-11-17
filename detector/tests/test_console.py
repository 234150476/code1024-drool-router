"""Real HTTP/worker integration against an isolated, free simulated gateway."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import ssl
import subprocess
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from detector import config
from detector.console_config import ConsoleError, defaults
from detector.console_service import ConsoleService, schedule_slot
from detector.console_store import ConsoleStore
from detector.server import make_server

ADMIN = 'fixture.admin.session'
USER = 'fixture.user.session'
SECRET = 'fixture-independent-secret-123456'
ACCOUNTS = [{'id': i, 'name': 'private-mail-%s@example.com' % i, 'platform': 'openai',
             'type': 'apikey', 'status': 'active', 'group_ids': [3, 4] if i == 1 else [4]}
            for i in (1, 2)]
GROUPS = [{'id': 3, 'name': 'Plus', 'platform': 'openai'}, {'id': 4, 'name': 'Pro', 'platform': 'openai'}]


class Gateway(BaseHTTPRequestHandler):
    requests = []
    def log_message(self, *args): pass
    def send(self, value, status=200):
        data = json.dumps({'code': 0, 'data': value}).encode()
        self.send_response(status); self.send_header('Content-Length', str(len(data)))
        self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/api/v1/auth/me':
            auth = self.headers.get('Authorization')
            if auth not in ('Bearer ' + ADMIN, 'Bearer ' + USER): return self.send({}, 401)
            return self.send({'id': 1, 'role': 'admin' if auth == 'Bearer ' + ADMIN else 'user', 'status': 'active'})
        if path == '/api/v1/admin/accounts': return self.send({'items': ACCOUNTS, 'total': 2})
        if path == '/api/v1/admin/groups/all': return self.send(GROUPS)
        if path == '/api/v1/admin/accounts/data':
            from urllib.parse import parse_qs, urlsplit
            ident = int(parse_qs(urlsplit(self.path).query)['ids'][0])
            a = ACCOUNTS[ident-1]
            return self.send({'accounts': [{**a, 'credentials': {'api_key': 'fixture-account-%s' % ident,
                         'base_url': self.upstream}}]})
        if path.startswith('/api/v1/admin/accounts/'):
            return self.send(ACCOUNTS[int(path.rsplit('/', 1)[1])-1])
        return self.send({}, 404)
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.requests.append((self.headers.get('Authorization'), body))
        content = '<!DOCTYPE html><html><body><svg xmlns="http://www.w3.org/2000/svg"><circle r="8"/></svg></body></html>'
        chunk = {'model': body['model'], 'choices': [{'delta': {'content': content}, 'finish_reason': None}]}
        end = {'choices': [{'delta': {}, 'finish_reason': 'stop'}]}
        data = ('data: ' + json.dumps(chunk) + '\n\ndata: ' + json.dumps(end) + '\n\ndata: [DONE]\n\n').encode()
        self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)


class ConsoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tlsdir = tempfile.TemporaryDirectory()
        cert = Path(cls.tlsdir.name) / 'cert.pem'
        key = Path(cls.tlsdir.name) / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-keyout', str(key), '-out', str(cert), '-subj', '/CN=localhost',
                        '-addext', 'subjectAltName=IP:127.0.0.1'], check=True, capture_output=True)
        cls.tls = ThreadingHTTPServer(('127.0.0.1', 0), Gateway)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(cert, key)
        cls.tls.socket = context.wrap_socket(cls.tls.socket, server_side=True)
        cls.tt = threading.Thread(target=cls.tls.serve_forever, daemon=True); cls.tt.start()
        Gateway.upstream = 'https://127.0.0.1:' + str(cls.tls.server_port)
        cls.gateway = ThreadingHTTPServer(('127.0.0.1', 0), Gateway)
        cls.gt = threading.Thread(target=cls.gateway.serve_forever, daemon=True); cls.gt.start()
        cls.base = 'http://127.0.0.1:' + str(cls.gateway.server_port)
    @classmethod
    def tearDownClass(cls):
        cls.gateway.shutdown(); cls.gt.join(); cls.gateway.server_close()
        cls.tls.shutdown(); cls.tt.join(); cls.tls.server_close(); cls.tlsdir.cleanup()
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.cfg = patch.dict(config.CONFIG, {'base_url': self.base, 'platforms': {'openai': {
            'enabled': True, 'model': 'fixture-new-model', 'protocol': 'openai_chat', 'effort': 'low',
            'group_ids': [3], 'group_names': [], 'exclude_names': []}},
            'web': {'session_auth_url': self.base}, 'upstream': {'ca_bundle': str(Path(self.tlsdir.name)/'cert.pem')},
            'budgets': {**config.CONFIG['budgets'], 'max_attempts': 1, 'drawing_platform_seconds': {}}}); self.cfg.start()
        self.env = patch.dict(os.environ, {}, clear=True); self.env.start()
        self.service = ConsoleService(self.root, start=False)
        self.service.sync_inventory(ADMIN, '1')
        self.plan = self.service.store.plans()[0]
        value = deepcopy(self.plan['value']); value['kinds'] = ['drawing']
        self.plan = self.service.save_plan(value, '1', self.plan['id'], self.plan['revision'])
        Gateway.requests = []
        self.http = make_server('127.0.0.1', 0, Path('web/dist'), self.root/'public',
                 access_token=SECRET, session_auth_url=self.base, console=self.service)
        self.ht = threading.Thread(target=self.http.serve_forever, daemon=True); self.ht.start()
        self.url = 'http://127.0.0.1:' + str(self.http.server_port)
    def tearDown(self):
        self.http.shutdown(); self.ht.join(); self.http.server_close()
        self.env.stop(); self.cfg.stop(); self.tmp.cleanup()
    def call(self, path, body=None, token=None, method=None, origin=None):
        headers = {'Authorization': 'Bearer ' + token} if token else {}
        if body is not None: headers.update({'Content-Type': 'application/json', 'Origin': origin or self.url})
        req = Request(self.url+path, data=json.dumps(body).encode() if body is not None else None, headers=headers, method=method)
        try: response = urlopen(req)
        except HTTPError as e: response = e
        with response:
            raw = response.read()
            return response.status, json.loads(raw) if 'json' in response.headers.get('Content-Type', '') else raw, response.headers
    def enqueue(self, plan=None, key='fixture-request-001'):
        plan = plan or self.plan
        preview = self.service.store.resolve(plan)
        payload = {'plan_id': plan['id'], 'request_key': key, 'confirmed': True,
                   **{k: preview[k] for k in ('plan_revision', 'settings_revision', 'inventory_revision')}}
        return self.service.enqueue(payload, ADMIN, '1'), payload
    def test_public_admin_and_legacy_boundaries(self):
        status, data, headers = self.call('/api/public/overview')
        self.assertEqual(status, 200); self.assertEqual(headers['Cache-Control'], 'no-store')
        text = json.dumps(data)
        for hidden in ('private-mail', '@example.com', 'account_id', 'credentials', SECRET): self.assertNotIn(hidden, text)
        for token, expected in [(None, 401), (USER, 403), (SECRET, 401), (ADMIN, 200)]:
            self.assertEqual(self.call('/api/admin/bootstrap', token=token)[0], expected)
        self.assertEqual(self.call('/api/state', token=SECRET)[0], 401)
        self.assertEqual(self.call('/api/admin/inventory/sync', {}, ADMIN, origin='https://evil.test')[0], 403)
        self.assertEqual(self.call('/api/admin/settings', {'value': [], 'revision': 1}, ADMIN, 'PUT')[0], 400)
        self.assertEqual(len(Gateway.requests), 0)
    def test_scope_dedup_and_snapshots_revision_idempotence(self):
        value = deepcopy(self.plan['value']); value['scope']['group_ids'] = [3,4]
        plan = self.service.save_plan(value, '1', self.plan['id'], self.plan['revision'])
        self.assertEqual(self.service.store.resolve(plan)['account_count'], 2)
        job, payload = self.enqueue(plan)
        self.assertEqual(self.service.enqueue(payload, ADMIN, '1')['id'], job['id'])
        doc = self.service.store.document('settings'); updated = deepcopy(doc['value']); updated['concurrency'] = 1
        self.service.save_settings(updated, doc['revision'], '1')
        self.assertEqual(self.service.store.job(job['id'])['snapshot']['settings']['value']['concurrency'], 2)
        with self.assertRaises(ConsoleError): self.service.save_settings(updated, doc['revision'], '1')
        payload['request_key'] = 'fixture-request-002'
        with self.assertRaises(ConsoleError): self.service.enqueue(payload, ADMIN, '1')
        self.assertEqual(len(Gateway.requests), 0)
    def test_public_groups_hide_account_metadata_and_legacy_account_routes(self):
        job, _ = self.enqueue()
        self.service.execute(self.service.store.job(job['id']))
        document = self.service.store.document('publication')
        names = deepcopy(document['value'])
        names['account_names']['1'] = 'private-account-alias'
        self.service.store.save_document('publication', names, document['revision'], '1')
        group_id = self.service.store.public_id('group', 'g3')
        account_id = self.service.store.public_id('account', 'g3:1')
        for token in (None, USER, ADMIN):
            for path in ('/api/public/overview', '/api/public/groups',
                         '/api/public/overview?include_accounts=true', '/api/public/overview?hours=1'):
                status, data, _ = self.call(path, token=token)
                self.assertEqual(status, 200)
                group = data['groups'][0]
                self.assertEqual(set(group), {'id', 'name', 'method', 'profiles'})
                self.assertEqual(group['profiles'][0]['pass'], 1)
                for hidden in ('private-account-alias', 'private-mail', 'account_count', account_id):
                    self.assertNotIn(hidden, json.dumps(data))
            self.assertEqual(self.call('/api/public/accounts/'+account_id+'/results', token=token)[0], 404)
        status, group, _ = self.call('/api/public/groups/'+group_id+'/results')
        self.assertEqual(status, 200)
        self.assertEqual(set(group), {'id', 'name', 'method', 'profiles'})
        detail_path = '/api/admin/jobs/'+job['id']
        self.assertEqual(self.call(detail_path, token=USER)[0], 403)
        status, detail, _ = self.call(detail_path, token=ADMIN)
        self.assertEqual(status, 200)
        self.assertEqual(detail['results'][0]['account_name'], ACCOUNTS[0]['name'])
    def test_real_worker_selected_account_new_model_history_and_revocation(self):
        job, _ = self.enqueue(); self.service.execute(self.service.store.job(job['id']))
        result = self.service.store.job(job['id'])
        self.assertEqual(result['status'], 'completed', (self.root/'private/jobs'/job['id']/'worker.log').read_text())
        self.assertEqual(len(Gateway.requests), 1)
        self.assertEqual(Gateway.requests[0][0], 'Bearer fixture-account-1')
        self.assertEqual(Gateway.requests[0][1]['model'], 'fixture-new-model')
        rows = self.service.store.results(); self.assertEqual(len(rows), 1); self.assertEqual(rows[0]['value']['status'], 'pass')
        # A changed plan targets only account 2; prior account 1 result remains.
        value = deepcopy(self.plan['value']); value['scope'] = {'mode':'accounts','group_ids':[],'account_ids':[2],'exclude_ids':[]}
        second = self.service.save_plan(value, '1')
        job2, _ = self.enqueue(second, 'fixture-request-002'); self.service.execute(self.service.store.job(job2['id']))
        self.assertEqual({r['account_id'] for r in self.service.store.results()}, {1,2})
        self.assertEqual([r[0] for r in Gateway.requests], ['Bearer fixture-account-1','Bearer fixture-account-2'])
        value = deepcopy(self.plan['value']); value['show_artifacts'] = True
        self.plan = self.service.save_plan(value, '1', self.plan['id'], self.plan['revision'])
        run = self.service.store.public_id('run', rows[0]['id'])
        self.assertEqual(self.call('/api/public/runs/'+run)[0], 200)
        status, _, headers = self.call('/api/public/runs/'+run+'/artifact')
        self.assertEqual(status, 200); self.assertIn('sandbox allow-scripts', headers['Content-Security-Policy'])
        value['published'] = False; self.service.save_plan(value, '1', self.plan['id'], self.plan['revision'])
        for suffix in ('','/artifact'): self.assertEqual(self.call('/api/public/runs/'+run+suffix)[0], 404)
        self.assertNotIn(ADMIN, self.service.store.path.read_bytes().decode(errors='ignore'))
        self.assertNotIn(ADMIN, (self.root/'private/jobs'/job['id']/'config.json').read_text())
    def test_stop_recovery_and_schedule_guard(self):
        job, _ = self.enqueue(); self.service.stop_job(job['id'], '1')
        self.service.execute(self.service.store.job(job['id'])); self.assertEqual(len(Gateway.requests), 0)
        job, _ = self.enqueue(key='fixture-request-002'); self.service.store.recover_jobs()
        self.assertEqual(self.service.store.job(job['id'])['status'], 'interrupted')
        value=deepcopy(self.plan['value']); value['schedule']['enabled']=True
        with self.assertRaises(ConsoleError): self.service.save_plan(value,'1')
        value['schedule']['enabled']=False; value['write_priority']=True
        with self.assertRaises(ConsoleError): self.service.save_plan(value,'1')
        spec=defaults()['schedule']; before=schedule_slot(spec, 1800000000)
        self.assertEqual(before,schedule_slot(spec,before+1)); self.assertLessEqual(before,1800000000)
    def test_profiles_do_not_mix_models_and_publication_survives_restart(self):
        job, _ = self.enqueue(); self.service.execute(self.service.store.job(job['id']))
        value=deepcopy(self.plan['value']);value['models']['openai']['model']='another-fixture-model'
        self.plan=self.service.save_plan(value,'1',self.plan['id'],self.plan['revision'])
        job2,_=self.enqueue(key='fixture-request-002');self.service.execute(self.service.store.job(job2['id']))
        public=self.service.public.projection();self.assertEqual(len(public['groups'][0]['profiles']),2)
        restarted=ConsoleService(self.root,start=False)
        self.assertEqual(len(restarted.store.results()),2)
        self.assertEqual(restarted.store.plans()[0]['revision'],self.plan['revision'])

if __name__ == '__main__': unittest.main()
