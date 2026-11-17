"""User isolation and redaction contracts; all model traffic is mocked."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

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
        for hidden in ('private-key-name', 'private response', 'private answer', 'raw upstream error', 'user_id', 'api_key_id', 'key_name', '11'):
            self.assertNotIn(hidden, serialized)

    def test_model_unsupported_stops_double_verification(self):
        job = self.store.create_self_job('11', 7, 'private-key-name', 'unsupported', 'medium', ['candy'], False)
        self.service.tokens[job['id']] = 'fixture.session.token'
        self.service.identity = Mock(return_value={'id': 11, 'role': 'user'})
        self.service._request = Mock(return_value={'items': [{'id': 7, 'key': 'fixture-secret-key'}]})
        self.service._call = Mock(side_effect=ConsoleError('HTTP 400：model not supported', 502, 'MODEL_UNSUPPORTED'))
        self.service.execute(job)
        self.service._call.assert_called_once()
        saved = self.store.self_job(job['id'])
        self.assertEqual(saved['status'], 'completed_with_errors')
        self.assertEqual(saved['results'][0]['rounds'][0]['error_code'], 'MODEL_UNSUPPORTED')
        self.assertNotIn('fixture-secret-key', Path(self.store.path).read_bytes().decode(errors='ignore'))
        self.assertEqual(self.service.tokens, {})


if __name__ == '__main__':
    unittest.main()
