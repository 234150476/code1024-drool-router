"""Optional account quarantine. Only closed-answer administrator probes can act."""
import time

from . import config
from .console_config import ConsoleError
from .quality_catalog import evaluate
from .quarantine_store import CLOSED, QuarantineStore
from .upstream import Sub2API, ProbeError, oauth_quota_state
from .account_selection import timestamp


DEFAULT_POLICY = {'enabled': False, 'cooldown_minutes': 60}


def gradable(questions):
    return [q for q in questions if q.get('enabled') and q.get('kind') in ('number', 'choice', 'json')]


def conclusion(questions, rows):
    """Any verified wrong answer fails; recovery needs every requested answer."""
    states = []
    for question in gradable(questions):
        matches = [row for row in rows if row.get('kind') == question['id'] and
                   row.get('bank_version') == question['version']]
        rounds = matches[0].get('rounds', []) if len(matches) == 1 else []
        if (not rounds or len(rounds) > question['rounds'] or
                [r.get('round') for r in rounds] != list(range(1, len(rounds)+1)) or
                any(r.get('status') in ('pass', 'fail') and (not isinstance(r.get('response'), str) or
                    evaluate(question, r['response'])[0] != r['status']) for r in rounds)):
            states.append('unknown')
        else:
            states.append('fail' if any(r.get('status') == 'fail' for r in rounds) else
                          'pass' if len(rounds) == question['rounds'] and all(r.get('status') == 'pass' for r in rounds) else 'unknown')
    return 'fail' if 'fail' in states else 'pass' if states and all(s == 'pass' for s in states) else 'unknown'


def same_identity(account, expected):
    return (all(account.get(k) == expected.get(k) for k in ('id', 'name', 'platform', 'type')) and
            set(account.get('group_ids', [])) == set(expected.get('group_ids', [])))


def available(account):
    now = time.time()
    return (account.get('status') == 'active' and type(account.get('schedulable')) is bool and
            (timestamp(account.get('expires_at')) or float('inf')) > now and
            not any((timestamp(account.get(k)) or 0) > now for k in
                    ('rate_limit_reset_at', 'overload_until', 'temp_unschedulable_until')) and
            not oauth_quota_state(account)['limited'])


class AccountQuarantine:
    def __init__(self, service):
        self.service = service
        self.store = service.store
        self.records = QuarantineStore(self.store)

    def key(self):
        try:
            file = self.store.private/'quarantine-key'
            return file.read_text().strip() if file.exists() else ''
        except OSError:
            return ''

    def api(self):
        key = self.key()
        if not key:
            raise ProbeError('QUARANTINE_KEY_MISSING', '未配置专用调度 Key')
        return Sub2API(config.get('base_url'), key)

    def save_key(self, key, actor):
        if not isinstance(key, str) or not 16 <= len(key.strip()) <= 1000:
            raise ConsoleError('请提供有效的专用调度 Key')
        try:
            Sub2API(config.get('base_url'), key.strip()).accounts(all_accounts=True)
        except ProbeError as exc:
            raise ConsoleError('专用 Key 无法读取账号：'+exc.code) from None
        file = self.store.private/'quarantine-key.tmp'
        file.write_text(key.strip()); file.chmod(0o600); file.replace(self.store.private/'quarantine-key')
        with self.store.db() as db:
            self.store.audit(db, actor, 'save_quarantine_key', 'quarantine')
        return {'configured': True}

    def current_plan(self, record):
        plan = self.store.plan(record['plan_id'])
        value = plan['value']
        if not value['enabled'] or not value.get('quarantine', DEFAULT_POLICY)['enabled']:
            return None
        if value['scope'] != record['profile']['value']['scope']:
            raise ProbeError('QUARANTINE_SCOPE_CHANGED', '分组范围已变化，转人工处理')
        return plan

    def admin_entries(self):
        return [{k: row.get(k) for k in ('account_id', 'account', 'plan_id', 'state', 'due_at', 'error_code',
                'attempts', 'created_at', 'updated_at', 'evidence_job_id', 'recheck_job_id')}
                for row in self.records.entries()[:200]]

    def admin_status(self):
        with self.store.db() as db:
            events = [dict(row) for row in db.execute(
                "SELECT action,object_id,created_at FROM console_audit WHERE action LIKE 'quarantine_%' ORDER BY id DESC LIMIT 30")]
        plans = {p['id']: p['value'] for p in self.store.plans()}
        return {'key_ready': bool(self.key()), 'entries': [
            {**row, 'policy_enabled': bool(plans.get(row['plan_id'], {}).get('enabled') and
                plans.get(row['plan_id'], {}).get('quarantine', {}).get('enabled'))}
            for row in self.admin_entries()], 'events': events}

    def authorized(self, job):
        marker = job['snapshot'].get('recovery', {})
        record = self.records.get(marker.get('account_id'))
        return bool(record and record['state'] == 'rechecking' and
                    record['generation'] == marker.get('generation') and record['recheck_job_id'] == job['id'] and
                    self.current_plan(record))

    def handover(self, account_id, actor):
        with self.service.lock:
            record = self.records.get(account_id)
            if not record:
                raise ConsoleError('剔除记录不存在', 404)
            self.records.change(record, action='quarantine_manual', actor=actor, state='manual', due_at=0)
            if record.get('recheck_job_id'):
                self.service.stop_job(record['recheck_job_id'], actor)
        return {'state': 'manual', 'scheduling_changed': False}

    def on_complete(self, job):
        if job['source'] == 'cooldown':
            marker = job['snapshot'].get('recovery', {})
            record = self.records.get(marker.get('account_id'))
            if record and record['generation'] == marker.get('generation') and record['recheck_job_id'] == job['id'] and record['state'] == 'rechecking':
                self.finish_recheck(record, job)
            return
        if job['source'] not in ('manual', 'timer') or job['status'] not in ('completed', 'completed_with_errors') or not job['snapshot'].get('questions'):
            return
        plan = self.store.plan(job['plan_id'])
        if (plan['revision'] != job['snapshot']['plan']['revision'] or not plan['value']['enabled'] or
                not plan['value'].get('quarantine', DEFAULT_POLICY)['enabled']):
            return
        rows = [r for r in self.store.results() if r['job_id'] == job['id']]
        for target in job['snapshot']['targets']:
            if target.get('schedulable') is not True:
                continue
            values = [r['value'] for r in rows if r['account_id'] == target['id']]
            if conclusion(job['snapshot']['questions'], values) != 'fail':
                continue
            try:
                account = self.api().account_state(target['id'])
            except ProbeError as exc:
                with self.store.db() as db:
                    self.store.audit(db, 'quarantine', 'quarantine_evidence_error:'+exc.code, str(target['id']))
                continue
            if not same_identity(account, target) or not available(account) or account['schedulable'] is not True:
                continue
            record = self.records.claim(job, account, gradable(job['snapshot']['questions']))
            if record:
                self.write(record, False)

    def write(self, record, enabled):
        try:
            plan = self.current_plan(record)
            if not plan:
                return
            if enabled:
                job = self.store.job(record['recheck_job_id'])
                if (job['status'] != 'completed' or not job.get('finished_at') or
                        time.time()-job['finished_at'] > plan['value']['quarantine']['cooldown_minutes']*60):
                    self.records.change(record, action='quarantine_pass_expired', state='cooling', due_at=time.time(), attempts=0)
                    return
            if record['attempts'] >= 3:
                self.records.change(record, action='quarantine_write_blocked', state='blocked', error_code=record.get('error_code') or 'QUARANTINE_WRITE_LIMIT')
                return
            api = self.api()
            account = api.account_state(record['account_id'])
            if not same_identity(account, record['account']) or not available(account):
                raise ProbeError('QUARANTINE_ACCOUNT_CHANGED', '账号状态、限额或身份已变化，转人工处理')
            if enabled and account['schedulable'] is True:
                self.records.change(record, action='quarantine_external_resume', state='manual', due_at=0, error_code='EXTERNAL_SCHEDULING_CHANGE')
                return
            if not enabled and account['schedulable'] is False and record['attempts'] == 0:
                self.records.change(record, action='quarantine_external_pause', state='manual', due_at=0, error_code='EXTERNAL_SCHEDULING_CHANGE')
                return
            record = self.records.change(record, action='quarantine_write_attempt', attempts=record['attempts']+1)
            api.set_schedulable_only(record['account_id'], enabled, record['account'])
            self.records.change(record, action='quarantine_restored' if enabled else 'quarantine_disabled',
                state='recovered' if enabled else 'cooling', attempts=0, error_code=None,
                due_at=0 if enabled else time.time()+plan['value']['quarantine']['cooldown_minutes']*60)
        except ProbeError as exc:
            terminal = exc.code in ('QUARANTINE_ACCOUNT_CHANGED', 'QUARANTINE_SCOPE_CHANGED',
                                   'HTTP_401', 'HTTP_403', 'QUARANTINE_HTTP_401', 'QUARANTINE_HTTP_403', 'QUARANTINE_ACCOUNT_BLOCKED')
            self.records.change(record, action='quarantine_write_error', error_code=exc.code,
                                **({'state': 'blocked'} if terminal else {'due_at': time.time()+60}))

    def finish_recheck(self, record, job):
        plan = self.current_plan(record)
        if not plan:
            return
        rows = [r['value'] for r in self.store.results() if r['job_id'] == job['id'] and r['account_id'] == record['account_id']]
        passed = (job['status'] == 'completed' and job.get('finished_at') and
                  time.time()-job['finished_at'] <= plan['value']['quarantine']['cooldown_minutes']*60 and
                  conclusion(record['profile']['value']['questions'], rows) == 'pass')
        if passed:
            record = self.records.change(record, action='quarantine_recheck_passed', state='restoring', attempts=0, error_code=None)
            self.write(record, True)
        else:
            self.records.change(record, action='quarantine_recheck_not_passed', state='cooling',
                due_at=time.time()+plan['value']['quarantine']['cooldown_minutes']*60,
                error_code='RECHECK_NOT_PASSED' if job['status'] == 'completed' else 'RECHECK_INCOMPLETE')

    def tick(self):
        from .console_service import quiet_paused
        for record in self.records.entries():
            if record['state'] in CLOSED or record['state'] == 'blocked':
                continue
            try:
                plan = self.current_plan(record)
                if not plan:
                    continue
                if record['state'] in ('disabling', 'restoring') and record['due_at'] <= time.time():
                    self.write(record, record['state'] == 'restoring')
                elif record['state'] == 'rechecking':
                    job = self.store.job(record['recheck_job_id'])
                    if job['status'] not in ('queued', 'running'):
                        self.finish_recheck(record, job)
                elif record['state'] == 'cooling' and record['due_at'] <= time.time():
                    if quiet_paused(plan['value']['schedule']) or not self.service.machine_key():
                        continue
                    account = self.api().account_state(record['account_id'])
                    if not same_identity(account, record['account']) or account.get('status') != 'active':
                        raise ProbeError('QUARANTINE_ACCOUNT_CHANGED', '账号身份或状态变化，转人工处理')
                    if account.get('schedulable') is True:
                        self.records.change(record, action='quarantine_external_resume', state='manual', due_at=0, error_code='EXTERNAL_SCHEDULING_CHANGE')
                    elif available(account) and account.get('schedulable') is False:
                        self.records.queue_recheck(record, account, plan)
                        self.service.wake.set()
                    else:
                        self.records.change(record, action='quarantine_waiting', due_at=time.time()+300, error_code='ACCOUNT_TEMPORARILY_UNAVAILABLE')
            except (ProbeError, ConsoleError) as exc:
                code = exc.code if isinstance(exc, ProbeError) else 'QUARANTINE_PLAN_UNAVAILABLE'
                terminal = code in ('QUARANTINE_ACCOUNT_CHANGED', 'QUARANTINE_SCOPE_CHANGED', 'QUARANTINE_PLAN_UNAVAILABLE')
                self.records.change(record, action='quarantine_reconcile_error', error_code=code,
                                    **({'state': 'blocked'} if terminal else {'due_at': time.time()+300}))
