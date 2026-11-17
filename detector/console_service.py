"""Administrative orchestration. Tokens remain in memory, workers get snapshots."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from zoneinfo import ZoneInfo

from . import config
from .console_config import ConsoleError, validate_plan
from .console_store import ConsoleStore, dumps
from .console_public import PublicResults
from .upstream import Sub2API, ProbeError
from .self_service import SelfService
from .quality_isolation import IsolationPolicy, policy


def schedule_slot(spec, now=None):
    now = time.time() if now is None else now
    tz = ZoneInfo(spec['timezone'])
    today = datetime.fromtimestamp(now, tz).date()
    slots = []
    def minutes(value):
        h, m = map(int, value.split(':'))
        return h * 60 + m
    start, end = minutes(spec['quiet_start']), minutes(spec['quiet_end'])
    for delta in (-2, -1, 0, 1):
        day = today + timedelta(days=delta)
        midnight = datetime(day.year, day.month, day.day, tzinfo=tz)
        for minute in range(0, 1440):
            quiet = start <= minute < end if end > start else minute >= start or minute < end
            anchor = start if quiet else end
            interval = spec['quiet_minutes'] if quiet else spec['regular_minutes']
            if (minute - anchor) % interval == 0:
                slots.append((midnight + timedelta(minutes=minute)).timestamp())
    return max(s for s in slots if s <= now)


class ConsoleService:
    def __init__(self, root, start=True):
        self.store = ConsoleStore(root)
        self.public = PublicResults(self.store)
        self.self_service = SelfService(self.store, start=start)
        self.isolation = IsolationPolicy(self.store)
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.tokens = {}
        self.lock = threading.RLock()
        self.store.recover_jobs()
        self.thread = None
        if start:
            self.thread = threading.Thread(target=self.loop, daemon=True)
            self.thread.start()

    def machine_key(self):
        try:
            path = os.environ.get('SUB2API_ADMIN_KEY_FILE')
            value = Path(path).read_text().strip() if path else os.environ.get('SUB2API_ADMIN_KEY', '')
            if not value:
                file=self.store.private/'automation-key'
                value=file.read_text().strip() if file.exists() else ''
            return value if len(value) >= 16 else ''
        except OSError:
            return ''

    def capabilities(self):
        return {'machine_credential_ready': bool(self.machine_key()), 'session_auth': bool(config.get('web.session_auth_url')),
                'gateway_configured': bool(config.get('base_url')),
                'write_priority': False, 'write_callable': False,
                'admin_entry': '/custom/quality-inspection'}

    def save_automation(self,key,actor):
        if not isinstance(key,str) or not 16<=len(key.strip())<=1000:
            raise ConsoleError('请提供有效的网关管理员 API Key')
        api=Sub2API(config.get('base_url'),key.strip())
        try:
            accounts=api.accounts(all_accounts=True)
            if accounts and not os.environ.get('SUB2API_ACCOUNT_CREDENTIALS_FILE'):
                # A list-only key cannot run unattended account probes. Confirm
                # the real export permission now, without calling any model.
                api.get('/accounts/data',{'ids':str(accounts[0]['id']),'include_proxies':'true'})
        except ProbeError as exc: raise ConsoleError('机器凭据验证失败：'+exc.code,400) from None
        file=self.store.private/'automation-key.tmp'
        file.write_text(key.strip());file.chmod(0o600);file.replace(self.store.private/'automation-key')
        with self.store.db() as db: self.store.audit(db,actor,'save_automation_credential','automation')
        return self.capabilities()

    def sync_inventory(self, token, actor):
        key = token or self.machine_key()
        if not key or not config.get('base_url'):
            raise ConsoleError('网关管理凭据不可用，请重新进入管理员页面', 401)
        api = Sub2API(config.get('base_url'), key, bearer=bool(token))
        try:
            accounts = api.accounts(all_accounts=True)
            groups = api.get('/groups/all')
            if isinstance(groups, dict):
                groups = groups.get('items', groups.get('groups'))
            return self.store.save_inventory(accounts, groups, actor)
        except ProbeError as exc:
            raise ConsoleError('同步失败：' + exc.code, 502) from None

    def save_plan(self, value, actor, ident=None, revision=None):
        if not isinstance(value, dict):
            raise ConsoleError('检测计划必须为对象')
        value=validate_plan(value)
        if isinstance(value.get('schedule'), dict) and value['schedule'].get('enabled') and not self.machine_key():
            raise ConsoleError('尚未配置机器凭据，无法启用定时；可关闭定时后保存')
        if policy(value)['enabled'] and not self.machine_key():
            raise ConsoleError('自动隔离及冷却复测需要先配置机器凭据')
        if policy(value)['enabled']:
            from .quality_catalog import select
            if not any(q['kind'] in ('number','choice','json') for q in select(self.store,value.get('kinds'))):
                raise ConsoleError('自动隔离至少需要一道有标准答案的题，作品和指纹不参与自动隔离')
        return self.store.save_plan(value, actor, ident, revision)

    def save_settings(self, value, revision, actor):
        if not isinstance(value, dict):
            raise ConsoleError('参数配置必须为对象')
        if isinstance(value.get('schedule'), dict) and value['schedule'].get('enabled') and not self.machine_key():
            raise ConsoleError('尚未配置机器凭据，无法启用定时默认值')
        return self.store.save_document('settings', value, revision, actor)

    def enqueue(self, payload, token, actor, source='manual'):
        if not token and not self.machine_key():
            raise ConsoleError('执行凭据已失效，请重新登录', 401)
        required = {'plan_id', 'request_key', 'confirmed', 'plan_revision', 'settings_revision', 'inventory_revision','questions_revision'}
        if not isinstance(payload, dict) or set(payload) != required or payload['confirmed'] is not True:
            raise ConsoleError('请先预览并确认检测范围和模型请求')
        with self.lock:
            job, created = self.store.create_job(payload['plan_id'], payload['request_key'], actor, source, payload)
            if created:
                self.tokens[job['id']] = token
        self.wake.set()
        return self.job_summary(job)

    def job_summary(self, job):
        snapshot = job['snapshot']
        root = self.store.private / 'jobs' / job['id']
        total = len(snapshot['targets']) * len(snapshot['plan']['value']['kinds'])
        completed, status_counts = 0, {}
        skipped = list(snapshot.get('skipped', []))
        try:
            skipped.extend(json.loads((root / 'skipped.json').read_text()))
        except (OSError, ValueError):
            pass
        dbpath = root / 'private' / 'state.sqlite3'
        if snapshot.get('questions'):
            try:
                rows=json.loads((root/'results.json').read_text())
                for row in rows: status_counts[row['status']]=status_counts.get(row['status'],0)+1
                completed=len(rows)
            except (OSError,ValueError): pass
        if dbpath.exists():
            try:
                with sqlite3.connect('file:' + str(dbpath) + '?mode=ro', uri=True, timeout=1) as db:
                    for status, count in db.execute('SELECT status,count(*) FROM runs GROUP BY status'):
                        status_counts[status] = count
                        if status not in ('pending', 'running'):
                            completed += count
            except sqlite3.Error:
                pass
        return {k: job[k] for k in ('id', 'plan_id', 'status', 'source', 'actor', 'created_at', 'started_at', 'finished_at', 'message')} | {
            'name': snapshot['plan']['value']['name'], 'account_count': len(snapshot['targets']),
            'plan_revision': snapshot['plan']['revision'], 'settings_revision': snapshot['settings']['revision'],
            'completed': completed, 'total': total, 'status_counts': status_counts,
            'models': snapshot['plan']['value']['models'], 'skipped': skipped}

    def stop_job(self, ident, actor):
        with self.lock:
            job = self.store.job(ident)
            if job['status'] == 'queued':
                self.store.update_job(ident, status='cancelled', finished_at=time.time(), message='管理员取消了排队任务')
                self.tokens.pop(ident, None)
            elif job['status'] == 'running':
                root = self.store.private / 'jobs' / ident
                root.mkdir(parents=True, exist_ok=True, mode=0o700)
                (root / 'stop').touch(mode=0o600)
                self.store.update_job(ident, message='正在停止后续请求；已发出的请求等待结束')
            with self.store.db() as db:
                self.store.audit(db, actor, 'stop_job', ident)
            if job['source']=='cooldown':
                self.review_retest(job,'ADMIN_STOPPED_RETEST')
        return self.job_summary(self.store.job(ident))

    def review_retest(self,job,message):
        ids={a['id'] for a in job['snapshot']['targets']}
        for record in self.isolation.records():
            if record['account_id'] in ids and record['state']=='cooling':
                record.update(state='review',message=message)
                self.isolation.save(record,'retest_review_required')

    def loop(self):
        last_schedule = 0
        while not self.stop.is_set():
            queued = sorted((j for j in self.store.jobs() if j['status'] == 'queued'), key=lambda j: j['created_at'])
            if queued:
                try:
                    self.execute(queued[0])
                except Exception:
                    logging.exception('Console job preparation failed: %s', queued[0]['id'])
                    self.tokens.pop(queued[0]['id'], None)
                    self.store.update_job(queued[0]['id'], status='failed', finished_at=time.time(),
                                          message='准备任务失败；未自动重发，请检查私有日志')
                continue
            if time.time() - last_schedule > 30:
                try:
                    self.tick()
                    self.cleanup()
                except (ConsoleError, OSError, sqlite3.Error, ValueError):
                    # Record a bounded error code, never token/connection values.
                    with self.store.db() as db:
                        self.store.audit(db, 'scheduler', 'scheduler_failed', 'console')
                last_schedule = time.time()
            self.wake.wait(2)
            self.wake.clear()

    def tick(self):
        if not self.machine_key():
            return
        for record in self.isolation.records():
            if record['state']!='cooling' or record['due_at']>time.time(): continue
            for pid in record['plans']:
                plan=self.store.plan(pid)
                if not plan['value']['enabled'] or not policy(plan['value'])['enabled'] or plan['revision']!=record['plans'][pid]['revision']:
                    record.update(state='review',message='ISOLATION_CONFIG_CHANGED')
                    self.isolation.save(record,'isolation_review_required');break
                with self.store.db() as db:
                    if db.execute("SELECT 1 FROM console_jobs WHERE plan_id=? AND source='cooldown' AND status IN ('queued','running')",(pid,)).fetchone(): continue
                self.sync_inventory(None,'scheduler')
                try:
                    preview=self.store.resolve(plan)
                    if record['account_id'] not in {a['id'] for a in preview['accounts']}: raise ConsoleError('账号已退出范围')
                except ConsoleError:
                    record.update(state='review',message='ISOLATION_SCOPE_OR_QUESTIONS_CHANGED')
                    self.isolation.save(record,'isolation_review_required');break
                key='cooldown:'+str(record['account_id'])+':'+pid+':'+str(int(record['due_at']))
                self.store.create_job(pid,key,'scheduler','cooldown',preview,target_ids={record['account_id']})
        for plan in self.store.plans():
            value = plan['value']
            if not value['enabled'] or not value['schedule']['enabled']:
                continue
            slot = schedule_slot(value['schedule'])
            key = 'timer:' + plan['id'] + ':' + str(plan['revision']) + ':' + str(int(slot))
            with self.store.db() as db:
                if db.execute('SELECT 1 FROM console_jobs WHERE request_key=?', (key,)).fetchone():
                    continue
            self.sync_inventory(None, 'scheduler')
            preview = self.store.resolve(plan)
            if not preview['account_count']:
                continue
            self.enqueue({'plan_id': plan['id'], 'request_key': key, 'confirmed': True,
                          **{k: preview[k] for k in ('plan_revision', 'settings_revision', 'inventory_revision','questions_revision')}}, None, 'scheduler', 'timer')

    def execute(self, job):
        ident = job['id']
        with self.lock:
            if self.store.job(ident)['status'] != 'queued':
                return
            self.store.update_job(ident, status='running', started_at=time.time())
            token = self.tokens.pop(ident, None)
        root = self.store.private / 'jobs' / ident
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        settings, plan = job['snapshot']['settings']['value'], job['snapshot']['plan']['value']
        snapshot = deepcopy(config.CONFIG)
        snapshot.update(title=settings['title'], data_dir=str(root), budgets=settings['budgets'],
                        retention=settings['retention'], schedule=plan['schedule'])
        snapshot['platforms'] = {}
        for name, spec in plan['models'].items():
            snapshot['platforms'][name] = {**spec, 'label': name,
                'group_ids': plan['scope']['group_ids'] if plan['scope']['mode'] == 'groups' else [],
                'group_names': [], 'exclude_names': []}
        snapshot['scope'] = {'account_ids': [a['id'] for a in job['snapshot']['targets']],
                             'targets': job['snapshot']['targets']}
        snapshot['tasks'] = {'kinds': plan['kinds']}
        snapshot['console'] = {'concurrency': settings['concurrency']}
        # Console tasks observe results only. Routing needs cross-task evidence before being enabled here.
        snapshot['routing'].update(write_priority=False, write_callable=False, weights=settings['weights'],
                                   rounds=settings['rounds'], stability_rounds=settings['stability_rounds'])
        configfile = root / 'config.json'
        configfile.write_text(dumps(snapshot)); configfile.chmod(0o600)
        if job['snapshot'].get('questions'):
            (root/'snapshot.json').write_text(dumps({**job['snapshot'],'created_at':job['created_at']}))
            (root/'snapshot.json').chmod(0o600)
        env = dict(os.environ)
        env.pop('DROOL_ADMIN_BEARER_TOKEN', None)
        if token:
            env['DROOL_ADMIN_BEARER_TOKEN'] = token
            env.pop('SUB2API_ADMIN_KEY', None)
            env.pop('SUB2API_ADMIN_KEY_FILE', None)
        elif self.machine_key():
            env['SUB2API_ADMIN_KEY']=self.machine_key()
            env.pop('SUB2API_ADMIN_KEY_FILE',None)
        try:
            if not token and not self.machine_key():
                raise ConsoleError('执行凭据不可用，请重新启动任务')
            with (root / 'worker.log').open('w') as log:
                os.chmod(root / 'worker.log', 0o600)
                module='detector.quality_worker' if job['snapshot'].get('questions') else 'detector.console_worker'
                result = subprocess.run([sys.executable, '-m', module, '--root', str(root)],
                                        cwd=Path(__file__).resolve().parent.parent, env=env,
                                        stdout=log, stderr=log, timeout=11000, check=False)
            self.store.import_results(job)
            cancelled = (root / 'stop').exists()
            skipped_count = len(json.loads((root / 'skipped.json').read_text())) if (root / 'skipped.json').exists() else 0
            status_counts = self.job_summary(self.store.job(ident)).get('status_counts', {})
            has_request_errors = bool(status_counts.get('error')) and not (status_counts.get('pass') or status_counts.get('fail'))
            final_status = 'cancelled' if cancelled else 'failed' if result.returncode != 0 else 'completed_with_errors' if has_request_errors else 'completed'
            if cancelled:
                final_message = '任务已停止'
            elif result.returncode != 0:
                final_message = '执行失败，请查看任务详情与私有日志'
            elif has_request_errors:
                final_message = '模型请求全部失败：0 项可判定 / %d 项请求故障' % status_counts.get('error', 0)
            elif skipped_count:
                final_message = '%d 个账号因成员或状态变化跳过，请查看任务详情' % skipped_count
            else:
                final_message = None
            self.store.update_job(ident, status=final_status,
                                  finished_at=time.time(), message=final_message)
            if self.machine_key():
                try:
                    self.isolation.observe(self.store.job(ident),Sub2API(config.get('base_url'),self.machine_key()))
                except Exception:
                    logging.exception('Quality policy failed for job %s',ident)
                    with self.store.db() as db: self.store.audit(db,'quality-policy','policy_execution_failed',ident)
        except Exception:
            logging.exception('Console worker failed: %s', ident)
            self.store.update_job(ident, status='failed', finished_at=time.time(), message='执行未完成；未自动重发模型请求')
        finally:
            if job['source']=='cooldown' and self.store.job(ident)['status'] not in ('completed','completed_with_errors'):
                self.review_retest(job,'RETEST_FAILED_OR_CANCELLED')
            token = None

    def cleanup(self):
        retention = self.store.document('settings')['value']['retention']
        cutoff = time.time() - retention['artifacts_hours'] * 3600
        with self.store.db() as db:
            db.execute('DELETE FROM console_results WHERE slot<?', (cutoff,))
            # Retain lightweight task metadata for 30 days, remove expensive worker output at configured expiry.
            old = [r['id'] for r in db.execute("SELECT id FROM console_jobs WHERE finished_at<? AND status NOT IN ('running','queued')", (cutoff,))]
            db.execute('DELETE FROM console_audit WHERE created_at<?', (time.time() - 90 * 86400,))
        for ident in old:
            path = self.store.private / 'jobs' / ident
            if path.exists():
                shutil.rmtree(path)

    def admin_results(self, ident=None):
        rows = self.store.results()
        if ident:
            job = self.store.job(ident)
            targets = {a['id']: a for a in job['snapshot']['targets']}
            return [{**r['value'], 'account_id': r['account_id'], 'account_name': targets.get(r['account_id'], {}).get('name', '')}
                    for r in rows if r['job_id'] == ident]
        return rows
