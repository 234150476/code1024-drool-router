#!/usr/bin/env python3
"""Export only an authorized group using the gateway's normal administrator APIs.

Run on the gateway host. The login file remains root-owned on that host and is
never mounted into the inspection container. Any step-up denial stops export.
"""
import argparse
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def export(base, login_file, group_id, output):
    headers = {'Accept':'application/json','Content-Type':'application/json',
               'Origin':'https://api.code1024.cc','User-Agent':'code1024-inspection-exporter'}
    def call(path, body=None):
        request = Request(base.rstrip('/')+path, headers=headers,
                          data=json.dumps(body).encode() if body is not None else None)
        try:
            with urlopen(request, timeout=30) as response:
                value = json.load(response)
        except HTTPError as exc:
            raise RuntimeError('GATEWAY_HTTP_'+str(exc.code)) from None
        if value.get('code',0) != 0:
            raise RuntimeError('GATEWAY_REJECTED')
        return value.get('data',value)
    login = json.loads(Path(login_file).read_text())
    authenticated = call('/api/v1/auth/login', {'email':login['email'],'password':login['password']})
    headers['Authorization'] = 'Bearer '+authenticated['access_token']
    rows = []
    for page in range(1,51):
        value = call('/api/v1/admin/accounts?group='+str(group_id)+'&page_size=100&page='+str(page))
        rows.extend(value['items'])
        if len(rows)==value['total']:break
        if not value['items'] or len(rows)>value['total']:raise RuntimeError('INCOMPLETE_INVENTORY')
    else: raise RuntimeError('INVENTORY_LIMIT')
    if any(group_id not in row.get('group_ids',[]) for row in rows):
        raise RuntimeError('GROUP_SCOPE_MISMATCH')
    settings = call('/api/v1/admin/settings')
    advanced = call('/api/v1/admin/ops/advanced-settings')
    quota = {'account_scheduling_threshold':settings['account_scheduling_thresholds'].get('openai'),
             **{key:advanced['openai_account_quota_auto_pause'].get(key) for key in ('default_threshold_5h','default_threshold_7d')}}
    accounts = {}
    for row in rows:
        ident = row['id']
        state = call('/api/v1/admin/accounts/'+str(ident))
        if group_id not in state.get('group_ids',[]):raise RuntimeError('GROUP_CHANGED')
        bundle = call('/api/v1/admin/accounts/data?include_proxies=true&ids='+str(ident))
        if len(bundle.get('accounts',[]))!=1:raise RuntimeError('EXPORT_INCOMPLETE')
        raw = bundle['accounts'][0]
        if raw.get('name')!=state['name'] or raw.get('platform')!=state['platform']:raise RuntimeError('IDENTITY_CHANGED')
        entry = {key:state.get(key) for key in ('name','platform','type','group_ids','last_used_at','extra','proxy_id')}
        entry['credentials'] = raw['credentials']
        entry['_quota_policy'] = quota
        if raw.get('proxy_key'):
            proxies=[p for p in bundle.get('proxies',[]) if p.get('proxy_key')==raw['proxy_key']]
            if len(proxies)!=1:raise RuntimeError('PROXY_CHANGED')
            entry['proxy']=proxies[0]
        accounts[str(ident)] = entry
    output=Path(output)
    output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temporary=output.with_suffix('.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as file:
        json.dump({'version':1,'generated_at':time.time(),'accounts':accounts},file,ensure_ascii=False)
        file.flush();os.fsync(file.fileno())
    os.chmod(temporary,0o600)
    if output.exists():
        metadata=output.stat();os.chown(temporary,metadata.st_uid,metadata.st_gid)
    temporary.replace(output)
    print(json.dumps({'snapshot_updated':True,'group_id':group_id,'accounts':len(accounts)}))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--gateway',default='http://127.0.0.1:18080')
    parser.add_argument('--login-file',required=True)
    parser.add_argument('--group-id',required=True,type=int)
    parser.add_argument('--output',required=True)
    arguments=parser.parse_args()
    try:export(arguments.gateway,arguments.login_file,arguments.group_id,arguments.output)
    except Exception as exc:
        # Never print bodies, credentials or session tokens on exporter failure.
        print(json.dumps({'snapshot_updated':False,'error':str(exc) if isinstance(exc,RuntimeError) else type(exc).__name__}))
        raise SystemExit(1)
