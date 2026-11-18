"""Public projection is an explicit field allowlist, never an admin-state copy."""
import json
from pathlib import Path
import time

from .console_config import ConsoleError
from .upstream import redact


def metrics(rows):
    counts = {status: sum(r['status'] == status for r in rows)
              for status in ('pass', 'fail', 'error', 'quota', 'cancelled','unknown','reference')}
    samples = counts['pass'] + counts['fail']
    durations = [r['duration'] for r in rows if isinstance(r.get('duration'), (int, float)) and r['duration'] >= 0]
    return {**counts, 'samples': samples, 'rate': round(100 * counts['pass'] / samples, 1) if samples else None,
            'average_seconds': round(sum(durations) / len(durations), 1) if durations else None,
            'last_test_at': max((r.get('finished_at') or r['slot'] for r in rows), default=None)}


def profiles(rows):
    groups = {}
    for r in rows:
        key = (r['platform'], r['model'], r['effort'], r['kind'], r.get('bank_version'))
        groups.setdefault(key, []).append(r)
    return [{'platform': key[0], 'model': key[1], 'effort': key[2], 'kind': key[3], 'bank_version': key[4],
             **metrics(values)} for key, values in groups.items()]


class PublicResults:
    def __init__(self, store):
        self.store = store

    def result_rows(self, include_hidden=False):
        rows = self.store.results()
        if include_hidden:
            return rows
        from .account_quarantine import conclusion
        plans = {p['id']: p['value'] for p in self.store.plans()}
        batches = {}
        for row in rows:
            batches.setdefault((row['job_id'], row['account_id']), []).append(row)
        hidden = set()
        for key, batch in batches.items():
            policy = plans.get(batch[0]['plan_id'], {}).get('quarantine', {})
            if not policy.get('hide_failed_results', True):
                continue
            job = self.store.job(key[0])
            if (job['source'] not in ('manual', 'timer', 'cooldown') or
                    not job['snapshot']['plan']['value'].get('quarantine', {}).get('enabled')):
                continue
            if conclusion(job['snapshot'].get('questions', []), [r['value'] for r in batch]) == 'fail':
                hidden.add(key)
        return [row for row in rows if (row['job_id'], row['account_id']) not in hidden]

    def projection(self, hours=None):
        value = self._projection(hours)
        value['groups'] = [{key: group[key] for key in ('id', 'name', 'method', 'profiles')}
                           for group in value['groups']]
        return value

    def _projection(self, hours=None):
        store = self.store
        plans = {p['id']: p for p in store.plans() if p['value']['published']}
        inventory = store.inventory()['value']
        names = store.document('publication')['value']
        raw_groups = {g['id']: g for g in inventory['groups']}
        groups = {}

        def ensure(key, name):
            public = store.public_id('group', key)
            return groups.setdefault(public, {'id': public, 'name': name, 'method': 'account-direct',
                                             'accounts': {}, '_rows': [], '_account_ids': set()})

        for p in plans.values():
            value = p['value']
            scope = value['scope']
            if scope['mode'] == 'groups':
                for gid in scope['group_ids']:
                    if gid in raw_groups:
                        ensure('g' + str(gid), names['group_names'].get(str(gid)) or raw_groups[gid]['name'])
            else:
                for a in inventory['accounts']:
                    if a['id'] in scope['account_ids'] and a['id'] not in scope['exclude_ids']:
                        for gid in a['group_ids']:
                            if gid in raw_groups: ensure('g'+str(gid),names['group_names'].get(str(gid)) or raw_groups[gid]['name'])

        for row in self.result_rows():
            if hours is not None and row['slot'] < time.time() - hours * 3600:
                continue
            plan = plans.get(row['plan_id'])
            if not plan:
                continue
            value = plan['value']
            scope = value['scope']
            if row['account_id'] in scope['exclude_ids']:
                continue
            if scope['mode'] == 'groups':
                keys = [('g' + str(gid), names['group_names'].get(str(gid)) or raw_groups[gid]['name'])
                        for gid in set(scope['group_ids']) & set(row['group_ids']) if gid in raw_groups]
            else:
                keys = [('g'+str(gid),names['group_names'].get(str(gid)) or raw_groups[gid]['name'])
                        for gid in row['group_ids'] if gid in raw_groups] if row['account_id'] in scope['account_ids'] else []
            raw = row['value']
            for key, name in keys:
                group = ensure(key, name)
                result = {k: raw.get(k) for k in ('kind', 'status', 'slot', 'duration', 'finished_at', 'model', 'effort', 'platform', 'bank_version')}
                result['id'] = store.public_id('run', row['id'])
                group['_rows'].append(result)
                group['_account_ids'].add(row['account_id'])
                if not value['show_accounts']:
                    continue
                account_id = store.public_id('account', key + ':' + str(row['account_id']))
                account = group['accounts'].setdefault(account_id, {
                    'id': account_id,
                    'name': names['account_names'].get(str(row['account_id'])) or '账号 ' + account_id[:6].upper(),
                    'group_id': group['id'], 'group_name': name, 'runs': [],
                })
                account['runs'].append(result)
        # Empty, explicitly published inventories still have useful real empty states.
        for p in plans.values():
            value, scope = p['value'], p['value']['scope']
            for a in inventory['accounts']:
                if a['id'] in scope['exclude_ids']:
                    continue
                keys = (['g' + str(g) for g in set(a['group_ids']) & set(scope['group_ids']) if g in raw_groups]
                        if scope['mode'] == 'groups' else ['g'+str(g) for g in a['group_ids'] if g in raw_groups] if a['id'] in scope['account_ids'] else [])
                for key in keys:
                    group = groups.get(store.public_id('group', key))
                    if not group:
                        continue
                    group['_account_ids'].add(a['id'])
                    if value['show_accounts']:
                        aid = store.public_id('account', key + ':' + str(a['id']))
                        group['accounts'].setdefault(aid, {'id': aid,
                            'name': names['account_names'].get(str(a['id'])) or '账号 ' + aid[:6].upper(),
                            'group_id': group['id'], 'group_name': group['name'], 'runs': []})
        # User checks are a separate measurement source, grouped by the key's
        # immutable group snapshot, without exposing user/key/account identity.
        for job in store.self_jobs(public_only=True):
            gid=job.get('group_id')
            if type(gid) is int and gid in raw_groups:
                ensure('g'+str(gid),names['group_names'].get(str(gid)) or raw_groups[gid]['name'])
        out = []
        for group in groups.values():
            # Result IDs deduplicate shared accounts across overlapping plans in this group.
            group['_rows'] = list({r['id']: r for r in group['_rows']}.values())
            group['profiles'] = profiles(group.pop('_rows'))
            group['account_count'] = len(group.pop('_account_ids'))
            group['accounts'] = list(group['accounts'].values())
            for a in group['accounts']:
                a['runs'] = list({r['id']: r for r in a['runs']}.values())
                a['profiles'] = profiles(a['runs'])
                a['runs'] = a['runs'][:100]
            out.append(group)
        return {'title': store.document('settings')['value']['title'], 'generated_at': time.time(),
                'history_hours': store.document('settings')['value']['retention']['artifacts_hours'],
                'method': 'account-direct', 'groups': out}

    def history(self, group_id, include_accounts=False):
        groups={g['id']:g for g in self.projection()['groups']}
        if group_id not in groups: raise ConsoleError('分组不存在或未公开',404)
        rows=[];store=self.store
        plans={p['id']:p['value'] for p in store.plans() if p['value']['published']}
        visible = {(r['job_id'], r['account_id']) for r in self.result_rows()} if include_accounts else None
        for row in self.result_rows(include_hidden=include_accounts):
            plan=plans.get(row['plan_id'])
            if not plan: continue
            s=plan['scope']
            if row['account_id'] in s['exclude_ids'] or (s['mode']=='accounts' and row['account_id'] not in s['account_ids']): continue
            gids=set(row['group_ids'])&(set(s['group_ids']) if s['mode']=='groups' else set(row['group_ids']))
            if not any(store.public_id('group','g'+str(g))==group_id for g in gids): continue
            raw=row['value']
            value={k:raw.get(k) for k in ('title','kind','status','model','effort','bank_version','duration','slot','finished_at','question_type')}
            value.update(id=store.public_id('record',row['id']),source='admin',batch_id=store.public_id('batch',row['job_id']),
                content_available=bool(plan['show_artifacts'] if raw.get('question_type')=='drawing' or raw['kind']=='drawing' else plan['show_answers']))
            if include_accounts:
                value['hidden_from_results'] = (row['job_id'], row['account_id']) not in visible
                targets = store.job(row['job_id'])['snapshot']['targets']
                value['account_id'] = row['account_id']
                value['account_name'] = next((a['name'] for a in targets if a['id']==row['account_id']), '')
            rows.append(value)
        cutoff=time.time()-store.document('settings')['value']['retention']['artifacts_hours']*3600
        for job in store.self_jobs(public_only=True):
            if job.get('group_id') is None or store.public_id('group','g'+str(job['group_id']))!=group_id or job['created_at']<cutoff: continue
            for index,r in enumerate(job['results']):
                value={k:r.get(k) for k in ('title','kind','status','bank_version','duration','finished_at','question_type')}
                value.update(id=store.public_id('self-record',job['id']+':'+str(index)),source='user',
                    batch_id=store.public_id('self-batch',job['id']),
                    model=job['model'],effort=job['effort'],slot=job['created_at'],content_available=bool(job.get('share_content')))
                rows.append(value)
        return sorted(rows,key=lambda r:r['slot'],reverse=True)[:300]

    def record(self, ident, include_hidden=False):
        allowed=next((r for g in self.projection()['groups'] for r in self.history(g['id'], include_accounts=include_hidden) if r['id']==ident),None)
        if not allowed: raise ConsoleError('结果不存在或已撤回',404)
        value={**allowed,'rounds':[], 'prompt': '', 'expected': None}
        if not allowed['content_available']: return value
        for row in self.store.results():
            if self.store.public_id('record',row['id'])==ident:
                raw=row['value'];rounds=raw.get('rounds',[])
                question = raw.get('question') or next((q for q in self.store.job(row['job_id'])['snapshot'].get('questions',[]) if q['id']==raw['kind']), {})
                value['prompt'] = redact(question.get('prompt',''))
                value['expected'] = question.get('expected') if question.get('kind') in ('number','choice','json') else None
                if not rounds:
                    path=self.store.private/'jobs'/row['job_id']/'public'/(row['id']+'.json')
                    if path.exists():
                        detail=json.loads(path.read_text());rounds=[{'round':1,'response':detail.get('response',''),'status':raw['status']}]
                break
        else:
            rounds=[]
            for job in self.store.self_jobs(public_only=True):
                for index,r in enumerate(job['results']):
                    if self.store.public_id('self-record',job['id']+':'+str(index))==ident:
                        rounds=r.get('rounds',[])
                        question=next((q for q in job.get('questions',[]) if q['id']==r['kind']),{})
                        value['prompt']=redact(question.get('prompt',''))
                        value['expected']=question.get('expected') if question.get('kind') in ('number','choice','json') else None
        value['rounds']=[{'round':r.get('round'),'status':r.get('status'),'answer':r.get('answer'),'response':redact(r.get('response',''))[:100000]} for r in rounds]
        return value

    def run(self, public_id, artifact=False):
        # Resolve only IDs that exist in the current public account projection.
        if not any(r['id'] == public_id for g in self._projection()['groups'] for a in g['accounts'] for r in a['runs']):
            raise ConsoleError('结果不存在或未公开', 404)
        row = next((r for r in self.store.results() if self.store.public_id('run', r['id']) == public_id), None)
        if not row:
            raise ConsoleError('结果不存在或未公开', 404)
        plan = self.store.plan(row['plan_id'])['value']
        raw = row['value']
        value = {k: raw.get(k) for k in ('kind', 'status', 'slot', 'duration', 'finished_at', 'model', 'effort', 'platform', 'bank_version')}
        value['id'] = public_id
        value['message'] = {'error': '请求故障，本次未判为答错', 'quota': '限额跳过', 'cancelled': '任务已取消'}.get(raw['status'])
        path = self.store.private / 'jobs' / row['job_id'] / 'public'
        if artifact:
            if not plan['show_artifacts'] or raw['kind'] != 'drawing' or raw['status'] != 'pass':
                raise ConsoleError('作品不存在或未公开', 404)
            file = path / (row['id'] + '.html')
            if not file.exists():
                raise ConsoleError('作品已过期', 404)
            return file
        value['artifact_available'] = bool(plan['show_artifacts'] and raw['kind'] == 'drawing' and raw['status'] == 'pass')
        if plan['show_answers'] and raw['kind'] == 'candy':
            detailfile = path / (row['id'] + '.json')
            if detailfile.exists():
                detail = json.loads(detailfile.read_text())
                value['answer'] = raw.get('answer')
                value['response'] = redact(detail.get('response', ''), errors=True)[:12000]
        return value
