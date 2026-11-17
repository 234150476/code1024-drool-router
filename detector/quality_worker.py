"""Run versioned catalog snapshots in an isolated process; never write routing."""
import argparse
import json
import os
from pathlib import Path
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True)
    root=Path(parser.parse_args().root)
    os.environ['DROOL_CONFIG']=str(root/'config.json')
    os.environ['DROOL_WORKER_SNAPSHOT']='1'
    from . import config
    from .quality_catalog import evaluate,verdict
    from .upstream import Sub2API,ProbeError,probe,oauth_quota_state,redact,client_info,secret_values,OAUTH_QUOTA_ERROR
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
            file=root/'results.tmp';file.write_text(json.dumps(rows,ensure_ascii=False));file.chmod(0o600)
            file.replace(root/'results.json')
    def run(target):
        platform=target['platform'];spec=snapshot['plan']['value']['models'][platform]
        for q in snapshot['questions']:
            if (root/'stop').exists(): return
            start=time.monotonic();rounds=[];group_ids=target['group_ids']
            for index in range(q['rounds']):
                if (root/'stop').exists(): return
                try:
                    account=api.account(target['id'])
                    group_ids=account.get('group_ids',[])
                    if account['platform']!=platform or set(group_ids)!=set(target['group_ids']):
                        raise ProbeError('ACCOUNT_IDENTITY_MISMATCH','账号平台或分组已变化，请重新同步')
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
            persist({'id':uuid.uuid4().hex,'account_id':target['id'],'group_ids':group_ids,
                'platform':platform,'model':spec['model'],'effort':spec['effort'],'kind':q['id'],
                'question_type':q['kind'],'title':q['title'],'bank_version':q['version'],
                'question':q,'status':status,'rounds':rounds,'slot':snapshot['created_at'],
                'duration':round(time.monotonic()-start,2),'finished_at':time.time(),
                'error_code':next((r.get('error_code') for r in rounds if r.get('error_code')),None)})
    with ThreadPoolExecutor(max_workers=config.get('console.concurrency',2)) as pool:
        list(pool.map(run,snapshot['targets']))


if __name__=='__main__': main()
