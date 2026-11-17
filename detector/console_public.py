"""Public projection is an explicit field allowlist, never an admin-state copy."""
import json
from pathlib import Path
import time

from .console_config import ConsoleError
from .upstream import redact


def metrics(rows):
    counts = {status: sum(r['status'] == status for r in rows)
              for status in ('pass', 'fail', 'error', 'quota', 'cancelled')}
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
                ensure('p' + p['id'], value['public_name'])

        for row in store.results():
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
                keys = [('p' + plan['id'], value['public_name'])] if row['account_id'] in scope['account_ids'] else []
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
                        if scope['mode'] == 'groups' else ['p' + p['id']] if a['id'] in scope['account_ids'] else [])
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
