"""Run versioned catalog snapshots in an isolated process; never write routing."""
import argparse
import json
import os
from pathlib import Path
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from .account_selection import unavailable


ACCOUNT_ERRORS = frozenset({
    'HTTP_429', 'HTTP_408', 'CONNECT_TIMEOUT', 'REQUEST_TIMEOUT',
    'FIRST_RESPONSE_TIMEOUT', 'STREAM_IDLE_TIMEOUT', 'STREAM_INTERRUPTED', 'NETWORK_ERROR',
    'AUTHENTICATION_FAILED', 'ACCOUNT_NOT_ACTIVE', 'ACCOUNT_INACTIVE', 'ACCOUNT_EXCLUDED',
    'ACCOUNT_IDENTITY_MISMATCH', 'ACCOUNT_SNAPSHOT_IDENTITY_CHANGED', 'ACCOUNT_SYNC_HTTP_404',
    'CREDENTIAL_UNAVAILABLE', 'MISSING_ACCESS_TOKEN', 'MODEL_MAPPING_MISMATCH',
    'PROXY_IDENTITY_MISMATCH',
})


def can_try_another(code):
    from .upstream import OAUTH_QUOTA_ERROR
    return (code == OAUTH_QUOTA_ERROR or code in ACCOUNT_ERRORS or
            (len(code) == 8 and code.startswith('HTTP_5') and code[-2:].isdigit()))


def save_json(root, name, value):
    file = root / (name + '.tmp')
    file.write_text(json.dumps(value, ensure_ascii=False))
    file.chmod(0o600)
    file.replace(root / (name + '.json'))


def run_candidates(root, snapshot, run, persist):
    from .account_quarantine import conclusion

    attempts = []
    targets = snapshot['targets']
    for index, target in enumerate(targets):
        rows, error, stopped = run(target, stop_on_error=True)
        answered_wrong = conclusion(snapshot['questions'], rows) == 'fail'
        retry = error is not None and can_try_another(error) and not answered_wrong
        if stopped:
            outcome = 'paused'
        elif retry:
            outcome = 'retrying' if index + 1 < len(targets) else 'unavailable'
        else:
            outcome = 'error' if error and not answered_wrong else 'completed'

        if rows or stopped:
            attempts.append({'account_id': target['id'], 'name': target['name'],
                             'priority': target.get('priority'), 'error_code': error,
                             'status': 'unavailable' if retry else outcome})
        save_json(root, 'inspection-attempts', {'outcome': outcome, 'accounts': attempts})
        if outcome == 'retrying':
            continue
        if outcome == 'unavailable':
            # Publish one availability result for the entire round. Partial answers
            # from abandoned attempts must not contribute to quality statistics.
            rows = [{**rows[-1], 'status': 'unavailable', 'rounds': []}]
        elif outcome == 'error':
            rows = [{**rows[-1], 'rounds': [rows[-1]['rounds'][-1]]}]
        elif answered_wrong and error:
            for row in rows:
                if conclusion(snapshot['questions'], [row]) == 'fail':
                    row['status'] = 'fail'
        elif stopped:
            rows = []
        for row in rows:
            persist(row)
        break


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True)
    root=Path(parser.parse_args().root)
    os.environ['DROOL_CONFIG']=str(root/'config.json')
    os.environ['DROOL_WORKER_SNAPSHOT']='1'
    from . import config
    from .quality_catalog import evaluate,verdict
    from .upstream import Sub2API,ProbeError,probe,oauth_quota_state,redact,client_info,secret_values,OAUTH_QUOTA_ERROR
    from .account_quarantine import available, same_identity
    from .monitor import read_admin_key
    snapshot=json.loads((root/'snapshot.json').read_text())
    api=Sub2API(config.get('base_url'),read_admin_key(os.environ.get('SUB2API_ADMIN_KEY_FILE','')))
    rows=[];lock=threading.Lock()
    def persist(row):
        with lock:
            rows.append(row)
            public=root/'public';public.mkdir(exist_ok=True)
            detail=public/(row['id']+'.json');detail.write_text(json.dumps({'response':'\n\n'.join(r.get('response','') for r in row['rounds'])},ensure_ascii=False));detail.chmod(0o600)
            if row['question_type']=='drawing' and row['status']=='pass':
                art=public/(row['id']+'.html');art.write_text(row['rounds'][-1]['response']);art.chmod(0o600)
            save_json(root, 'results', rows)
    def run(target, stop_on_error=False):
        platform=target['platform'];spec=snapshot['plan']['value']['models'][platform]
        results = []
        for q in snapshot['questions']:
            start=time.monotonic();rounds=[];group_ids=target['group_ids']
            for index in range(q['rounds']):
                if (root/'stop').exists(): return results, None, True
                if snapshot.get('source') in ('timer', 'cooldown'):
                    from .console_service import quiet_paused
                    if quiet_paused(snapshot['plan']['value']['schedule']):
                        return results, None, True
                try:
                    account=api.account(target['id'])
                    if not same_identity(account, target):
                        raise ProbeError('ACCOUNT_IDENTITY_MISMATCH','账号平台或分组已变化，请重新同步')
                    if snapshot.get('source') == 'cooldown':
                        recovery = snapshot.get('recovery', {})
                        if (recovery.get('account_id') != target['id'] or not recovery.get('generation') or
                                not available(account) or account.get('schedulable') is not False):
                            raise ProbeError('QUARANTINE_RECHECK_BLOCKED', '复测账号身份或状态已变化，未请求模型')
                    if snapshot['plan']['value'].get('selection', {}).get('mode') == 'recent_active' and unavailable(account, time.time()):
                        raise ProbeError('ACCOUNT_NOT_ACTIVE','抽样账号已变为不可调度，本次未请求模型')
                    if oauth_quota_state(account)['limited']:
                        raise ProbeError(OAUTH_QUOTA_ERROR,'账号限额，未请求模型')
                    reply=probe(account,'drawing' if q['kind']=='drawing' else 'candy',
                        timeout=config.get('budgets.drawing_platform_seconds',{}).get(platform,config.get('budgets.default_seconds',300)) if q['kind']=='drawing' else config.get('budgets.default_seconds',300),prompt=q['prompt'],
                        instructions=q['instructions'],request_id=uuid.uuid4().hex)
                    status,answer=evaluate(q,reply['text'])
                    rounds.append({'round':index+1,'status':status,'answer':answer,
                        'response':redact(reply['text'],list(secret_values(account))+[account['name']])[:100000],
                        'tokens':reply.get('tokens',{}),'client':client_info(platform)})
                except ProbeError as exc:
                    rounds.append({'round':index+1,'status':'quota' if exc.code==OAUTH_QUOTA_ERROR else 'error','error_code':exc.code,'error':redact(exc.message,errors=True)[:600]})
                    break
            states=[r['status'] for r in rounds]
            status=verdict(states)
            row = {'id':uuid.uuid4().hex,'account_id':target['id'],'group_ids':group_ids,
                'platform':platform,'model':spec['model'],'effort':spec['effort'],'kind':q['id'],
                'question_type':q['kind'],'title':q['title'],'bank_version':q['version'],
                'question':q,'status':status,'rounds':rounds,'slot':snapshot['created_at'],
                'duration':round(time.monotonic()-start,2),'finished_at':time.time(),
                'error_code':next((r.get('error_code') for r in rounds if r.get('error_code')),None)}
            if stop_on_error:
                results.append(row)
                if row['error_code']:
                    return results, row['error_code'], False
            else:
                persist(row)
        return results, None, False

    if snapshot.get('source') in ('manual', 'timer') and snapshot.get('preview', {}).get('max_account_attempts'):
        run_candidates(root, snapshot, run, persist)
    else:
        with ThreadPoolExecutor(max_workers=config.get('console.concurrency',2)) as pool:
            list(pool.map(run,snapshot['targets']))


if __name__=='__main__': main()
