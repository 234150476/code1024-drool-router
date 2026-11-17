"""Versioned questions and public result boundaries without routing side effects."""
from copy import deepcopy
import json
import tempfile
import time
import unittest
import uuid
from detector.console_config import ConsoleError
from detector.console_store import ConsoleStore
from detector.quality_catalog import defaults,validate,select,evaluate,verdict


class QualityV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=ConsoleStore(self.tmp.name)
        self.store.save_inventory([{'id':1,'name':'private-account-1','platform':'openai','type':'apikey','status':'active','group_ids':[3]}],[{'id':3,'name':'Plus'}],'admin')
        self.plan=self.store.save_plan({'name':'quality','public_name':'公开分组','enabled':True,
            'scope':{'mode':'accounts','account_ids':[1],'group_ids':[],'exclude_ids':[]},
            'models':{'openai':{'enabled':True,'model':'fixture','protocol':'openai_chat','effort':'low'}},
            'kinds':['numeric'],'schedule':{**self.store.document('settings')['value']['schedule'],'enabled':False},
            'published':True,'show_accounts':False,'show_answers':True,'show_artifacts':True,
            'write_priority':False,'write_callable':False},'admin')

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
