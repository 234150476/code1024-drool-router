"""Sampling, quiet hours and credentials are exercised without model spending."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from detector import config
from detector.account_selection import recent_active
from detector.console_config import ConsoleError, schedule
from detector.console_service import ConsoleService, schedule_slot
from detector.credential_snapshot import read
from detector.upstream import Sub2API, ProbeError


def at(clock):
    return datetime.fromisoformat('2026-11-17T'+clock).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()


class InspectionTests(unittest.TestCase):
    def setUp(self):
        self.spec={'enabled':True,'timezone':'Asia/Shanghai','regular_minutes':60,'quiet_start':'22:00','quiet_end':'09:00','quiet_minutes':60,'quiet_pause':True}

    def test_thirteen_daytime_slots_and_no_night_catch_up(self):
        for hour in range(24):
            slot=schedule_slot(self.spec,at('%02d:30'%hour))
            if 9<=hour<22:self.assertEqual(slot,at('%02d:00'%hour))
            else:self.assertIsNone(slot)
        self.assertEqual(schedule_slot(self.spec,at('09:00')),at('09:00'))
        self.assertIsNone(schedule_slot(self.spec,at('22:00')))
        self.assertEqual(schedule_slot(self.spec,at('21:59')),at('21:00'))

    def test_old_interval_schedules_still_work_and_pause_requires_boolean(self):
        old={k:v for k,v in self.spec.items() if k!='quiet_pause'}
        self.assertIsNotNone(schedule_slot(schedule(old),at('22:00')))
        with self.assertRaises(ConsoleError):schedule({**self.spec,'quiet_pause':'true'})

    def test_recent_callable_account_wins_and_paused_accounts_never_win(self):
        def account(ident,used,**kwargs):return {'id':ident,'status':'active','schedulable':True,'last_used_at':used,**kwargs}
        rows=[account(1,90),account(2,99,schedulable=False),account(3,95),account(4,100,status='error'),account(5,None),account(6,98,rate_limit_reset_at=200)]
        selected,skipped=recent_active(rows,1,now=100)
        self.assertEqual([a['id'] for a in selected],[3]);self.assertEqual(len(skipped),5)
        self.assertEqual(recent_active([account(1,None)],1,now=100)[0],[])

    def test_snapshot_supplies_credentials_and_quota_without_export_permission(self):
        with tempfile.TemporaryDirectory() as root:
            file=Path(root)/'accounts.json'
            original={'id':1,'name':'Plus account','type':'oauth','platform':'openai','status':'active','schedulable':True,'group_ids':[3]}
            entry={**original,'group_ids':[3,9],'credentials':{'access_token':'fixture-private-value'},'last_used_at':time.time()-1,'extra':{},'_quota_policy':{'account_scheduling_threshold':100}}
            file.write_text(json.dumps({'version':1,'generated_at':time.time(),'accounts':{'1':entry}}))
            api=Sub2API('http://unused.invalid','fixture-machine-key')
            with patch.dict('os.environ',{'SUB2API_ACCOUNT_SNAPSHOT_FILE':str(file)}),patch.object(api,'get',return_value=deepcopy(original)) as get:
                value=api.account(1)
                self.assertEqual(value['credentials']['access_token'],'fixture-private-value')
                self.assertEqual(value['group_ids'],[3,9])
                self.assertEqual(get.call_args_list[0].args,('/accounts/1',))
                self.assertEqual(get.call_count,1)
            file.write_text(json.dumps({'version':1,'generated_at':time.time()-601,'accounts':{'1':entry}}))
            with self.assertRaises(ProbeError):read(file)

    def test_group_change_fails_closed(self):
        from detector.credential_snapshot import entry
        with tempfile.TemporaryDirectory() as root:
            file=Path(root)/'accounts.json'
            account={'id':1,'name':'Plus','type':'oauth','platform':'openai','group_ids':[3]}
            file.write_text(json.dumps({'version':1,'generated_at':time.time(),'accounts':{'1':account}}))
            with self.assertRaises(ProbeError):entry(file,{**account,'group_ids':[2]})

    def test_night_tick_makes_no_inventory_or_model_request(self):
        with tempfile.TemporaryDirectory() as root:
            service=ConsoleService(root,start=False)
            service.store.save_inventory([{'id':1,'name':'Plus','platform':'openai','type':'apikey','status':'active','schedulable':True,'last_used_at':time.time(),'group_ids':[3]}],[{'id':3,'name':'Plus','platform':'openai'}],'admin')
            from detector.inspection import save
            with patch.object(service,'machine_key',return_value='fixture-machine-key'):
                plan=save(service,{'group_id':3,'enabled':True,'model':'fixture','effort':'high','question_ids':['drawing'],'questions':[],'schedule':self.spec},'admin')
                with patch('detector.console_service.time.time',return_value=at('22:30')),patch.object(service,'sync_inventory') as sync:
                    service.tick();self.assertEqual(service.store.jobs(),[]);sync.assert_not_called()
                value=service.store.plan(plan['id'])['value'];value['isolation']={'enabled':True,'failure_rounds':2,'cooldown_minutes':60,'min_available':1}
                with self.assertRaises(ConsoleError):service.save_plan(value,'admin')
