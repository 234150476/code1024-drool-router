"""Deleting an inspection removes its history and refuses while work is pending."""
import unittest
import uuid

from detector.console_config import ConsoleError
from detector.plan_deletion import delete_plan
from detector.tests.test_inspection import InspectionFallbackTests


def answer(*args, **kwargs):
    return {'text': '最终答案：323' if kwargs['prompt'].startswith('计算') else '最终答案：3'}


class PlanDeletionTests(unittest.TestCase):
    setUp = InspectionFallbackTests.setUp
    run_job = InspectionFallbackTests.run_job

    def quarantine(self, state):
        with self.service.store.db() as db:
            self.service.quarantine.records._write(db, {
                'account_id': 1, 'plan_id': self.plan['id'], 'state': state, 'due_at': 0,
                'generation': uuid.uuid4().hex}, 'fixture')

    def test_delete_removes_plan_jobs_results_and_artifacts(self):
        from detector.inspection import save
        other = save(self.service, {**self.payload, 'group_id': 4}, 'admin')
        job = self.run_job(answer)
        store = self.service.store
        job_dir = store.private / 'jobs' / job['id']
        self.assertTrue(job_dir.exists())
        self.assertTrue(store.results())
        self.quarantine('recovered')
        result = delete_plan(self.service, self.plan['id'], self.plan['revision'], 'admin')
        self.assertEqual(result, {'id': self.plan['id'], 'deleted': True, 'jobs': 1})
        self.assertEqual([p['id'] for p in store.plans()], [other['id']])
        self.assertEqual(store.results(), [])
        self.assertFalse(job_dir.exists())
        self.assertIsNone(self.service.quarantine.records.get(1))
        with self.assertRaises(ConsoleError) as error:
            store.job(job['id'])
        self.assertEqual(error.exception.status, 404)
        self.assertEqual(self.service.public.history(store.public_id('group', 'g3')), [])

    def test_stale_revision_is_rejected(self):
        with self.assertRaises(ConsoleError) as error:
            delete_plan(self.service, self.plan['id'], self.plan['revision'] + 1, 'admin')
        self.assertEqual(error.exception.status, 409)
        self.service.store.plan(self.plan['id'])

    def test_active_quarantine_blocks_delete(self):
        for state in ('cooling', 'blocked', 'rechecking'):
            self.quarantine(state)
            with self.assertRaises(ConsoleError) as error:
                delete_plan(self.service, self.plan['id'], self.plan['revision'], 'admin')
            self.assertEqual(error.exception.status, 409)
        self.service.store.plan(self.plan['id'])

    def test_queued_job_blocks_delete(self):
        store = self.service.store
        plan = store.plan(self.plan['id'])
        store.create_job(plan['id'], uuid.uuid4().hex, 'admin', 'manual', store.resolve(plan))
        with self.assertRaises(ConsoleError) as error:
            delete_plan(self.service, self.plan['id'], self.plan['revision'], 'admin')
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(len(store.jobs()), 1)


if __name__ == '__main__':
    unittest.main()
