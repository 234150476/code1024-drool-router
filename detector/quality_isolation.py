"""Persistent isolation ownership and conservative recovery across all jobs."""
import json
import time

import requests

from .console_config import ConsoleError
from .console_store import dumps
from .upstream import ProbeError, oauth_quota_state


def policy(plan):
    return plan.get('isolation') or {'enabled':False,'failure_rounds':2,'cooldown_minutes':60,'min_available':1}


def fingerprint(account):
    # Any external edit after our confirmed write makes restoration a manual decision.
    return {k:account.get(k) for k in ('id','platform','type','status','schedulable','updated_at','group_ids')}


class IsolationPolicy:
    def __init__(self,store): self.store=store

    def records(self):
        with self.store.db() as db:
            return [json.loads(r['value']) for r in db.execute('SELECT value FROM quality_isolations')]

    def save(self,value,action):
        with self.store.db() as db:
            db.execute('INSERT OR REPLACE INTO quality_isolations VALUES (?,?)',(value['account_id'],dumps(value)))
            self.store.audit(db,'quality-policy',action,str(value['account_id']))

    @staticmethod
    def matches(account,plan):
        s=plan['scope']; ident=account['id']
        return (ident not in s['exclude_ids'] and account.get('status')=='active' and
            (bool(set(s['group_ids'])&set(account.get('group_ids',[]))) if s['mode']=='groups' else ident in s['account_ids']))

    def write(self,api,before,enabled):
        # Do not use legacy set_callable: it can also change status to active.
        latest=api.account_state(before['id'])
        if fingerprint(latest)!=fingerprint(before) or latest.get('status')!='active' or oauth_quota_state(latest)['limited']:
            raise ProbeError('ISOLATION_STATE_CHANGED','账号已变化或达到限额，未修改调度')
        try:
            with requests.Session() as session:
                session.trust_env=False
                r=session.post(api.base+'/api/v1/admin/accounts/'+str(before['id'])+'/schedulable',
                    headers=api.admin_headers(),json={'schedulable':enabled},timeout=(5,30),allow_redirects=False)
                if r.status_code!=200 or r.json().get('code',0)!=0:
                    raise ProbeError('ISOLATION_WRITE_UNCONFIRMED','调度写入未确认，需要管理员核对')
        except (requests.RequestException,ValueError):
            raise ProbeError('ISOLATION_WRITE_UNCONFIRMED','调度回执不明确，需要管理员核对') from None
        after=api.account_state(before['id'])
        if after.get('schedulable') is not enabled or any(after.get(k)!=before.get(k) for k in ('status','platform','type','group_ids')):
            raise ProbeError('ISOLATION_READBACK_FAILED','调度回读不一致，需要管理员核对')
        return after

    def observe(self,job,api):
        if job['status'] not in ('completed','completed_with_errors'): return
        plan=self.store.plan(job['plan_id'])
        if plan['revision']!=job['snapshot']['plan']['revision'] or not plan['value']['enabled']: return
        p=policy(plan['value'])
        if not p['enabled']: return
        rows=[r['value'] for r in self.store.results() if r['job_id']==job['id']]
        questions=[q for q in job['snapshot'].get('questions',[]) if q['kind'] in ('number','choice','json')]
        if not questions: return
        signature=dumps([plan['revision'],[(q['id'],q['version']) for q in questions]])
        owned={r['account_id']:r for r in self.records()}
        for target in job['snapshot']['targets']:
            ident=target['id']
            values=[r for r in rows if r['account_id']==ident and r['kind'] in {q['id'] for q in questions}]
            complete=len(values)==len(questions)
            failed=complete and any(r['status']=='fail' for r in values) and all(r['status'] in ('pass','fail') for r in values)
            passed=complete and all(r['status']=='pass' for r in values)
            with self.store.db() as db:
                previous=db.execute('SELECT * FROM quality_evidence WHERE account_id=? AND plan_id=?',(ident,plan['id'])).fetchone()
                if previous and previous['last_job']==job['id']: continue
                streak=(previous['streak'] if previous and previous['signature']==signature else 0)+1 if failed else 0
                db.execute('INSERT OR REPLACE INTO quality_evidence VALUES (?,?,?,?,?)',(ident,plan['id'],streak,job['id'],signature))
            record=owned.get(ident)
            try:
                before=api.account_state(ident)
                if not self.matches(before,plan['value']):
                    raise ProbeError('ISOLATION_SCOPE_CHANGED','账号退出范围或状态变化')
                if record and record['state']=='cooling':
                    if fingerprint(before)!=record['observed']:
                        raise ProbeError('ISOLATION_EXTERNAL_CHANGE','隔离后账号被外部修改，不自动恢复')
                    if plan['id'] not in record['plans']:
                        if not failed: continue
                        record['plans'][plan['id']]={'revision':plan['revision'],'signature':signature,'passed':False}
                    if record['plans'][plan['id']]['revision']!=plan['revision'] or record['plans'][plan['id']].get('signature')!=signature:
                        raise ProbeError('ISOLATION_CONFIG_CHANGED','隔离计划已修改，需要管理员核对')
                    if passed and time.time()<record['due_at'] and job['source']!='cooldown': continue
                    record['plans'][plan['id']]['passed']=passed
                    record['due_at']=time.time()+p['cooldown_minutes']*60
                    for pid in record['plans']:
                        current=self.store.plan(pid)
                        if not current['value']['enabled'] or not policy(current['value'])['enabled'] or current['revision']!=record['plans'][pid]['revision']:
                            raise ProbeError('ISOLATION_CONFIG_CHANGED','隔离计划已停用或修改，需要管理员核对')
                    if all(v['passed'] for v in record['plans'].values()):
                        self.write(api,before,True)
                        record['state']='restored';record['restored_at']=time.time()
                    self.save(record,'quality_retest');continue
                if record and record['state'] in ('review','pending'): continue
                if not failed or streak<p['failure_rounds'] or before.get('schedulable') is not True: continue
                # Check every actual group, including groups not selected in this plan.
                inventory=api.accounts(all_accounts=True)
                if any(sum(a.get('status')=='active' and a.get('schedulable') is True and a['id']!=ident and not oauth_quota_state(a)['limited']
                           for a in inventory if gid in a.get('group_ids',[])) < p['min_available']
                       for gid in before.get('group_ids',[])):
                    with self.store.db() as db: self.store.audit(db,'quality-policy','minimum_available_protected',str(ident))
                    continue
                record={'account_id':ident,'state':'pending','plans':{plan['id']:{'revision':plan['revision'],'signature':signature,'passed':False}},
                    'isolated_at':time.time(),'due_at':time.time()+p['cooldown_minutes']*60,
                    'before':fingerprint(before),'observed':None,'message':None}
                self.save(record,'isolation_intent')
                after=self.write(api,before,False)
                record.update(state='cooling',observed=fingerprint(after))
                self.save(record,'isolation_confirmed');owned[ident]=record
            except (ProbeError,ConsoleError) as exc:
                if record and record['state'] in ('pending','cooling'):
                    record.update(state='review',message=getattr(exc,'code',None) or 'POLICY_REVIEW_REQUIRED')
                    self.save(record,'isolation_review_required')

    def acknowledge(self,ident,actor):
        # Releasing ownership never changes gateway state; subsequent manual
        # enablement in code1024 remains an administrator action.
        record=next((r for r in self.records() if r['account_id']==ident),None)
        if not record: raise ConsoleError('隔离记录不存在',404)
        record.update(state='released',message='管理员已接管；服务不再自动恢复该账号')
        self.save(record,'isolation_released_by_'+actor)
        return record
