"""Durable ownership, write intents and atomic cooldown task creation."""
from copy import deepcopy
import json
import time
import uuid

from .console_config import ConsoleError
from .console_store import dumps

CLOSED = ('recovered', 'manual')


class QuarantineStore:
    def __init__(self, store):
        self.store = store
        with store.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS inspection_quarantine (
                account_id INTEGER PRIMARY KEY, state TEXT NOT NULL, due_at REAL NOT NULL,
                value TEXT NOT NULL, updated_at REAL NOT NULL)''')

    def entries(self):
        with self.store.db() as db:
            rows = db.execute('SELECT value FROM inspection_quarantine ORDER BY updated_at DESC').fetchall()
        return [json.loads(row['value']) for row in rows]

    def get(self, account_id):
        with self.store.db() as db:
            row = db.execute('SELECT value FROM inspection_quarantine WHERE account_id=?', (account_id,)).fetchone()
        return json.loads(row['value']) if row else None

    def _write(self, db, record, action, actor='quarantine'):
        record = {**record, 'revision': record.get('revision', 0)+1, 'updated_at': time.time()}
        db.execute('''INSERT INTO inspection_quarantine VALUES (?,?,?,?,?)
            ON CONFLICT(account_id) DO UPDATE SET state=excluded.state,due_at=excluded.due_at,
            value=excluded.value,updated_at=excluded.updated_at''',
            (record['account_id'], record['state'], record['due_at'], dumps(record), record['updated_at']))
        self.store.audit(db, actor, action, str(record['account_id']))
        return record

    def claim(self, job, account, questions):
        with self.store.db() as db:
            previous = self.get(account['id'])
            if previous and previous['state'] not in CLOSED:
                return None
            profile = deepcopy(job['snapshot']['plan'])
            profile['value']['questions'] = deepcopy(questions)
            profile['value']['kinds'] = [q['id'] for q in questions]
            record = {'account_id': account['id'], 'account': {k: account.get(k) for k in
                      ('id', 'name', 'platform', 'type', 'group_ids')},
                      'plan_id': job['plan_id'], 'generation': uuid.uuid4().hex,
                      'state': 'disabling', 'due_at': 0, 'attempts': 0,
                      'evidence_job_id': job['id'], 'recheck_job_id': None,
                      'profile': profile, 'error_code': None, 'created_at': time.time()}
            return self._write(db, record, 'quarantine_intent')

    def change(self, record, *, action, actor='quarantine', **changes):
        with self.store.db() as db:
            current = self.get(record['account_id'])
            if (not current or current['generation'] != record['generation'] or
                    current.get('revision') != record.get('revision')):
                raise ConsoleError('剔除状态已变化，请刷新', 409)
            return self._write(db, {**current, **changes}, action, actor)

    def queue_recheck(self, record, account, current_plan):
        with self.store.db() as db:
            fresh = self.get(record['account_id'])
            if (not fresh or fresh['generation'] != record['generation'] or fresh['state'] != 'cooling' or
                    fresh.get('revision') != record.get('revision')):
                return None
            if db.execute("SELECT count(*) FROM console_jobs WHERE status IN ('running','queued')").fetchone()[0] >= 20:
                return None
            profile = deepcopy(record['profile'])
            profile['value']['schedule'] = deepcopy(current_plan['value']['schedule'])
            profile['value']['selection'] = {'mode': 'all', 'count': 1}
            target = {k: account.get(k) for k in ('id', 'name', 'platform', 'type', 'status', 'schedulable', 'group_ids')}
            snapshot = {'plan': profile, 'settings': self.store.document('settings'), 'targets': [target],
                        'questions': profile['value']['questions'], 'skipped': [], 'preview': {},
                        'recovery': {'account_id': account['id'], 'generation': record['generation']}}
            ident = uuid.uuid4().hex
            db.execute('''INSERT INTO console_jobs(id,request_key,plan_id,status,source,actor,created_at,snapshot)
                VALUES (?,?,?,'queued','cooldown','quarantine',?,?)''',
                (ident, 'cooldown:'+ident, record['plan_id'], time.time(), dumps(snapshot)))
            self.store.audit(db, 'quarantine', 'create_recheck', ident)
            self._write(db, {**fresh, 'state': 'rechecking', 'recheck_job_id': ident, 'error_code': None}, 'quarantine_recheck')
            return ident
