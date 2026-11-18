"""Optional scheduling quarantine; no real credentials or paid model requests."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import Mock, patch

from detector import config
from detector.account_quarantine import conclusion
from detector.console_config import ConsoleError
from detector.console_service import ConsoleService
from detector.inspection import save, describe
from detector.quality_catalog import evaluate
from detector.upstream import ProbeError, Sub2API

ACCOUNT = {'id': 1, 'name': 'fixture-private-account', 'platform': 'openai', 'type': 'apikey',
           'status': 'active', 'schedulable': True, 'group_ids': [3], 'last_used_at': time.time()}


class FakeGateway:
    def __init__(self):
        self.account = deepcopy(ACCOUNT)
        self.writes = []
        self.error = None

    def account_state(self, ident):
        return deepcopy(self.account)

    def set_schedulable_only(self, ident, enabled, expected):
        self.writes.append((ident, enabled))
        if self.error:
            raise ProbeError(self.error, 'fixture error')
        self.account['schedulable'] = enabled
        return deepcopy(self.account)


class QuarantineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {}, clear=True); self.env.start(); self.addCleanup(self.env.stop)
        self.s = ConsoleService(self.tmp.name, start=False)
        self.s.store.save_inventory([ACCOUNT], [{'id': 3, 'name': 'Plus', 'platform': 'openai'}], 'admin')
        self.machine = patch.object(self.s, 'machine_key', return_value='fixture-machine-key')
        self.machine.start(); self.addCleanup(self.machine.stop)
        self.q = self.s.quarantine
        (self.s.store.private/'quarantine-key').write_text('fixture-scheduling-key')
        self.api = FakeGateway()
        self.gateway = patch.object(self.q, 'api', return_value=self.api)
        self.gateway.start(); self.addCleanup(self.gateway.stop)
        self.plan = self.new_plan()

    def new_plan(self, kind='numeric', enabled=True):
        schedule = deepcopy(self.s.store.document('settings')['value']['schedule'])
        schedule.update(enabled=False, quiet_pause=False)
        return save(self.s, {'group_id': 3, 'enabled': True, 'model': 'fixture-model', 'effort': 'low',
            'question_ids': [kind], 'questions': [], 'schedule': schedule,
            'quarantine': {'enabled': enabled, 'cooldown_minutes': 60}}, 'admin',
            getattr(self, 'plan', {}).get('id'), getattr(self, 'plan', {}).get('revision'))

    def job(self, response='最终答案：324', source='manual', plan=None, complete=True):
        plan = self.s.store.plan((plan or self.plan)['id'])
        job, _ = self.s.store.create_job(plan['id'], uuid.uuid4().hex, 'admin', source, self.s.store.resolve(plan))
        if complete:
            self.results(job, response)
        return self.s.store.job(job['id'])

    def results(self, job, response, status='completed'):
        rows = []
        for question in job['snapshot']['questions']:
            answer = response.get(question['id'], '') if isinstance(response, dict) else response
            verdict = evaluate(question, answer)[0]
            rounds = [{'round': i+1, 'status': verdict, 'response': answer} for i in range(question['rounds'])]
            rows.append({'id': uuid.uuid4().hex, 'account_id': 1, 'group_ids': [3], 'slot': time.time(),
                'model': 'fixture-model', 'platform': 'openai', 'effort': 'low', 'kind': question['id'],
                'question_type': question['kind'], 'bank_version': question['version'], 'rounds': rounds,
                'status': verdict, 'title': question['title'], 'duration': 1, 'finished_at': time.time()})
        self.s.store.import_quality_results(job, rows)
        self.s.store.update_job(job['id'], status=status, finished_at=time.time())
        return rows

    def quarantine(self):
        self.q.on_complete(self.job())
        row = self.q.records.get(1)
        self.assertEqual(row['state'], 'cooling')
        self.assertFalse(self.api.account['schedulable'])
        return row

    def due(self, row=None):
        return self.q.records.change(row or self.q.records.get(1), action='fixture_due', due_at=time.time()-1)

    def recheck(self):
        self.due(); self.q.tick()
        row = self.q.records.get(1)
        self.assertEqual(row['state'], 'rechecking')
        return self.s.store.job(row['recheck_job_id'])

    def test_disabled_policy_is_no_write(self):
        self.plan = self.new_plan(enabled=False)
        self.q.on_complete(self.job()); self.q.tick()
        self.assertEqual(self.api.writes, [])
        self.assertIsNone(self.q.records.get(1))

    def test_legacy_missing_policy_stays_disabled(self):
        value = self.s.store.plan(self.plan['id'])['value']; value.pop('quarantine')
        result = self.s.save_plan(value, 'admin', self.plan['id'], self.plan['revision'])
        self.assertFalse(describe(result)['quarantine']['enabled'])
        self.q.on_complete(self.job()); self.assertEqual(self.api.writes, [])

    def test_key_and_closed_answer_validation(self):
        (self.s.store.private/'quarantine-key').unlink()
        with self.assertRaises(ConsoleError): self.new_plan()
        for kind in ('drawing', 'fingerprint'):
            with self.assertRaises(ConsoleError): self.new_plan(kind=kind)
        for bad in ({'enabled': 'true', 'cooldown_minutes': 60}, {'enabled': True, 'cooldown_minutes': 0},
                    {'enabled': True, 'cooldown_minutes': True}, {'enabled': True, 'cooldown_minutes': 10081}):
            value = deepcopy(self.s.store.plan(self.plan['id'])['value']); value['quarantine'] = bad
            with self.assertRaises(ConsoleError): self.s.save_plan(value, 'admin', self.plan['id'], self.plan['revision'])

    def test_unknown_pass_errors_and_user_source_do_not_disable(self):
        for response in ('最终答案：323', '无法判断', ''):
            self.q.on_complete(self.job(response))
        job = self.job(''); self.s.store.update_job(job['id'], status='completed_with_errors')
        self.q.on_complete(self.s.store.job(job['id']))
        self.q.on_complete(self.job(source='user'))
        self.assertEqual(self.api.writes, [])

    def test_visual_and_reference_never_quarantine(self):
        for kind in ('drawing', 'fingerprint'):
            self.plan = self.new_plan(kind=kind, enabled=False)
            self.q.on_complete(self.job('failed rendering'))
        self.assertEqual(self.api.writes, [])

    def test_forged_or_incomplete_evidence_is_unknown(self):
        job = self.job(); questions = job['snapshot']['questions']
        rows = [r['value'] for r in self.s.store.results() if r['job_id'] == job['id']]
        self.assertEqual(conclusion(questions, rows), 'fail')
        for mutate in (lambda r: r.update(bank_version='wrong'), lambda r: r.update(rounds=[]),
                       lambda r: r['rounds'][0].update(status='pass'), lambda r: r['rounds'][0].update(round=2),
                       lambda r: r['rounds'][0].update(status='error')):
            broken = deepcopy(rows); mutate(broken[0]); self.assertEqual(conclusion(questions, broken), 'unknown')
        self.assertEqual(conclusion(questions, rows+rows), 'unknown')

    def test_wrong_answer_disables_then_only_pass_restores(self):
        row = self.quarantine(); self.q.tick()
        self.assertEqual(self.api.writes, [(1, False)])
        self.assertEqual(self.q.records.get(1)['state'], 'cooling')
        job = self.recheck()
        self.assertFalse(job['snapshot']['targets'][0]['schedulable'])
        self.assertEqual(job['source'], 'cooldown'); self.assertTrue(self.q.authorized(job))
        self.results(job, '最终答案：323'); self.q.on_complete(self.s.store.job(job['id']))
        self.assertEqual(self.api.writes, [(1, False), (1, True)])
        self.assertEqual(self.q.records.get(1)['state'], 'recovered')
        self.q.on_complete(self.s.store.job(job['id'])); self.q.tick()
        self.assertEqual(len(self.api.writes), 2)

    def test_recheck_failure_or_request_error_keeps_disabled(self):
        self.quarantine()
        for status, response in [('completed', '最终答案：324'), ('completed', '不知道'), ('completed_with_errors', '')]:
            job = self.recheck(); self.results(job, response, status)
            self.q.on_complete(self.s.store.job(job['id']))
            row = self.q.records.get(1)
            self.assertEqual(row['state'], 'cooling'); self.assertGreater(row['due_at'], time.time())
            self.assertFalse(self.api.account['schedulable'])
        self.assertEqual(self.api.writes, [(1, False)])

    def test_restart_interrupted_recheck_reenters_cooldown(self):
        self.quarantine(); self.recheck()
        new = ConsoleService(self.tmp.name, start=False)
        with patch.object(new.quarantine, 'api', return_value=self.api): new.quarantine.tick()
        self.assertEqual(new.quarantine.records.get(1)['state'], 'cooling')
        self.assertEqual(self.api.writes, [(1, False)])

    def test_duplicate_tick_does_not_duplicate_recheck(self):
        self.quarantine(); job = self.recheck(); self.q.tick(); self.q.tick()
        self.assertEqual([j['id'] for j in self.s.store.jobs() if j['source']=='cooldown'], [job['id']])

    def test_night_never_enqueues_recheck(self):
        self.quarantine(); self.due()
        with patch('detector.console_service.quiet_paused', return_value=True): self.q.tick()
        self.assertEqual(self.q.records.get(1)['state'], 'cooling')

    def test_policy_off_cancels_recheck_and_never_restores(self):
        self.quarantine(); job = self.recheck(); self.plan = self.new_plan(enabled=False)
        self.assertEqual(self.s.store.job(job['id'])['status'], 'cancelled')
        self.results(job, '最终答案：323'); self.q.on_complete(self.s.store.job(job['id'])); self.q.tick()
        self.assertEqual(self.api.writes, [(1, False)])
        self.assertFalse(self.q.admin_status()['entries'][0]['policy_enabled'])
        self.assertFalse(self.q.authorized(job))

    def test_handover_cancels_and_cannot_be_undone_by_stale_callback(self):
        self.quarantine(); job = self.recheck(); old = self.q.records.get(1)
        self.q.handover(1, 'admin'); self.results(job, '最终答案：323')
        self.q.on_complete(self.s.store.job(job['id'])); self.q.tick()
        self.assertEqual(self.q.records.get(1)['state'], 'manual')
        self.assertEqual(self.api.writes, [(1, False)])
        with self.assertRaises(ConsoleError): self.q.records.change(old, action='stale', state='restoring')

    def test_changed_account_and_preexisting_manual_pause_not_claimed(self):
        for changes in ({'group_ids': [4]}, {'name': 'renamed'}, {'status': 'inactive'}, {'schedulable': False},
                        {'rate_limit_reset_at': time.time()+3600}):
            self.api.account = {**ACCOUNT, **changes}; self.q.on_complete(self.job())
        self.assertEqual(self.api.writes, []); self.assertIsNone(self.q.records.get(1))

    def test_changed_identity_during_cooldown_blocks_restore(self):
        self.quarantine(); self.api.account['name'] = 'renamed'; self.due(); self.q.tick()
        self.assertEqual(self.q.records.get(1)['state'], 'blocked')
        self.assertEqual(self.api.writes, [(1, False)])

    def test_human_resume_closes_ownership(self):
        self.quarantine(); self.api.account['schedulable'] = True; self.due(); self.q.tick()
        self.assertEqual(self.q.records.get(1)['state'], 'manual')
        self.assertEqual(self.api.writes, [(1, False)])

    def test_write_permission_denied_is_visible_and_not_retried(self):
        self.api.error = 'QUARANTINE_HTTP_403'; self.q.on_complete(self.job()); self.q.tick()
        row = self.q.admin_status()['entries'][0]
        self.assertEqual(row['state'], 'blocked'); self.assertEqual(row['error_code'], 'QUARANTINE_HTTP_403')
        self.assertEqual(len(self.api.writes), 1)

    def blocked(self):
        self.api.error = 'QUARANTINE_HTTP_403'
        self.q.on_complete(self.job())
        self.assertEqual(self.q.records.get(1)['state'], 'blocked')
        self.api.error = None
        return self.q.records.get(1)

    def test_explicit_retry_after_permission_repair_disables_and_cools_without_model_request(self):
        self.blocked()
        result = self.q.retry(1, 'admin')
        self.assertEqual(result['state'], 'cooling')
        self.assertFalse(self.api.account['schedulable'])
        self.assertEqual(self.api.writes, [(1, False), (1, False)])
        self.assertEqual(len(self.s.store.jobs()), 1)
        with self.assertRaises(ConsoleError): self.q.retry(1, 'admin')

    def test_retry_after_restoration_permission_repair_requires_current_pass(self):
        self.quarantine(); job = self.recheck(); self.results(job, '最终答案：323')
        self.api.error = 'QUARANTINE_HTTP_403'; self.q.on_complete(self.s.store.job(job['id']))
        self.assertEqual(self.q.records.get(1)['state'], 'blocked')
        self.assertFalse(self.api.account['schedulable'])
        self.api.error = None
        self.assertEqual(self.q.retry(1, 'admin')['state'], 'recovered')
        self.assertTrue(self.api.account['schedulable'])

    def test_retry_rejects_old_evidence_changed_questions_disabled_policy_and_handover(self):
        record = self.blocked(); job = self.s.store.job(record['evidence_job_id'])
        self.s.store.update_job(job['id'], finished_at=time.time()-3601)
        with self.assertRaises(ConsoleError): self.q.retry(1, 'admin')
        self.s.store.update_job(job['id'], finished_at=time.time())
        self.plan = self.new_plan(kind='choice')
        with self.assertRaises(ConsoleError): self.q.retry(1, 'admin')
        self.plan = self.new_plan(enabled=False)
        with self.assertRaises(ConsoleError): self.q.retry(1, 'admin')
        self.plan = self.new_plan()
        self.q.handover(1, 'admin')
        with self.assertRaises(ConsoleError): self.q.retry(1, 'admin')
        self.assertTrue(self.api.account['schedulable'])
        self.assertEqual(len(self.api.writes), 1)

    def test_real_gateway_permission_contract_403_retry_and_passed_recovery(self):
        state = deepcopy(ACCOUNT); calls = []; permissions = {'accounts.read', 'accounts.schedule'}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def reply(self, value, status=200):
                data = json.dumps({'code': 0, 'data': value}).encode()
                self.send_response(status); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
            def do_GET(self): self.reply(state)
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append((self.path, body))
                # Sub2API's scopedAccountFieldPermissions maps schedulable to accounts.status.
                if 'accounts.status' not in permissions:
                    return self.reply({'error': 'operation not authorized'}, 403)
                if self.path != '/api/v1/admin/accounts/1/schedulable' or set(body) != {'schedulable'}:
                    return self.reply({}, 400)
                state.update(body); self.reply(state)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            api = Sub2API('http://127.0.0.1:'+str(server.server_port), 'fixture-restricted-key')
            with patch.object(self.q, 'api', return_value=api):
                self.q.on_complete(self.job())
                self.assertEqual(self.q.records.get(1)['error_code'], 'QUARANTINE_HTTP_403')
                self.assertTrue(state['schedulable'])
                permissions.remove('accounts.schedule'); permissions.add('accounts.status')
                self.assertEqual(self.q.retry(1, 'admin')['state'], 'cooling')
                self.assertFalse(state['schedulable'])
                job = self.recheck(); self.results(job, '最终答案：323')
                self.q.on_complete(self.s.store.job(job['id']))
                self.assertTrue(state['schedulable'])
                self.assertEqual(self.q.records.get(1)['state'], 'recovered')
            self.assertEqual(calls, [('/api/v1/admin/accounts/1/schedulable', {'schedulable': False}),
                ('/api/v1/admin/accounts/1/schedulable', {'schedulable': False}),
                ('/api/v1/admin/accounts/1/schedulable', {'schedulable': True})])
        finally:
            server.shutdown(); thread.join(); server.server_close()

    def test_transient_write_has_bounded_retries_and_delays(self):
        self.api.error = 'QUARANTINE_UNCONFIRMED'; self.q.on_complete(self.job())
        self.q.tick(); self.assertEqual(len(self.api.writes), 1)
        for _ in range(3): self.due(); self.q.tick()
        self.assertEqual(len(self.api.writes), 3)
        self.assertEqual(self.q.records.get(1)['state'], 'blocked')

    def test_initial_read_error_is_audited_without_changing_detection(self):
        job = self.job()
        self.api.account_state = Mock(side_effect=ProbeError('HTTP_403', 'fixture'))
        self.q.on_complete(job)
        self.assertEqual(self.s.store.job(job['id'])['status'], 'completed')
        self.assertIn('HTTP_403', self.q.admin_status()['events'][0]['action'])

    def test_recheck_uses_original_question_and_model_snapshot(self):
        self.quarantine()
        payload = deepcopy(self.plan)
        questions = deepcopy(self.s.store.plan(self.plan['id'])['value']['questions'])
        questions[0]['expected'] = '999'
        self.plan = save(self.s, {'group_id': 3, 'enabled': True, 'model': 'new-model', 'effort': 'high',
            'question_ids': ['numeric'], 'questions': questions, 'schedule': self.plan['schedule'],
            'quarantine': {'enabled': True, 'cooldown_minutes': 60}}, 'admin', self.plan['id'], self.plan['revision'])
        job = self.recheck()
        self.assertEqual(job['snapshot']['questions'][0]['expected'], '323')
        self.assertEqual(job['snapshot']['plan']['value']['models']['openai']['model'], 'fixture-model')
        self.results(job, '最终答案：323'); self.q.on_complete(self.s.store.job(job['id']))
        self.assertEqual(self.q.records.get(1)['state'], 'recovered')

    def test_stale_pass_requires_a_new_recheck(self):
        self.quarantine(); job = self.recheck(); self.results(job, '最终答案：323')
        self.s.store.update_job(job['id'], finished_at=time.time()-3601)
        self.q.on_complete(self.s.store.job(job['id']))
        self.assertEqual(self.q.records.get(1)['state'], 'cooling')
        self.assertEqual(self.api.writes, [(1, False)])

    def test_callback_is_wired_after_worker_and_errors_do_not_change_result(self):
        job = self.job(complete=False)
        root = self.s.store.private/'jobs'/job['id']; root.mkdir(parents=True)
        rows = self.results(job, '最终答案：324')
        self.s.store.update_job(job['id'], status='queued')
        (root/'results.json').write_text(json.dumps(rows))
        with patch('detector.console_service.subprocess.run', return_value=Mock(returncode=0)):
            self.s.execute(self.s.store.job(job['id']))
        self.assertEqual(self.q.records.get(1)['state'], 'cooling')
        self.plan = self.new_plan(enabled=False)
        job = self.job(complete=False)
        with patch('detector.console_service.subprocess.run', return_value=Mock(returncode=0)), patch.object(self.q, 'on_complete', side_effect=RuntimeError('fixture')):
            self.s.execute(job)
        self.assertEqual(self.s.store.job(job['id'])['status'], 'completed')

    def multi_plan(self, hidden=True):
        catalog = self.s.store.document('questions')['value']
        questions = [next(q for q in catalog if q['id']==kind) for kind in ('drawing', 'candy', 'numeric')]
        self.plan = save(self.s, {'group_id': 3, 'enabled': True, 'model': 'fixture-model', 'effort': 'low',
            'question_ids': [q['id'] for q in questions], 'questions': questions, 'schedule': self.plan['schedule'],
            'quarantine': {'enabled': True, 'cooldown_minutes': 60, 'hide_failed_results': hidden}},
            'admin', self.plan['id'], self.plan['revision'])
        return questions

    def test_multi_question_any_wrong_answer_quarantines_and_recovery_needs_all_pass(self):
        questions = self.multi_plan()
        self.assertEqual(len(self.plan['questions']), 3)
        self.assertEqual(questions[1]['rounds'], 1)
        self.q.on_complete(self.job({'drawing': '<svg></svg>', 'numeric': '最终答案：323', 'candy': '最终答案：0个'}))
        self.assertFalse(self.api.account['schedulable'])
        job = self.recheck()
        self.assertEqual([q['id'] for q in job['snapshot']['questions']], ['candy', 'numeric'])
        self.results(job, {'numeric': '最终答案：323', 'candy': '不能回答'})
        self.q.on_complete(self.s.store.job(job['id']))
        self.assertEqual(self.q.records.get(1)['state'], 'cooling')
        job = self.recheck()
        self.results(job, {q['id']: '最终答案：'+str(q['expected'])+'个' for q in job['snapshot']['questions']})
        self.q.on_complete(self.s.store.job(job['id']))
        self.assertEqual(self.q.records.get(1)['state'], 'recovered')

    def test_verified_failure_is_sufficient_despite_another_request_error(self):
        self.multi_plan()
        job = self.job({'drawing': '', 'numeric': '最终答案：324', 'candy': ''})
        self.s.store.update_job(job['id'], status='completed_with_errors')
        self.q.on_complete(self.s.store.job(job['id']))
        self.assertFalse(self.api.account['schedulable'])
        q = deepcopy(job['snapshot']['questions'][2]); q['rounds'] = 2
        row = {'kind': q['id'], 'bank_version': q['version'], 'rounds': [
            {'round': 1, 'status': 'fail', 'response': '最终答案：324'}, {'round': 2, 'status': 'error'}]}
        self.assertEqual(conclusion([q], [row]), 'fail')
        row['rounds'] = row['rounds'][:1]
        self.assertEqual(conclusion([q], [row]), 'fail')

    def test_hiding_covers_entire_account_batch_stats_detail_and_artifact_but_not_admin(self):
        self.multi_plan()
        job = self.job({'drawing': '<svg></svg>', 'numeric': '最终答案：324', 'candy': ''})
        public = self.s.public; group = public.projection()['groups'][0]
        self.assertEqual(group['profiles'], [])
        self.assertEqual(public.history(group['id']), [])
        admin = public.history(group['id'], include_accounts=True)
        self.assertEqual(len(admin), 3); self.assertTrue(all(r['hidden_from_results'] for r in admin))
        for row in admin:
            with self.assertRaises(ConsoleError): public.record(row['id'])
            self.assertEqual(public.record(row['id'], include_hidden=True)['account_id'], 1)
        raw = next(r for r in self.s.store.results() if r['job_id']==job['id'])
        with self.assertRaises(ConsoleError): public.run(self.s.store.public_id('run', raw['id']))
        with self.assertRaises(ConsoleError): public.run(self.s.store.public_id('run', raw['id']), artifact=True)
        self.multi_plan(hidden=False)
        self.assertEqual(len(public.history(group['id'])), 3)
        self.assertTrue(public.projection()['groups'][0]['profiles'])

    def test_hidden_rule_does_not_hide_other_accounts_or_user_key_results(self):
        self.multi_plan()
        self.job({'numeric': '最终答案：324'})
        user = self.s.store.create_self_job('2', 1, 'fixture-user-key', 'fixture-model', 'low', ['numeric'], True,
            group_id=3, group_name='Plus', share_content=True)
        self.s.store.update_self_job(user['id'], status='completed', results=[{'kind': 'numeric', 'title': '计算',
            'question_type': 'number', 'status': 'fail', 'rounds': [{'round': 1, 'status': 'fail', 'response': '最终答案：324'}]}])
        group = self.s.public.projection()['groups'][0]
        self.assertEqual([r['source'] for r in self.s.public.history(group['id'])], ['user'])

    def test_default_candy_upgrade_is_idempotent_and_does_not_rewrite_history_or_custom_questions(self):
        from detector.quality_catalog import validate
        from detector.console_store import ConsoleStore, dumps
        questions = self.s.store.document('questions')['value']
        questions[0].update(title='糖果双次校验', rounds=2)
        questions = validate(questions)
        with self.s.store.db() as db:
            db.execute("UPDATE console_documents SET value=? WHERE key='questions'", (dumps(questions),))
            db.execute("DELETE FROM console_documents WHERE key='candy_single_v4'")
        store = ConsoleStore(self.tmp.name)
        self.assertEqual(store.document('questions')['value'][0]['rounds'], 1)
        self.assertEqual(store.document('questions')['value'][0]['title'], '糖果测试')
        revision = store.document('questions')['revision']; ConsoleStore(self.tmp.name)
        self.assertEqual(store.document('questions')['revision'], revision)
        questions[0]['prompt'] = 'customized fixture question'
        with store.db() as db:
            db.execute("UPDATE console_documents SET value=? WHERE key='questions'", (dumps(validate(questions)),))
            db.execute("DELETE FROM console_documents WHERE key='candy_single_v4'")
        self.assertEqual(ConsoleStore(self.tmp.name).document('questions')['value'][0]['rounds'], 2)


class SchedulingHttpTests(unittest.TestCase):
    def test_real_http_changes_schedulable_only_and_never_enables_inactive_accounts(self):
        state = deepcopy(ACCOUNT); writes = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def reply(self, value):
                data = json.dumps({'code': 0, 'data': value}).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
            def do_GET(self): self.reply(state)
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                writes.append((self.path, body)); state.update(body); self.reply(state)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            api = Sub2API('http://127.0.0.1:'+str(server.server_port), 'fixture-scheduling-key')
            api.set_schedulable_only(1, False, ACCOUNT); api.set_schedulable_only(1, True, ACCOUNT)
            self.assertEqual(writes, [('/api/v1/admin/accounts/1/schedulable', {'schedulable': False}),
                                      ('/api/v1/admin/accounts/1/schedulable', {'schedulable': True})])
            state['status'] = 'inactive'
            with self.assertRaises(ProbeError): api.set_schedulable_only(1, True, ACCOUNT)
            self.assertEqual(len(writes), 2)
        finally:
            server.shutdown(); thread.join(); server.server_close()


class RecoveryWorkerTests(unittest.TestCase):
    def test_worker_probes_disabled_recovery_account_directly_and_respects_quiet_hours(self):
        from detector.quality_catalog import defaults, validate
        from detector.quality_worker import main
        account = {**ACCOUNT, 'schedulable': False}
        question = validate([next(q for q in defaults() if q['id']=='numeric')])[0]
        snapshot = {'plan': {'value': {'models': {'openai': {'model': 'fixture', 'effort': 'low'}},
                    'selection': {'mode': 'all', 'count': 1}, 'schedule': {'quiet_pause': False}}},
                    'questions': [question], 'targets': [account], 'created_at': time.time(), 'source': 'cooldown',
                    'recovery': {'account_id': 1, 'generation': 'fixture-generation'}}
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            root = Path(folder); (root/'config.json').write_text('{}')
            gateway = Mock(); gateway.account.return_value = deepcopy(account)
            with patch('sys.argv', ['worker', '--root', folder]), patch('detector.upstream.Sub2API', return_value=gateway), patch('detector.monitor.read_admin_key', return_value='fixture-machine-key'), patch('detector.upstream.probe', return_value={'text': '最终答案：323'}) as probe:
                (root/'snapshot.json').write_text(json.dumps(snapshot)); main()
                self.assertEqual(probe.call_count, 1); gateway.set_callable.assert_not_called()
                gateway.set_schedulable_only.assert_not_called()
                snapshot['recovery']['account_id'] = 2
                (root/'snapshot.json').write_text(json.dumps(snapshot)); main()
                self.assertEqual(probe.call_count, 1)
                self.assertEqual(json.loads((root/'results.json').read_text())[0]['error_code'], 'QUARANTINE_RECHECK_BLOCKED')
                snapshot['recovery']['account_id'] = 1
                (root/'snapshot.json').write_text(json.dumps(snapshot))
                with patch('detector.console_service.quiet_paused', return_value=True): main()
                self.assertEqual(probe.call_count, 1)
