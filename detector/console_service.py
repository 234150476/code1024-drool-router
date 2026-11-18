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
from .account_quarantine import AccountQuarantine


def quiet_paused(spec, now=None):
    if not spec.get('quiet_pause'):
        return False
    local = datetime.fromtimestamp(time.time() if now is None else now, ZoneInfo(spec['timezone']))
    minute = local.hour * 60 + local.minute
    start, end = (int(value[:2]) * 60 + int(value[3:]) for value in (spec['quiet_start'], spec['quiet_end']))
    return start <= minute < end if end > start else minute >= start or minute < end


def schedule_slot(spec, now=None):
    now = time.time() if now is None else now
    if quiet_paused(spec, now):
        return None
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
            if quiet and spec.get('quiet_pause'):
                continue
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
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.tokens = {}
        self.lock = threading.RLock()
        self.store.recover_jobs()
        self.quarantine = AccountQuarantine(self)
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
                'admin_entry': '/custom/quality-inspection',
                'quarantine_key_ready': bool(self.quarantine.key())}

    def save_automation(self,key,actor):
        if not isinstance(key,str) or not 16<=len(key.strip())<=1000:
            raise ConsoleError('请提供有效的网关管理员 API Key')
        api=Sub2API(config.get('base_url'),key.strip())
        try:
            accounts=api.accounts(all_accounts=True)
            if accounts and os.environ.get('SUB2API_ACCOUNT_SNAPSHOT_FILE'):
                api.account(accounts[0]['id'])
            elif accounts and not os.environ.get('SUB2API_ACCOUNT_CREDENTIALS_FILE'):
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
        with self.lock:
            if value.get('quarantine', {}).get('enabled') and not self.quarantine.key():
                raise ConsoleError('启用自动剔除前，请先配置带 accounts.read、accounts.status 权限的专用调度 Key')
            plan = self.store.save_plan(value, actor, ident, revision)
            if not value['enabled'] or not value.get('quarantine', {}).get('enabled'):
                for record in self.quarantine.records.entries():
                    if record['plan_id'] == plan['id'] and record['state'] == 'rechecking':
                        self.stop_job(record['recheck_job_id'], actor)
            return plan

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
        attempt_limit = snapshot.get('preview', {}).get('max_account_attempts')
        progress = {}
        if attempt_limit and job['source'] in ('manual', 'timer'):
            total = len(snapshot['questions']) if snapshot['targets'] else 0
            try:
                progress = json.loads((root / 'inspection-attempts.json').read_text())
            except (OSError, ValueError):
                pass
            progress = {'max_account_attempts': attempt_limit, 'attempts': progress.get('accounts', []),
                        'attempt_outcome': progress.get('outcome')}
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
            'models': snapshot['plan']['value']['models'], 'skipped': skipped, **progress}

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
        return self.job_summary(self.store.job(ident))

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
        with self.lock:
            self.quarantine.tick()
            self.schedule_tick()

    def schedule_tick(self):
        if not self.machine_key():
            return
        for plan in self.store.plans():
            value = plan['value']
            if not value['enabled'] or not value['schedule']['enabled']:
                continue
            slot = schedule_slot(value['schedule'])
            if slot is None:
                continue
            key = 'timer:' + plan['id'] + ':' + str(int(slot))
            with self.store.db() as db:
                if db.execute('SELECT 1 FROM console_jobs WHERE request_key=?', (key,)).fetchone():
                    continue
            self.sync_inventory(None, 'scheduler')
            preview = self.store.resolve(plan)
            if not preview['account_count'] and not preview.get('max_account_attempts'):
                continue
            self.enqueue({'plan_id': plan['id'], 'request_key': key, 'confirmed': True,
                          **{k: preview[k] for k in ('plan_revision', 'settings_revision', 'inventory_revision','questions_revision')}}, None, 'scheduler', 'timer')

    def execute(self, job):
        ident = job['id']
        with self.lock:
            if self.store.job(ident)['status'] != 'queued':
                return
            if job['source'] == 'cooldown' and not self.quarantine.authorized(job):
                self.store.update_job(ident, status='cancelled', finished_at=time.time(), message='自动剔除已停用或账号已转人工，未发起复测')
                return
            if job['source'] in ('timer', 'cooldown') and quiet_paused(job['snapshot']['plan']['value']['schedule']):
                self.tokens.pop(ident, None)
                self.store.update_job(ident, status='cancelled', finished_at=time.time(), message='已进入暂停时段，未发起检测请求')
                return
            if not job['snapshot']['targets'] and job['snapshot'].get('preview', {}).get('max_account_attempts'):
                self.tokens.pop(ident, None)
                self.store.update_job(ident, status='completed_with_errors', finished_at=time.time(),
                                      message='当前分组没有可检测账号')
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
            (root/'snapshot.json').write_text(dumps({**job['snapshot'],'created_at':job['created_at'],'source':job['source']}))
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
            summary = self.job_summary(job)
            status_counts = summary['status_counts']
            outcome = summary.get('attempt_outcome')
            has_request_errors = bool(status_counts.get('error')) and not (status_counts.get('pass') or status_counts.get('fail'))
            if cancelled or outcome == 'paused':
                final_status = 'cancelled'
                final_message = '任务已停止' if cancelled else '已进入暂停时段，停止后续检测'
            elif result.returncode != 0:
                final_status = 'failed'
                final_message = '执行失败，请查看任务详情与私有日志'
            elif outcome == 'unavailable':
                final_status = 'completed_with_errors'
                final_message = '已尝试 %d 个账号，本轮账号均不可用' % len(summary['attempts'])
            elif outcome == 'error':
                final_status = 'completed_with_errors'
                final_message = '检测请求失败（%s）' % summary['attempts'][-1]['error_code']
            elif outcome == 'completed':
                final_status = 'completed'
                final_message = None
            elif has_request_errors:
                final_status = 'completed_with_errors'
                final_message = '模型请求全部失败：0 项可判定 / %d 项请求故障' % status_counts.get('error', 0)
            else:
                final_status = 'completed'
                final_message = '%d 个账号因成员或状态变化跳过，请查看任务详情' % skipped_count if skipped_count else None
            self.store.update_job(ident, status=final_status,
                                  finished_at=time.time(), message=final_message)
        except Exception:
            logging.exception('Console worker failed: %s', ident)
            self.store.update_job(ident, status='failed', finished_at=time.time(), message='执行未完成；未自动重发模型请求')
        finally:
            token = None
        # Scheduling errors must never turn a completed model test into a failed test.
        try:
            with self.lock:
                self.quarantine.on_complete(self.store.job(ident))
        except Exception:
            with self.store.db() as db:
                self.store.audit(db, 'quarantine', 'quarantine_callback_failed', ident)

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
