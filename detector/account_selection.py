"""Choose recent, callable accounts from an already authorized plan scope."""
from datetime import datetime
import math
import time


def timestamp(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except (ValueError, OverflowError):
            return None
    return value if type(value) in (float, int) and math.isfinite(value) and value > 0 else None


def recent_active(accounts, count, now=None):
    now = time.time() if now is None else now
    eligible, skipped = [], []
    for account in accounts:
        used = timestamp(account.get('last_used_at'))
        blocked = any((timestamp(account.get(field)) or 0) > now for field in (
            'rate_limit_reset_at', 'overload_until', 'temp_unschedulable_until'))
        expires = timestamp(account.get('expires_at'))
        if account['status'] != 'active' or account.get('schedulable') is not True or blocked or (expires and expires <= now):
            reason = '账号当前未启用、不可调度、限额或处于暂停期'
        elif used is None or used > now + 300:
            reason = '没有有效的最近使用记录，本轮不以默认账号代替'
        else:
            eligible.append((used, account))
            continue
        skipped.append({'id': account['id'], 'reason': reason})
    eligible.sort(key=lambda item: (-item[0], item[1]['id']))
    skipped.extend({'id': account['id'], 'reason': '本轮按最近使用时间抽样，未选中'} for _, account in eligible[count:])
    return [account for _, account in eligible[:count]], skipped
