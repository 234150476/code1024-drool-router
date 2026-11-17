"""Versioning, public boundaries and scheduling ownership; no model costs."""
from copy import deepcopy
import json
import tempfile
import time
import unittest
import uuid
from unittest.mock import Mock,patch

from detector import config
from detector.console_config import ConsoleError
from detector.console_service import ConsoleService
from detector.console_store import ConsoleStore
from detector.quality_catalog import defaults,validate,select,evaluate,verdict
from detector.quality_isolation import IsolationPolicy,fingerprint
from detector.upstream import ProbeError


class QualityV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=ConsoleStore(self.tmp.name)
        self.accounts=[{'id':i,'name':'private-account-'+str(i),'platform':'openai','type':'apikey',
            'status':'active','schedulable':True,'group_ids':[3],'updated_at':'t0','credentials':{'api_key':'fixture-private-value'}} for i in (1,2)]
        self.store.save_inventory(self.accounts,[{'id':3,'name':'Plus'}],'admin')
        self.router=IsolationPolicy(self.store)
        self.api=Mock();self.api.account_state.side_effect=lambda ident:deepcopy(next(a for a in self.accounts if a['id']==ident))
        self.api.accounts.side_effect=lambda **_:deepcopy(self.accounts)
        self.write=patch.object(self.router,'write',side_effect=self.do_write);self.write.start();self.addCleanup(self.write.stop)
        self.plan=self.store.save_plan({'name':'quality','public_name':'公开分组','enabled':True,
            'scope':{'mode':'accounts','account_ids':[1],'group_ids':[],'exclude_ids':[]},
            'models':{'openai':{'enabled':True,'model':'fixture','protocol':'openai_chat','effort':'low'}},
            'kinds':['numeric'],'schedule':{**self.store.document('settings')['value']['schedule'],'enabled':False},
            'published':True,'show_accounts':False,'show_answers':True,'show_artifacts':True,
            'write_priority':False,'write_callable':False,
            'isolation':{'enabled':True,'failure_rounds':2,'cooldown_minutes':5,'min_available':1}},'admin')

    def do_write(self,api,before,enabled):
        account=next(a for a in self.accounts if a['id']==before['id'])
        self.assertEqual(fingerprint(account),fingerprint(before))
        account.update(schedulable=enabled,updated_at=str(time.monotonic()))
        return deepcopy(account)

    def job(self,status='fail',source='manual'):
        preview=self.store.resolve(self.plan)
        job,_=self.store.create_job(self.plan['id'],uuid.uuid4().hex,'admin',source,preview)
        q=job['snapshot']['questions'][0]
        self.store.import_quality_results(job,[{'id':uuid.uuid4().hex,'account_id':1,'group_ids':[3],
            'slot':time.time(),'duration':1,'finished_at':time.time(),'model':'fixture','platform':'openai','effort':'low',
            'kind':q['id'],'question_type':q['kind'],'title':q['title'],'bank_version':q['version'],
            'status':status,'rounds':[{'round':1,'status':status,'response':'最终答案：323'}]}])
        self.store.update_job(job['id'],status='completed_with_errors' if status=='error' else 'completed')
        return self.store.job(job['id'])

    def observe(self,status='fail',source='manual'):
        job=self.job(status,source);self.router.observe(job,self.api);return job

    def test_snapshot_is_immutable_and_version_changes(self):
        job=self.job('pass');old=job['snapshot']['questions'][0]
        doc=self.store.document('questions');doc['value'][3]['prompt']='计算不同的题，最终答案只写数值。'
        self.store.save_document('questions',doc['value'],doc['revision'],'admin')
        self.assertNotEqual(select(self.store,['numeric'])[0]['version'],old['version'])
        self.assertEqual(self.store.job(job['id'])['snapshot']['questions'][0],old)

    def test_catalog_and_reference_grading(self):
        qs=validate(defaults());self.assertIn('鹈鹕',next(q for q in qs if q['id']=='drawing')['prompt'])
        self.assertEqual(evaluate(next(q for q in qs if q['id']=='fingerprint'),'I am any model')[0],'reference')
        qs[0]['rounds']=4
        with self.assertRaises(ConsoleError):validate(qs)

    def test_round_errors_and_quota_cannot_become_a_pass_or_wrong_answer(self):
        for states,expected in [([], 'error'),(['pass','error'],'error'),(['fail','error'],'error'),
            (['quota'],'quota'),(['pass','unknown'],'unknown'),(['reference'],'reference'),(['pass','pass'],'pass')]:
            self.assertEqual(verdict(states),expected)

    def test_errors_never_isolate_or_count_as_wrong(self):
        self.observe();self.observe('error');self.observe()
        self.assertEqual(self.router.records(),[])
        self.router.write.assert_not_called()

    def test_failure_threshold_cooldown_and_success_recovery(self):
        self.observe();self.assertEqual(self.router.records(),[])
        job=self.observe();self.assertFalse(self.accounts[0]['schedulable'])
        self.assertEqual(self.router.records()[0]['state'],'cooling')
        self.router.observe(job,self.api);self.assertEqual(self.router.write.call_count,1)
        # A fresh service instance retains ownership and the exact state fingerprint.
        restarted=IsolationPolicy(ConsoleStore(self.tmp.name))
        self.assertEqual(restarted.records()[0]['state'],'cooling')
        self.observe('error');self.assertFalse(self.accounts[0]['schedulable'])
        self.observe('pass','cooldown');self.assertTrue(self.accounts[0]['schedulable'])
        self.assertEqual(self.router.records()[0]['state'],'restored')

    def test_external_change_prevents_restoration(self):
        self.observe();self.observe();self.accounts[0]['updated_at']='manually-edited'
        self.observe('pass')
        self.assertFalse(self.accounts[0]['schedulable'])
        self.assertEqual(self.router.records()[0]['state'],'review')
        self.assertEqual(self.router.write.call_count,1)

    def test_last_available_account_is_protected(self):
        self.accounts[1]['schedulable']=False
        self.observe();self.observe()
        self.router.write.assert_not_called();self.assertTrue(self.accounts[0]['schedulable'])

    def test_uncertain_write_and_restart_require_review(self):
        self.router.write.side_effect=ProbeError('ISOLATION_WRITE_UNCONFIRMED','uncertain')
        self.observe();self.observe()
        self.assertEqual(self.router.records()[0]['state'],'review')
        record=self.router.records()[0];record['state']='pending';self.router.save(record,'fixture')
        self.store.recover_jobs()
        self.assertEqual(self.router.records()[0]['state'],'review')

    def test_account_plans_publish_to_real_group_without_account_identity(self):
        from detector.console_public import PublicResults
        self.job('pass');public=PublicResults(self.store)
        group=public.projection()['groups'][0]
        self.assertEqual(group['name'],'Plus')
        rows=public.history(group['id']);self.assertEqual(rows[0]['source'],'admin')
        value=public.record(rows[0]['id']);self.assertIn('323',value['rounds'][0]['response'])
        self.assertNotIn('account_id',json.dumps(value));self.assertNotIn('private-account',json.dumps(value))
        plan=deepcopy(self.plan['value']);plan['published']=False
        self.store.save_plan(plan,'admin',self.plan['id'],self.plan['revision'])
        with self.assertRaises(ConsoleError):public.record(rows[0]['id'])

    def test_user_content_requires_explicit_opt_in_and_revokes(self):
        from detector.console_public import PublicResults
        job=self.store.create_self_job('11',7,'private-key','fixture','low',['drawing'],True,group_id=3,group_name='Plus')
        self.store.update_self_job(job['id'],status='completed',results=[{'title':'作品','kind':'drawing','question_type':'drawing',
            'status':'pass','rounds':[{'response':'<svg></svg>','status':'pass','round':1}]}])
        public=PublicResults(self.store);gid=public.projection()['groups'][0]['id']
        rows=public.history(gid);ident=next(r['id'] for r in rows if r['source']=='user')
        self.assertEqual(public.record(ident)['rounds'],[])
        self.store.update_self_job(job['id'],share_content=True)
        self.assertEqual(public.record(ident)['rounds'][0]['response'],'<svg></svg>')
        self.store.update_self_job(job['id'],public=False)
        with self.assertRaises(ConsoleError):public.record(ident)

    def test_cooldown_timer_targets_only_owned_account(self):
        self.observe();self.observe();record=self.router.records()[0]
        record['due_at']=time.time()-1;self.router.save(record,'fixture')
        service=ConsoleService(self.tmp.name,start=False)
        with patch.object(service,'machine_key',return_value='fixture-machine-key'),patch.object(service,'sync_inventory'):
            service.tick();service.tick()
        jobs=[j for j in self.store.jobs() if j['source']=='cooldown']
        self.assertEqual(len(jobs),1)
        self.assertEqual([a['id'] for a in jobs[0]['snapshot']['targets']],[1])

    def test_manual_success_during_cooldown_does_not_restore_early(self):
        self.observe();self.observe();self.observe('pass')
        self.assertFalse(self.accounts[0]['schedulable'])
        self.assertEqual(self.router.records()[0]['state'],'cooling')

    def test_multiple_failure_plans_must_all_pass_before_recovery(self):
        self.observe();self.observe();first=self.plan
        value=deepcopy(first['value']);value['kinds']=['json'];value['isolation']['failure_rounds']=1
        self.plan=self.store.save_plan(value,'admin')
        self.observe();self.assertEqual(len(self.router.records()[0]['plans']),2)
        self.observe('pass','cooldown');self.assertFalse(self.accounts[0]['schedulable'])
        self.plan=first;self.observe('pass','cooldown');self.assertTrue(self.accounts[0]['schedulable'])

    def test_restart_delays_interrupted_retest_instead_of_replaying(self):
        self.observe();self.observe();record=self.router.records()[0]
        record['due_at']=time.time()-1;self.router.save(record,'fixture')
        preview=self.store.resolve(self.plan)
        self.store.create_job(self.plan['id'],uuid.uuid4().hex,'scheduler','cooldown',preview,target_ids={1})
        self.store.recover_jobs()
        self.assertGreater(self.router.records()[0]['due_at'],time.time()+200)
        self.assertFalse(self.accounts[0]['schedulable'])

    def test_native_write_only_changes_schedulable_and_requires_readback(self):
        writer=IsolationPolicy(self.store);before=deepcopy(self.accounts[0])
        after={**before,'schedulable':False,'updated_at':'t1'}
        self.api.base='http://fixture.invalid';self.api.admin_headers.return_value={'x-api-key':'fixture-key'}
        self.api.account_state.side_effect=[before,after]
        with patch('detector.quality_isolation.requests.Session') as factory:
            session=factory.return_value.__enter__.return_value
            session.post.return_value.status_code=200;session.post.return_value.json.return_value={'code':0}
            result=writer.write(self.api,before,False)
            self.assertIs(result['schedulable'],False)
            self.assertEqual(session.post.call_args.kwargs['json'],{'schedulable':False})
            session.put.assert_not_called()
        self.api.account_state.side_effect=[{**before,'status':'inactive'}]
        with patch('detector.quality_isolation.requests.Session') as factory:
            with self.assertRaises(ProbeError):writer.write(self.api,before,True)
            factory.assert_not_called()


if __name__=='__main__':unittest.main()
