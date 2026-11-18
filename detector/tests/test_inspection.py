"""Sampling, quiet hours and credentials are exercised without model spending."""
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from detector import config
from detector.account_selection import inspection_candidates, recent_active
from detector.console_config import ConsoleError, schedule
from detector.console_service import ConsoleService, schedule_slot
from detector.credential_snapshot import read
from detector.upstream import OAUTH_QUOTA_ERROR, Sub2API, ProbeError


def at(clock):
    return datetime.fromisoformat('2026-11-17T'+clock).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()


class InspectionTests(unittest.TestCase):
    def setUp(self):
        self.spec={'enabled':True,'timezone':'Asia/Shanghai','regular_minutes':60,'quiet_start':'22:00','quiet_end':'09:00','quiet_minutes':60,'quiet_pause':True}

    def test_thirteen_daytime_slots_and_no_night_catch_up(self):
        for hour in range(24):
            slot=schedule_slot(self.spec,at('%02d:30'%hour))
            if 9<=hour<22:self.assertEqual(slot,at('%02d:00'%hour))
            else:self.assertIsNone(slot)
        self.assertEqual(schedule_slot(self.spec,at('09:00')),at('09:00'))
        self.assertIsNone(schedule_slot(self.spec,at('22:00')))
        self.assertEqual(schedule_slot(self.spec,at('21:59')),at('21:00'))

    def test_old_interval_schedules_still_work_and_pause_requires_boolean(self):
        old={k:v for k,v in self.spec.items() if k!='quiet_pause'}
        self.assertIsNotNone(schedule_slot(schedule(old),at('22:00')))
        with self.assertRaises(ConsoleError):schedule({**self.spec,'quiet_pause':'true'})

    def test_recent_callable_account_wins_and_paused_accounts_never_win(self):
        def account(ident,used,**kwargs):return {'id':ident,'status':'active','schedulable':True,'last_used_at':used,**kwargs}
        rows=[account(1,90),account(2,99,schedulable=False),account(3,95),account(4,100,status='error'),account(5,None),account(6,98,rate_limit_reset_at=200)]
        selected,skipped=recent_active(rows,1,now=100)
        self.assertEqual([a['id'] for a in selected],[3]);self.assertEqual(len(skipped),5)
        self.assertEqual(recent_active([account(1,None)],1,now=100)[0],[])

    def test_no_usage_history_starts_with_highest_priority(self):
        rows = [{'id': i, 'priority': priority, 'status': 'active', 'schedulable': schedulable}
                for i, priority, schedulable in ((1, 0, False), (2, 5, True), (3, 1, True), (4, 3, True))]
        selected, skipped = inspection_candidates(rows, 2)
        self.assertEqual([row['id'] for row in selected], [3, 4])
        self.assertEqual({row['id'] for row in skipped}, {1, 2})

    def test_snapshot_supplies_credentials_and_quota_without_export_permission(self):
        with tempfile.TemporaryDirectory() as root:
            file=Path(root)/'accounts.json'
            original={'id':1,'name':'Plus account','type':'oauth','platform':'openai','status':'active','schedulable':True,'group_ids':[3]}
            entry={**original,'group_ids':[3,9],'credentials':{'access_token':'fixture-private-value'},'last_used_at':time.time()-1,'extra':{},'_quota_policy':{'account_scheduling_threshold':100}}
            file.write_text(json.dumps({'version':1,'generated_at':time.time(),'accounts':{'1':entry}}))
            api=Sub2API('http://unused.invalid','fixture-machine-key')
            with patch.dict('os.environ',{'SUB2API_ACCOUNT_SNAPSHOT_FILE':str(file)}),patch.object(api,'get',return_value=deepcopy(original)) as get:
                value=api.account(1)
                self.assertEqual(value['credentials']['access_token'],'fixture-private-value')
                self.assertEqual(value['group_ids'],[3,9])
                self.assertEqual(get.call_args_list[0].args,('/accounts/1',))
                self.assertEqual(get.call_count,1)
            file.write_text(json.dumps({'version':1,'generated_at':time.time()-601,'accounts':{'1':entry}}))
            with self.assertRaises(ProbeError):read(file)

    def test_group_change_fails_closed(self):
        from detector.credential_snapshot import entry
        with tempfile.TemporaryDirectory() as root:
            file=Path(root)/'accounts.json'
            account={'id':1,'name':'Plus','type':'oauth','platform':'openai','group_ids':[3]}
            file.write_text(json.dumps({'version':1,'generated_at':time.time(),'accounts':{'1':account}}))
            with self.assertRaises(ProbeError):entry(file,{**account,'group_ids':[2]})

    def test_night_tick_makes_no_inventory_or_model_request(self):
        with tempfile.TemporaryDirectory() as root:
            service=ConsoleService(root,start=False)
            service.store.save_inventory([{'id':1,'name':'Plus','platform':'openai','type':'apikey','status':'active','schedulable':True,'last_used_at':time.time(),'group_ids':[3]}],[{'id':3,'name':'Plus','platform':'openai'}],'admin')
            from detector.inspection import save
            with patch.object(service,'machine_key',return_value='fixture-machine-key'):
                plan=save(service,{'group_id':3,'enabled':True,'model':'fixture','effort':'high','question_ids':['drawing'],'questions':[],'schedule':self.spec},'admin')
                with patch('detector.console_service.time.time',return_value=at('22:30')),patch.object(service,'sync_inventory') as sync:
                    service.tick();self.assertEqual(service.store.jobs(),[]);sync.assert_not_called()
                value=service.store.plan(plan['id'])['value'];value['isolation']={'enabled':True,'failure_rounds':2,'cooldown_minutes':60,'min_available':1}
                with self.assertRaises(ConsoleError):service.save_plan(value,'admin')


class InspectionFallbackTests(unittest.TestCase):
    def setUp(self):
        from detector.inspection import save
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.service = ConsoleService(folder.name, start=False)
        now = time.time()
        self.accounts = {i: {'id': i, 'name': 'fixture-account-'+str(i), 'type': 'apikey',
                            'platform': 'openai', 'status': 'active', 'schedulable': True,
                            'priority': priority, 'last_used_at': now-age, 'group_ids': [group]}
                         for i, priority, age, group in ((1, 8, 10, 3), (2, 1, 50, 3),
                                                         (3, 2, 20, 3), (4, 0, 5, 4), (5, 3, 15, 3))}
        self.groups = [{'id': i, 'name': 'fixture group '+str(i), 'platform': 'openai'} for i in (3, 4)]
        self.service.store.save_inventory(list(self.accounts.values()), self.groups, 'admin')
        self.payload = {'group_id': 3, 'enabled': True, 'model': 'fixture', 'effort': 'low',
                        'question_ids': ['numeric', 'choice'], 'questions': [],
                        'schedule': {**self.service.store.document('settings')['value']['schedule'], 'enabled': False}}
        self.plan = save(self.service, self.payload, 'admin')

    def run_job(self, probe, quota=None, read_account=None):
        from detector.quality_worker import main
        store = self.service.store
        plan = store.plan(self.plan['id'])
        preview = store.resolve(plan)
        job, _ = store.create_job(plan['id'], uuid.uuid4().hex, 'admin', 'manual', preview)

        def run(args, *, env, **kwargs):
            with patch.dict(os.environ, env, clear=True), patch('sys.argv', ['worker', *args[3:]]):
                main()
            return SimpleNamespace(returncode=0)

        gateway = SimpleNamespace(account=read_account or (lambda i: deepcopy(self.accounts[i])))
        with (patch.object(self.service, 'machine_key', return_value='fixture-machine-key'),
              patch('detector.console_service.subprocess.run', side_effect=run),
              patch('detector.upstream.Sub2API', return_value=gateway),
              patch('detector.upstream.oauth_quota_state', side_effect=quota or (lambda a: {'limited': False})),
              patch('detector.upstream.probe', side_effect=probe)):
            self.service.execute(job)
        return self.service.job_summary(store.job(job['id']))

    def test_quota_and_partial_failure_retry_whole_suite_in_priority_order(self):
        seen = []

        def probe(account, kind, *, prompt, **kwargs):
            answer_is_numeric = prompt.startswith('计算')
            seen.append((account['id'], answer_is_numeric))
            if account['id'] == 2 and not answer_is_numeric:
                raise ProbeError('HTTP_429', 'limited')
            return {'text': '最终答案：323' if answer_is_numeric else '最终答案：3'}

        job = self.run_job(probe, quota=lambda account: {'limited': account['id'] == 1})
        self.assertEqual(seen, [(2, True), (2, False), (3, True), (3, False)])
        self.assertEqual(job['status'], 'completed')
        self.assertEqual([(r['account_id'], r['error_code']) for r in job['attempts']],
                         [(1, OAUTH_QUOTA_ERROR), (2, 'HTTP_429'), (3, None)])
        self.assertEqual({r['account_id'] for r in self.service.store.results()}, {3})
        history = self.service.public.history(self.service.store.public_id('group', 'g3'))
        self.assertEqual(len(history), 2)
        self.assertNotIn('account_id', json.dumps(history))

    def test_exhaustion_produces_one_unavailable_result_and_respects_custom_limit(self):
        from detector.inspection import save
        self.plan = save(self.service, {**self.payload, 'max_account_attempts': 2}, 'admin',
                         self.plan['id'], self.plan['revision'])
        seen = []

        def probe(account, *args, **kwargs):
            seen.append(account['id'])
            raise ProbeError('HTTP_503', 'upstream unavailable')

        job = self.run_job(probe)
        self.assertEqual(seen, [1, 2])
        self.assertEqual(job['status'], 'completed_with_errors')
        self.assertEqual(job['status_counts'], {'unavailable': 1})
        history = self.service.public.history(self.service.store.public_id('group', 'g3'))
        self.assertEqual([r['status'] for r in history], ['unavailable'])
        self.assertFalse(history[0]['content_available'])

    def test_definite_wrong_answer_is_not_replaced_after_later_error(self):
        seen = []

        def probe(account, kind, *, prompt, **kwargs):
            seen.append(account['id'])
            if not prompt.startswith('计算'):
                raise ProbeError('NETWORK_ERROR', 'upstream disconnected')
            return {'text': '最终答案：324'}

        job = self.run_job(probe)
        self.assertEqual(seen, [1, 1])
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(job['status_counts'], {'fail': 1, 'error': 1})

    def test_gateway_denial_stops_without_switching(self):
        def read_account(account_id):
            self.assertEqual(account_id, 1)
            raise ProbeError('ACCOUNT_SYNC_HTTP_403', 'gateway denied')

        job = self.run_job(lambda *args, **kwargs: self.fail('probe called'), read_account=read_account)
        self.assertEqual(job['status'], 'completed_with_errors')
        self.assertEqual([(r['account_id'], r['error_code']) for r in job['attempts']], [(1, 'ACCOUNT_SYNC_HTTP_403')])

    def test_no_eligible_accounts_has_public_outcome_and_skips_worker(self):
        self.service.store.save_inventory([{**row, 'schedulable': False} for row in self.accounts.values()], self.groups, 'admin')
        job = self.run_job(lambda *args, **kwargs: self.fail('probe called'))
        self.assertEqual(job['status'], 'completed_with_errors')
        self.assertEqual(job['attempts'], [])
        history = self.service.public.history(self.service.store.public_id('group', 'g3'))
        self.assertEqual([r['status'] for r in history], ['unavailable'])
        self.assertEqual(self.service.public.record(history[0]['id'])['rounds'], [])

    def test_retention_prunes_old_rounds_and_artifacts_by_group(self):
        from detector.inspection import save
        self.plan = save(self.service, {**self.payload, 'max_saved_rounds': 1}, 'admin',
                         self.plan['id'], self.plan['revision'])
        old = self.run_job(lambda *a, **kw: {'text': '最终答案：323' if kw['prompt'].startswith('计算') else '最终答案：3'})
        store = self.service.store
        old_dir = store.private / 'jobs' / old['id']
        self.assertTrue(old_dir.exists())
        self.assertEqual(len(store.results()), 2)
        current = self.run_job(lambda *a, **kw: {'text': '最终答案：323' if kw['prompt'].startswith('计算') else '最终答案：3'})
        other = save(self.service, {**self.payload, 'group_id': 4}, 'admin')
        self.assertEqual(other['max_saved_rounds'], 20)
        self.plan = other
        other_job = self.run_job(lambda *a, **kw: {'text': '最终答案：323' if kw['prompt'].startswith('计算') else '最终答案：3'})
        self.service.cleanup()
        self.service.cleanup()
        self.assertFalse(old_dir.exists())
        self.assertEqual(store.quarantine_evidence(old['id'], 1), [])
        self.assertEqual({r['job_id'] for r in store.results()}, {current['id'], other_job['id']})
        for gid in ('g3', 'g4'):
            history = self.service.public.history(store.public_id('group', gid))
            self.assertEqual({row['kind'] for row in history}, {'numeric', 'choice'})

    def test_empty_inspection_history_is_pruned_with_same_limit(self):
        from detector.inspection import save
        self.plan = save(self.service, {**self.payload, 'max_saved_rounds': 1}, 'admin',
                         self.plan['id'], self.plan['revision'])
        self.service.store.save_inventory([], self.groups, 'admin')
        old = self.run_job(lambda *a, **kw: self.fail('probe called'))
        self.run_job(lambda *a, **kw: self.fail('probe called'))
        self.service.cleanup()
        history = self.service.public.history(self.service.store.public_id('group', 'g3'))
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['status'], 'unavailable')
        self.assertEqual(self.service.store.job(old['id'])['results_pruned'], 1)
