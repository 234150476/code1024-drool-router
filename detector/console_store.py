"""Console persistence, scope resolution and immutable task snapshots."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from . import config
from .account_selection import inspection_attempt_limit, inspection_candidates, recent_active
from .console_config import ConsoleError, defaults, validate_plan, validate_settings


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


class ConsoleStore:
    def __init__(self, root):
        self.root = Path(root)
        self.private = self.root / 'private'
        self.private.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.private / 'console.sqlite3'
        self.lock = threading.RLock()
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS console_documents (
                    key TEXT PRIMARY KEY, revision INTEGER NOT NULL, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS console_plans (
                    id TEXT PRIMARY KEY, revision INTEGER NOT NULL, value TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS console_jobs (
                    id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, plan_id TEXT NOT NULL,
                    status TEXT NOT NULL, source TEXT NOT NULL, actor TEXT NOT NULL,
                    created_at REAL NOT NULL, started_at REAL, finished_at REAL,
                    snapshot TEXT NOT NULL, message TEXT);
                CREATE TABLE IF NOT EXISTS console_results (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, plan_id TEXT NOT NULL,
                    account_id INTEGER NOT NULL, group_ids TEXT NOT NULL, slot REAL NOT NULL,
                    profile TEXT NOT NULL, value TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS console_results_time ON console_results(slot);
                CREATE INDEX IF NOT EXISTS console_results_plan ON console_results(plan_id,slot);
                CREATE INDEX IF NOT EXISTS console_results_job ON console_results(job_id);
                CREATE TABLE IF NOT EXISTS console_audit (
                    id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL,
                    object_id TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS self_jobs (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, api_key_id INTEGER NOT NULL,
                    key_name TEXT NOT NULL, status TEXT NOT NULL, model TEXT NOT NULL,
                    effort TEXT NOT NULL, kinds TEXT NOT NULL, public INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, started_at REAL, finished_at REAL,
                    message TEXT, results TEXT NOT NULL DEFAULT '[]');
                CREATE INDEX IF NOT EXISTS self_jobs_user_time ON self_jobs(user_id, created_at);
            ''')
            job_columns = {r['name'] for r in db.execute('PRAGMA table_info(console_jobs)')}
            if 'results_pruned' not in job_columns:
                db.execute('ALTER TABLE console_jobs ADD COLUMN results_pruned INTEGER NOT NULL DEFAULT 0')
            columns={r['name'] for r in db.execute('PRAGMA table_info(self_jobs)')}
            for name, definition in [('questions', "TEXT NOT NULL DEFAULT '[]'"),('group_id','INTEGER'),('group_name',"TEXT NOT NULL DEFAULT ''"),('share_content','INTEGER NOT NULL DEFAULT 0')]:
                if name not in columns:
                    db.execute('ALTER TABLE self_jobs ADD COLUMN '+name+' '+definition)
            from .quality_catalog import defaults as question_defaults, validate as validate_questions
            db.execute('INSERT OR IGNORE INTO console_documents VALUES (?,1,?)',('questions',dumps(validate_questions(question_defaults()))))
            for key, value in [('settings', defaults()), ('inventory', {'accounts': [], 'groups': [], 'synced_at': None}),
                               ('publication', {'group_names': {}, 'account_names': {}}),
                               ('access', {'allow_anonymous_results': False})]:
                db.execute('INSERT OR IGNORE INTO console_documents VALUES (?,1,?)', (key, dumps(value)))
            # Migrate existing deployment into a disabled-timer plan, without a paid request.
            if not db.execute("SELECT 1 FROM console_documents WHERE key='migration_v1'").fetchone():
                now = time.time()
                for name, spec in config.enabled_platforms().items():
                    group_ids = config.evaluation_scope_ids(name)
                    if not group_ids:
                        continue
                    plan = {'name': (spec.get('label') or name) + ' 质量检测', 'enabled': True,
                            'scope': {'mode': 'groups', 'group_ids': group_ids, 'account_ids': [], 'exclude_ids': []},
                            'models': defaults()['platforms'], 'kinds': ['candy', 'drawing'],
                            'schedule': {**defaults()['schedule'], 'enabled': False}, 'published': True,
                            'public_name': (spec.get('label') or name) + ' 质量检测',
                            'show_accounts': True, 'show_answers': False, 'show_artifacts': False,
                            'write_priority': False, 'write_callable': False}
                    db.execute('INSERT INTO console_plans VALUES (?,1,?,?,?)',
                               (uuid.uuid4().hex, dumps(plan), now, now))
                db.execute('INSERT INTO console_documents VALUES (?,?,?)', ('migration_v1', 1, '{}'))
            if not db.execute("SELECT 1 FROM console_documents WHERE key='inspection_v3'").fetchone():
                for row in db.execute('SELECT id,value FROM console_plans').fetchall():
                    value=json.loads(row['value'])
                    value.pop('isolation',None)
                    value['write_priority']=False
                    value['write_callable']=False
                    db.execute('UPDATE console_plans SET value=? WHERE id=?',(dumps(value),row['id']))
                # Old isolation evidence is retained for rollback/audit only.
                # No code in this service reads it or modifies gateway routing.
                db.execute('INSERT INTO console_documents VALUES (?,?,?)',('inspection_v3',1,'{}'))
            from .question_migrations import candy_single
            candy_single(db)
        os.chmod(self.path, 0o600)

    @contextmanager
    def db(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=15)
            db.row_factory = sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                db.close()

    def document(self, key):
        with self.db() as db:
            row = db.execute('SELECT * FROM console_documents WHERE key=?', (key,)).fetchone()
        return {'revision': row['revision'], 'value': json.loads(row['value'])}

    def save_document(self, key, value, revision, actor):
        if type(revision) is not int or revision < 1:
            raise ConsoleError('配置版本无效')
        if key == 'settings':
            value = validate_settings(value)
        elif key == 'access':
            if (not isinstance(value, dict) or set(value) != {'allow_anonymous_results'} or
                    type(value['allow_anonymous_results']) is not bool):
                raise ConsoleError('结果访问配置必须包含有效的匿名访问开关')
        elif key == 'publication':
            if not isinstance(value, dict) or set(value) != {'group_names', 'account_names'}:
                raise ConsoleError('公开名称配置无效')
            from .console_config import text
            for names in value.values():
                if not isinstance(names, dict) or len(names) > 1000:
                    raise ConsoleError('公开名称数量超出限制')
                for name_id, name in names.items():
                    if not name_id.isdigit() or int(name_id) <= 0:
                        raise ConsoleError('公开名称 ID 无效')
                    text(name, '公开名称', 80)
        elif key == 'questions':
            from .quality_catalog import validate
            value=validate(value)
        with self.db() as db:
            changed = db.execute('UPDATE console_documents SET revision=revision+1,value=? WHERE key=? AND revision=?',
                                 (dumps(value), key, revision)).rowcount
            if not changed:
                raise ConsoleError('配置已被其他操作更新，请刷新后重试', 409)
            self.audit(db, actor, 'save_' + key, key)
        return self.document(key)

    @staticmethod
    def audit(db, actor, action, object_id):
        db.execute('INSERT INTO console_audit(actor,action,object_id,created_at) VALUES (?,?,?,?)',
                   (str(actor), action, object_id, time.time()))

    def inventory(self):
        return self.document('inventory')

    def save_inventory(self, accounts, groups, actor):
        clean_accounts = []
        for account in accounts:
            group_ids = account.get('group_ids')
            if not isinstance(group_ids, list) or any(type(x) is not int for x in group_ids):
                raise ConsoleError('账号分组信息不完整，保留原清单', 502)
            clean_accounts.append({'id': account['id'], 'name': str(account['name'])[:200],
                                   'platform': str(account.get('platform', ''))[:32],
                                   'type': str(account.get('type', 'unknown'))[:32],
                                   'status': str(account.get('status', 'unknown'))[:32],
                                   'priority': account.get('priority') if type(account.get('priority')) is int else None,
                                   **{key: account.get(key) for key in ('schedulable', 'last_used_at', 'expires_at',
                                       'rate_limit_reset_at', 'overload_until', 'temp_unschedulable_until')},
                                   'group_ids': sorted(set(group_ids))})
        if not isinstance(groups, list) or any(type(g.get('id')) is not int for g in groups):
            raise ConsoleError('网关分组清单不完整，保留原清单', 502)
        value = {'accounts': clean_accounts,
                 'groups': [{'id': g['id'], 'name': str(g.get('name', ''))[:120],
                             'platform': str(g.get('platform', ''))[:32]} for g in groups],
                 'synced_at': time.time()}
        with self.db() as db:
            db.execute("UPDATE console_documents SET value=?,revision=revision+1 WHERE key='inventory'", (dumps(value),))
            self.audit(db, actor, 'sync_inventory', 'inventory')
        return self.inventory()

    def plans(self):
        with self.db() as db:
            rows = db.execute('SELECT * FROM console_plans ORDER BY created_at').fetchall()
        return [self.plan_row(r) for r in rows]

    @staticmethod
    def plan_row(row):
        return {'id': row['id'], 'revision': row['revision'], 'value': json.loads(row['value']),
                'updated_at': row['updated_at']}

    def plan(self, ident):
        with self.db() as db:
            row = db.execute('SELECT * FROM console_plans WHERE id=?', (ident,)).fetchone()
        if not row:
            raise ConsoleError('检测计划不存在', 404)
        return self.plan_row(row)

    def save_plan(self, value, actor, ident=None, revision=None):
        value = validate_plan(value)
        from .quality_catalog import select
        if not value.get('questions'):
            select(self,value['kinds'])
        if ident and (type(revision) is not int or revision < 1):
            raise ConsoleError('计划版本无效')
        with self.db() as db:
            if ident:
                if not db.execute('UPDATE console_plans SET value=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?',
                                  (dumps(value), time.time(), ident, revision)).rowcount:
                    raise ConsoleError('计划已被更新，请刷新后重试', 409)
            else:
                if db.execute('SELECT count(*) FROM console_plans').fetchone()[0] >= 100:
                    raise ConsoleError('检测计划最多 100 个')
                ident = uuid.uuid4().hex
                db.execute('INSERT INTO console_plans VALUES (?,1,?,?,?)', (ident, dumps(value), time.time(), time.time()))
            self.audit(db, actor, 'save_plan', ident)
        return self.plan(ident)

    def resolve(self, plan):
        settings = self.document('settings')
        inventory = self.inventory()
        value = plan['value']
        scope = value['scope']
        selected, skipped = [], []
        found = set()
        for a in inventory['value']['accounts']:
            match = (bool(set(scope['group_ids']) & set(a['group_ids'])) if scope['mode'] == 'groups'
                     else a['id'] in scope['account_ids'])
            if not match or a['id'] in scope['exclude_ids']:
                continue
            found.add(a['id'])
            model = value['models'].get(a['platform'])
            if not model or not model['enabled'] or a['status'] not in ('active', 'inactive', 'error'):
                skipped.append({'id': a['id'], 'reason': '平台模型未启用或账号状态不支持'})
                continue
            selected.append(deepcopy(a))
        if scope['mode'] == 'accounts':
            skipped.extend({'id': i, 'reason': '账号已移除或清单未同步'} for i in set(scope['account_ids']) - found)
        selection = value.get('selection', {})
        limit = inspection_attempt_limit(value)
        if limit is not None:
            selected, omitted = inspection_candidates(selected, min(limit, settings['value']['max_accounts']))
            skipped.extend(omitted)
        elif selection.get('mode') == 'recent_active':
            selected, omitted = recent_active(selected, selection['count'])
            skipped.extend(omitted)
        if len(selected) > settings['value']['max_accounts']:
            raise ConsoleError('检测账号数超过每轮上限，请缩小范围')
        attempts = settings['value']['budgets']['max_attempts']
        from .quality_catalog import select
        units=sum(q['rounds'] for q in (value.get('questions') or select(self,value['kinds'])))
        return {'accounts': selected, 'skipped': skipped, 'account_count': len(selected),
                **({'max_account_attempts': limit} if limit is not None else {}),
                'max_requests': len(selected) * attempts * units, 'estimated_cost': None,
                'inventory_revision': inventory['revision'], 'settings_revision': settings['revision'],
                'questions_revision':self.document('questions')['revision'],
                'plan_revision': plan['revision'], 'method': 'account-direct'}

    def create_job(self, plan_id, request_key, actor, source, expected, target_ids=None):
        if not isinstance(plan_id, str) or len(plan_id) != 32:
            raise ConsoleError('无效检测计划')
        if not isinstance(request_key, str) or not 16 <= len(request_key) <= 100:
            raise ConsoleError('无效任务幂等标识')
        with self.db() as db:
            row = db.execute('SELECT id,plan_id FROM console_jobs WHERE request_key=?', (request_key,)).fetchone()
            if row:
                if row['plan_id'] != plan_id:
                    raise ConsoleError('幂等标识已用于其他计划', 409)
                return self.job(row['id']), False
            plan = self.plan(plan_id)
            if not plan['value']['enabled']:
                raise ConsoleError('计划已停用')
            preview = self.resolve(plan)
            if target_ids is not None:
                preview['accounts']=[a for a in preview['accounts'] if a['id'] in target_ids]
                preview['account_count']=len(preview['accounts'])
            for key in ('plan_revision', 'settings_revision', 'inventory_revision','questions_revision'):
                if expected.get(key) != preview[key]:
                    raise ConsoleError('检测范围或配置已变化，请重新预览并确认', 409)
            if not preview['account_count'] and not (source in ('manual', 'timer') and preview.get('max_account_attempts')):
                raise ConsoleError('当前范围没有可检测账号，请同步或检查平台模型')
            if db.execute("SELECT count(*) FROM console_jobs WHERE status IN ('queued','running')").fetchone()[0] >= 20:
                raise ConsoleError('任务队列已满，请稍后重试', 409)
            settings = self.document('settings')
            from .quality_catalog import select
            snapshot = {'plan': plan, 'settings': settings, 'targets': preview['accounts'],
                        'questions':plan['value'].get('questions') or select(self,plan['value']['kinds']),
                        'skipped': preview['skipped'], 'preview': preview}
            ident = uuid.uuid4().hex
            db.execute('INSERT INTO console_jobs(id,request_key,plan_id,status,source,actor,created_at,snapshot) VALUES (?,?,?,?,?,?,?,?)',
                       (ident, request_key, plan_id, 'queued', source, str(actor), time.time(), dumps(snapshot)))
            self.audit(db, actor, 'create_job', ident)
        return self.job(ident), True

    def jobs(self):
        with self.db() as db:
            rows = db.execute('SELECT * FROM console_jobs ORDER BY created_at DESC LIMIT 200').fetchall()
        return [self.job_row(row) for row in rows]

    def empty_inspections(self, cutoff):
        with self.db() as db:
            rows = db.execute('''SELECT j.* FROM console_jobs j WHERE j.created_at >= ?
                AND j.results_pruned = 0 AND j.status = 'completed_with_errors' AND j.source IN ('manual','timer')
                AND json_array_length(j.snapshot, '$.targets') = 0
                AND json_extract(j.snapshot, '$.preview.max_account_attempts') > 0''', (cutoff,)).fetchall()
        return [self.job_row(row) for row in rows]

    @staticmethod
    def job_row(row):
        result = dict(row)
        result['snapshot'] = json.loads(result['snapshot'])
        return result

    def job(self, ident):
        with self.db() as db:
            row = db.execute('SELECT * FROM console_jobs WHERE id=?', (ident,)).fetchone()
        if not row:
            raise ConsoleError('任务不存在', 404)
        return self.job_row(row)

    def update_job(self, ident, **changes):
        if set(changes) - {'status', 'started_at', 'finished_at', 'message'}:
            raise ValueError('INVALID_JOB_UPDATE')
        with self.db() as db:
            db.execute('UPDATE console_jobs SET ' + ','.join(k + '=?' for k in changes) + ' WHERE id=?', (*changes.values(), ident))

    def recover_jobs(self):
        with self.db() as db:
            db.execute("UPDATE console_jobs SET status='interrupted',finished_at=?,message='服务已重启，任务未自动重发' WHERE status IN ('running','queued')", (time.time(),))
            db.execute("UPDATE self_jobs SET status='interrupted',finished_at=?,message='服务已重启，任务未自动重发' WHERE status IN ('running','queued')", (time.time(),))
    def create_self_job(self, user_id, api_key_id, key_name, model, effort, kinds, public=False, questions=None, group_id=None, group_name='',share_content=False):
        ident, now = uuid.uuid4().hex, time.time()
        with self.db() as db:
            db.execute('INSERT INTO self_jobs(id,user_id,api_key_id,key_name,status,model,effort,kinds,public,created_at,results,questions,group_id,group_name,share_content) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (ident, str(user_id), int(api_key_id), str(key_name)[:120], 'queued', str(model)[:160], str(effort), dumps(kinds), 1 if public else 0, now, '[]',dumps(questions or []),group_id,str(group_name or '')[:120],int(share_content)))
        return self.self_job(ident)

    @staticmethod
    def self_job_row(row):
        value = dict(row)
        value['kinds'] = json.loads(value['kinds'])
        value['results'] = json.loads(value['results'])
        value['questions'] = json.loads(value['questions'])
        value['public'] = bool(value['public'])
        return value

    def self_job(self, ident):
        with self.db() as db:
            row = db.execute('SELECT * FROM self_jobs WHERE id=?', (ident,)).fetchone()
        if not row:
            raise ConsoleError('自助检测任务不存在', 404)
        return self.self_job_row(row)

    def self_jobs(self, user_id=None, public_only=False):
        query, args, clauses = 'SELECT * FROM self_jobs', [], []
        if user_id is not None:
            clauses.append('user_id=?'); args.append(str(user_id))
        if public_only:
            clauses.append('public=1')
        if clauses:
            query += ' WHERE ' + ' AND '.join(clauses)
        query += ' ORDER BY created_at DESC LIMIT 200'
        with self.db() as db:
            rows = db.execute(query, args).fetchall()
        return [self.self_job_row(row) for row in rows]

    def update_self_job(self, ident, **changes):
        allowed = {'status', 'started_at', 'finished_at', 'message', 'results', 'public','share_content'}
        if set(changes) - allowed:
            raise ValueError('INVALID_SELF_JOB_UPDATE')
        values = dict(changes)
        if 'results' in values:
            values['results'] = dumps(values['results'])
        if 'public' in values:
            values['public'] = 1 if values['public'] else 0
        with self.db() as db:
            db.execute('UPDATE self_jobs SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?', (*values.values(), ident))

    def import_results(self, job):
        root = self.private / 'jobs' / job['id']
        if job['snapshot'].get('questions'):
            file=root/'results.json'
            if file.exists():
                self.import_quality_results(job,json.loads(file.read_text()))
            return
        statefile = root / 'private' / 'state.sqlite3'
        if not statefile.exists():
            return
        targets = {a['id']: a for a in job['snapshot']['targets']}
        with sqlite3.connect('file:' + str(statefile) + '?mode=ro', uri=True) as source:
            source.row_factory = sqlite3.Row
            rows = source.execute('SELECT * FROM runs').fetchall()
        with self.db() as db:
            for row in rows:
                if row['account_id'] not in targets:
                    continue
                value = {k: row[k] for k in ('id', 'kind', 'status', 'slot', 'duration', 'finished_at', 'model', 'effort', 'platform', 'bank_version', 'attempts', 'answer', 'error_code', 'error')}
                if value['status'] in ('pending', 'running', 'paused'):
                    value['status'] = 'cancelled' if (root / 'stop').exists() else 'error'
                profile = hashlib.sha256(dumps([row['platform'], row['model'], row['effort'], row['kind'], row['bank_version']]).encode()).hexdigest()[:16]
                db.execute('INSERT OR REPLACE INTO console_results VALUES (?,?,?,?,?,?,?,?)',
                           (row['id'], job['id'], job['plan_id'], row['account_id'], dumps(targets[row['account_id']]['group_ids']),
                            row['slot'], profile, dumps(value)))

    def results(self):
        cutoff = time.time() - self.document('settings')['value']['retention']['artifacts_hours'] * 3600
        with self.db() as db:
            rows = db.execute('''SELECT r.* FROM console_results r JOIN console_jobs j ON j.id=r.job_id
                WHERE r.slot>=? AND j.results_pruned=0 ORDER BY r.slot DESC''', (cutoff,)).fetchall()
        return [{**dict(r), 'value': json.loads(r['value']), 'group_ids': json.loads(r['group_ids'])} for r in rows]

    def quarantine_evidence(self, job_id, account_id):
        with self.db() as db:
            rows = db.execute('SELECT value FROM console_results WHERE job_id=? AND account_id=?',
                              (job_id, account_id)).fetchall()
        return [json.loads(row['value']) for row in rows]

    def prune_results(self, cutoff, protected_jobs):
        with self.db() as db:
            for plan in db.execute('SELECT id,value FROM console_plans').fetchall():
                value = json.loads(plan['value'])
                limit = value.get('max_saved_rounds')
                if limit is None:
                    continue
                # Count rounds started for the current group even when a plan
                # used to point at a different group.
                db.execute('''UPDATE console_jobs SET results_pruned=1 WHERE id IN (
                    SELECT j.id FROM console_jobs j WHERE j.plan_id=? AND j.created_at>=?
                      AND j.results_pruned=0 AND j.finished_at IS NOT NULL
                      AND j.source IN ('manual','timer','cooldown')
                      AND json_extract(j.snapshot, '$.plan.value.scope.mode')='groups'
                      AND json_array_length(j.snapshot, '$.plan.value.scope.group_ids')=1
                      AND json_extract(j.snapshot, '$.plan.value.scope.group_ids[0]')=?
                      AND (EXISTS (SELECT 1 FROM console_results r WHERE r.job_id=j.id) OR (
                        j.status='completed_with_errors' AND j.source IN ('manual','timer')
                        AND json_array_length(j.snapshot, '$.targets')=0
                        AND json_extract(j.snapshot, '$.preview.max_account_attempts') > 0))
                    ORDER BY j.created_at DESC,j.rowid DESC LIMIT -1 OFFSET ?)''',
                    (plan['id'], cutoff, value['scope']['group_ids'][0], limit))
            db.execute('CREATE TEMP TABLE protected_inspection_jobs (id TEXT PRIMARY KEY)')
            db.executemany('INSERT INTO protected_inspection_jobs VALUES (?)', ((i,) for i in protected_jobs))
            db.execute('''DELETE FROM console_results WHERE (slot<? OR job_id IN (
                    SELECT id FROM console_jobs WHERE results_pruned=1))
                AND job_id NOT IN (SELECT id FROM protected_inspection_jobs)''', (cutoff,))
            old = [row['id'] for row in db.execute('''SELECT id FROM console_jobs
                WHERE (results_pruned=1 OR finished_at<?) AND status NOT IN ('running','queued')''', (cutoff,))]
            db.execute('DELETE FROM console_audit WHERE created_at<?', (time.time() - 90 * 86400,))
        return old

    def import_quality_results(self,job,rows):
        targets={a['id']:a for a in job['snapshot']['targets']}
        with self.db() as db:
            for row in rows:
                if row['account_id'] not in targets: continue
                profile=hashlib.sha256(dumps([row['platform'],row['model'],row['effort'],row['kind'],row['bank_version']]).encode()).hexdigest()[:16]
                db.execute('INSERT OR REPLACE INTO console_results VALUES (?,?,?,?,?,?,?,?)',
                    (row['id'],job['id'],job['plan_id'],row['account_id'],dumps(row['group_ids']),row['slot'],profile,dumps(row)))

    def public_id(self, kind, ident):
        return hashlib.sha256((config.config_salt() + ':console:' + kind + ':' + str(ident)).encode()).hexdigest()[:24]
