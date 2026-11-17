"""User isolation and redaction contracts; all model traffic is mocked."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from detector import config
from detector.console_api import public_self_job
from detector.console_config import ConsoleError
from detector.console_store import ConsoleStore
from detector.self_service import SelfService


class SelfServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ConsoleStore(self.tmp.name)
        self.service = SelfService(self.store, start=False)

    def job(self, public=False):
        job = self.store.create_self_job('11', 7, 'private-key-name', 'fixture-model', 'medium', ['candy'], public)
        rows = [{'kind': 'candy', 'title': '糖果双次校验', 'status': 'error', 'rounds': [
            {'round': 1, 'status': 'error', 'response': 'private response', 'error': 'raw upstream error',
             'answer': 'private answer'}]}]
        self.store.update_self_job(job['id'], status='completed_with_errors', results=rows)
        return self.store.self_job(job['id'])

    def test_users_cannot_read_stop_or_publish_another_users_job(self):
        job = self.job()
        for operation in (self.service.job, self.service.stop_job):
            with self.subTest(operation=operation.__name__), self.assertRaises(ConsoleError) as caught:
                operation(job['id'], '12')
            self.assertEqual(caught.exception.status, 403)
        with self.assertRaises(ConsoleError) as caught:
            self.service.publish(job['id'], '12', True)
        self.assertEqual(caught.exception.status, 403)
        self.assertFalse(self.store.self_job(job['id'])['public'])

    def test_anonymous_identity_is_rejected(self):
        with self.assertRaises(ConsoleError) as caught:
            self.service.identity('')
        self.assertEqual(caught.exception.status, 401)

    def test_public_projection_omits_private_fields(self):
        value = public_self_job(self.job(public=True))
        self.assertEqual(set(value), {'id', 'model', 'effort', 'status', 'created_at', 'finished_at', 'results'})
        serialized = json.dumps(value)
        for hidden in ('private-key-name', 'private response', 'private answer', 'raw upstream error', 'user_id', 'api_key_id', 'key_name'):
            self.assertNotIn(hidden, serialized)

    def test_model_unsupported_stops_double_verification(self):
        job = self.store.create_self_job('11', 7, 'private-key-name', 'unsupported', 'medium', ['candy'], False)
        self.service.tokens[job['id']] = 'fixture.session.token'
        self.service.identity = Mock(return_value={'id': 11, 'role': 'user'})
        self.service._request = Mock(return_value={'id': 7, 'key': 'fixture-secret-key', 'status': 'active'})
        self.service._call = Mock(side_effect=ConsoleError('HTTP 400：model not supported', 502, 'MODEL_UNSUPPORTED'))
        self.service.execute(job)
        self.service._call.assert_called_once()
        saved = self.store.self_job(job['id'])
        self.assertEqual(saved['status'], 'completed_with_errors')
        self.assertEqual(saved['results'][0]['rounds'][0]['error_code'], 'MODEL_UNSUPPORTED')
        self.assertNotIn('fixture-secret-key', Path(self.store.path).read_bytes().decode(errors='ignore'))
        self.assertEqual(self.service.tokens, {})

    def test_execute_rechecks_user_and_key_status_before_calling_model(self):
        for identity, key in [({'id': 12}, {'id': 7, 'status': 'active', 'key': 'fixture-secret'}),
                              ({'id': 11}, {'id': 7, 'status': 'inactive', 'key': 'fixture-secret'})]:
            job = self.store.create_self_job('11', 7, 'My Key', 'fixture-model', 'medium', ['candy'], False)
            self.service.tokens[job['id']] = 'fixture.session.token'
            self.service.identity = Mock(return_value=identity)
            self.service._request = Mock(return_value=key)
            self.service._call = Mock()
            self.service.execute(job)
            self.service._call.assert_not_called()
            self.assertEqual(self.store.self_job(job['id'])['status'], 'failed')

    def test_keys_follow_gateway_pagination_without_exposing_raw_keys(self):
        self.service.identity = Mock(return_value={'id': 11})
        self.service._request = Mock(side_effect=[
            {'items': [{'id': 7, 'status': 'active', 'key': 'fixture-secret-key'}], 'pages': 2},
            {'items': [{'id': 8, 'status': 'active', 'key': 'fixture-other-key'}], 'pages': 2}])
        keys = self.service.keys('fixture.session.token')
        self.assertEqual([k['id'] for k in keys], [7, 8])
        self.assertNotIn('fixture-secret-key', json.dumps(keys))
        self.assertEqual(self.service._request.call_args.kwargs['params']['page'], 2)

    def test_gateway_html_and_non_object_responses_are_explicit_errors(self):
        for content_type, value in [('text/html', None), ('application/json', [])]:
            response = Mock(status_code=200, headers={'Content-Type': content_type})
            response.json.return_value = value
            with patch.dict(config.CONFIG, {'base_url': 'http://fixture.invalid'}), patch('detector.self_service.requests.Session') as session:
                session.return_value.__enter__.return_value.request.return_value = response
                with self.assertRaises(ConsoleError) as caught:
                    self.service._request('fixture.session.token', 'GET', '/api/v1/keys')
                self.assertEqual(caught.exception.status, 502)


if __name__ == '__main__':
    unittest.main()
