"""Delete one inspection plan together with its jobs, results and artifacts."""
import shutil

from .console_config import ConsoleError
from .quarantine_store import CLOSED


def delete_plan(service, ident, revision, actor):
    if type(revision) is not int or revision < 1:
        raise ConsoleError('计划版本无效')
    store = service.store
    # The service lock serialises the scheduler, job start and quarantine
    # callbacks, so no new job or quarantine claim can appear mid-delete.
    with service.lock:
        plan = store.plan(ident)
        if plan['revision'] != revision:
            raise ConsoleError('计划已被更新，请刷新后重试', 409)
        records = [r for r in service.quarantine.records.entries() if r['plan_id'] == ident]
        if any(r['state'] not in CLOSED for r in records):
            raise ConsoleError('此分组还有自动剔除托管中的账号，请先在「自动剔除管理」停止托管或等待恢复', 409)
        with store.db() as db:
            if db.execute("SELECT 1 FROM console_jobs WHERE plan_id=? AND status IN ('queued','running')",
                          (ident,)).fetchone():
                raise ConsoleError('此分组还有排队或运行中的巡检任务，请先停止任务', 409)
            jobs = [row['id'] for row in db.execute('SELECT id FROM console_jobs WHERE plan_id=?', (ident,))]
            db.execute('DELETE FROM console_results WHERE plan_id=?', (ident,))
            db.execute('DELETE FROM console_jobs WHERE plan_id=?', (ident,))
            db.executemany('DELETE FROM inspection_quarantine WHERE account_id=?',
                           ((r['account_id'],) for r in records))
            if not db.execute('DELETE FROM console_plans WHERE id=? AND revision=?', (ident, revision)).rowcount:
                raise ConsoleError('计划已被更新，请刷新后重试', 409)
            store.audit(db, actor, 'delete_plan', ident)
        for job_id in jobs:
            service.tokens.pop(job_id, None)
    for job_id in jobs:
        path = store.private / 'jobs' / job_id
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    return {'id': ident, 'deleted': True, 'jobs': len(jobs)}
