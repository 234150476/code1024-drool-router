"""HTTP isolation, embedding and job controls. No gateway or paid requests."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from detector.runtime import Runtime
from detector.server import frame_policy, make_server
from detector.upstream import ProbeError, Sub2API
from detector.session_auth import verify_admin_session

TOKEN = 'offline-dashboard-secret-123456789'


class StandaloneHTTP(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.public = self.root / 'public'
        self.public.mkdir()
        (self.root / 'index.html').write_text('<html><head></head><body>dashboard</body></html>')
        (self.public / 'state.json').write_text(json.dumps({'accounts': []}))
        self.runtime = Mock()
        self.runtime.status.return_value = {'configured': False}
        self.server = make_server('127.0.0.1', 0, self.root, self.public, access_token=TOKEN,
                                  frame_ancestors=['https://panel.example.com'], runtime=self.runtime)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.temp.cleanup()

    def request(self, path, body=None, auth=True, origin=None, method=None):
        headers = {'Authorization': 'Bearer ' + TOKEN} if auth else {}
        if body is not None:
            headers.update({'Content-Type': 'application/json', 'Origin': origin or self.base})
        request = Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                          headers=headers, method=method)
        try:
            return urlopen(request)
        except HTTPError as response:
            return response

    def test_data_requires_auth_static_shell_does_not_and_no_secrets_are_projected(self):
        with self.request('/api/state', auth=False) as response:
            self.assertEqual(response.status, 401)
        with self.request('/api/state') as response:
            value = json.load(response)
            self.assertEqual(value['accounts'], [])
            self.assertNotIn(TOKEN, json.dumps(value))
        with self.request('/', auth=False) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("frame-ancestors 'self' https://panel.example.com", response.headers['Content-Security-Policy'])
            self.assertIsNone(response.headers.get('X-Frame-Options'))
        with self.request('/healthz', auth=False) as response:
            self.assertEqual(response.status, 200)

    def test_artifact_capability_is_scoped_and_works_without_dashboard_token(self):
        ident = 'a' * 32
        (self.public / (ident + '.json')).write_text(json.dumps({'kind': 'drawing', 'status': 'pass', 'slot': time.time()}))
        (self.public / (ident + '.html')).write_text('<html>art</html>')
        with self.request('/api/artifact-links/' + ident) as response:
            url = json.load(response)['url']
        self.assertNotIn(TOKEN, url)
        with self.request(url, auth=False) as response:
            self.assertEqual(response.status, 200)
            self.assertIn('sandbox allow-scripts', response.headers['Content-Security-Policy'])
            self.assertIn("frame-ancestors 'self' https://panel.example.com", response.headers['Content-Security-Policy'])
        with self.request(url.replace('a' * 32, 'b' * 32), auth=False) as response:
            self.assertEqual(response.status, 401)
        with self.request('/artifacts/' + ident + '.html', auth=False) as response:
            self.assertEqual(response.status, 401)

    def test_jobs_require_auth_origin_post_and_valid_payload(self):
        for kwargs, status in [({'auth': False}, 401), ({'origin': 'https://attacker.example'}, 403),
                               ({'method': 'PUT'}, 405)]:
            with self.request('/api/jobs', {'action': 'sync'}, **kwargs) as response:
                self.assertEqual(response.status, status)
        self.runtime.start.assert_not_called()
        self.runtime.start.return_value = True
        with self.request('/api/jobs', {'action': 'sync'}) as response:
            self.assertEqual(response.status, 202)
        self.runtime.start.assert_called_once_with('sync')
        self.runtime.start.return_value = False
        with self.request('/api/jobs', {'action': 'run'}) as response:
            self.assertEqual(response.status, 409)

    def test_host_session_is_checked_by_server_and_never_trusted_from_url(self):
        self.server.session_auth_url = 'http://owning-gateway:8080'
        with patch('detector.server.verify_admin_session', return_value=True) as verify:
            request = Request(self.base + '/api/state', headers={'Authorization': 'Bearer header.session.token'})
            with urlopen(request) as response:
                self.assertEqual(response.status, 200)
            self.assertEqual(verify.call_args.args[1], 'header.session.token')
        with patch('detector.server.verify_admin_session', return_value=False):
            with self.request('/api/state?token=untrusted.query.token', auth=False) as response:
                self.assertEqual(response.status, 401)
            self.server.access_token = ''
            with self.request('/api/state', auth=False) as response:
                self.assertEqual(response.status, 401)


class StandaloneConfig(unittest.TestCase):
    def test_manual_worker_uses_ephemeral_verified_session_without_persisting_it(self):
        from detector import config
        with tempfile.TemporaryDirectory() as root, patch.dict('os.environ', {}, clear=True), \
                patch.dict(config.CONFIG, {'base_url': 'http://gateway:8080'}), \
                patch.dict(config.CONFIG['web'], {'session_auth_url': 'http://gateway:8080'}):
            runtime = Runtime(root)
            self.assertTrue(runtime.status()['configured'])
            self.assertFalse(runtime.credential_ready())
            with self.assertRaises(ValueError):
                runtime.start('sync')
            with patch('detector.runtime.subprocess.run') as run:
                run.return_value.returncode = 0
                runtime._execute('sync', 'verified.session.token')
                self.assertEqual(run.call_args.kwargs['env']['DROOL_ADMIN_BEARER_TOKEN'], 'verified.session.token')
                self.assertIn('--metadata-only', run.call_args.args[0])
                self.assertNotIn('verified.session.token', ' '.join(run.call_args.args[0]))
            self.assertNotIn('verified.session.token', json.dumps(runtime.status()))

    def test_session_auth_requires_current_active_admin_and_no_redirects(self):
        with patch('detector.session_auth.requests.Session') as factory:
            session = factory.return_value.__enter__.return_value
            response = session.get.return_value
            response.status_code = 200
            response.content = b'fixture'
            for role, status, expected in [('admin', 'active', True), ('user', 'active', False),
                                            ('admin', 'disabled', False)]:
                response.json.return_value = {'code': 0, 'data': {'role': role, 'status': status}}
                self.assertEqual(verify_admin_session('http://gateway:8080', 'a.b.c'), expected)
            self.assertFalse(session.get.call_args.kwargs['allow_redirects'])
            response.status_code = 401
            self.assertFalse(verify_admin_session('http://gateway:8080', 'a.b.c'))

    def test_loopback_without_token_rejects_rebound_host_on_reads_and_writes(self):
        server = make_server('127.0.0.1', 0, '.', '.')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for method in ('GET', 'POST'):
                request = Request('http://127.0.0.1:' + str(server.server_port) + '/api/jobs',
                                  headers={'Host': 'attacker.example', 'Origin': 'http://attacker.example'}, method=method)
                with self.assertRaises(HTTPError) as caught:
                    urlopen(request)
                self.assertEqual(caught.exception.code, 403)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_rejects_csp_injection_and_unprotected_network_service(self):
        for value in [['*'], ['https://panel.test; frame-src *'], ['https://panel.test/path'],
                      ['https://u:p@panel.test'], 'https://panel.test']:
            with self.assertRaises(ValueError):
                frame_policy(value)
        with self.assertRaises(ValueError):
            make_server('0.0.0.0', 0, '.', '.')
        with self.assertRaises(ValueError):
            make_server('127.0.0.1', 0, '.', '.', True)

    def test_unconfigured_runtime_cannot_start_or_spawn(self):
        with tempfile.TemporaryDirectory() as root, patch.dict('os.environ', {}, clear=True):
            runtime = Runtime(root)
            self.assertFalse(runtime.status()['configured'])
            with self.assertRaises(ValueError):
                runtime.start('run')

    def test_local_credentials_keep_export_protected_and_check_identity(self):
        api = Sub2API('https://unused.example', 'unused')
        state = {'id': 7, 'name': 'test-account', 'platform': 'openai', 'type': 'apikey', 'group_ids': []}
        api.account_state = Mock(return_value=state.copy())
        api.get = Mock(side_effect=AssertionError('must not export credentials'))
        with tempfile.TemporaryDirectory() as root:
            file = Path(root) / 'accounts.json'
            entry = {'name': 'test-account', 'platform': 'openai', 'credentials': {'api_key': 'offline-fixture'}}
            file.write_text(json.dumps({'7': entry}))
            with patch.dict('os.environ', {'SUB2API_ACCOUNT_CREDENTIALS_FILE': str(file)}):
                self.assertEqual(api.account(7)['credentials'], entry['credentials'])
                entry['name'] = 'different-account'
                file.write_text(json.dumps({'7': entry}))
                with self.assertRaises(ProbeError) as caught:
                    api.account(7)
                self.assertEqual(caught.exception.code, 'LOCAL_CREDENTIALS_INVALID')
        api.get.assert_not_called()


if __name__ == '__main__':
    unittest.main()
