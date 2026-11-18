"""Choose recent, callable accounts from an already authorized plan scope."""
from datetime import datetime
import math
import time

DEFAULT_ACCOUNT_ATTEMPTS = 3
MAX_ACCOUNT_ATTEMPTS = 10


def timestamp(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except (ValueError, OverflowError):
            return None
    return value if type(value) in (float, int) and math.isfinite(value) and value > 0 else None


def unavailable(account, now):
    expires = timestamp(account.get('expires_at'))
    return (account.get('status') != 'active' or account.get('schedulable') is not True or
            (expires is not None and expires <= now) or
            any((timestamp(account.get(field)) or 0) > now for field in (
                'rate_limit_reset_at', 'overload_until', 'temp_unschedulable_until')))


def inspection_attempt_limit(plan):
    selection = plan.get('selection', {})
    scope = plan['scope']
    if scope['mode'] == 'groups' and len(scope['group_ids']) == 1 and (
            selection.get('mode') == 'recent_active' and selection.get('count') == 1):
        return selection.get('max_account_attempts', DEFAULT_ACCOUNT_ATTEMPTS)
    return None


def inspection_candidates(accounts, limit, now=None):
    now = time.time() if now is None else now
    eligible, skipped = [], []
    for account in accounts:
        if unavailable(account, now):
            skipped.append({'id': account['id'], 'reason': '账号未启用、不可调度、过期或处于暂停期'})
            continue
        used = timestamp(account.get('last_used_at'))
        priority = account.get('priority')
        eligible.append((used if used is not None and used <= now + 300 else 0,
                         priority if type(priority) is int else math.inf, account))
    active = [item for item in eligible if item[0]]
    first = min(active, key=lambda item: (-item[0], item[1], item[2]['id'])) if active else None
    ordered = sorted((item for item in eligible if item is not first),
                     key=lambda item: (item[1], -item[0], item[2]['id']))
    if first is not None:
        ordered.insert(0, first)
    skipped.extend({'id': account['id'], 'reason': '超出本轮尝试账号上限'} for _, _, account in ordered[limit:])
    return [account for _, _, account in ordered[:limit]], skipped


def recent_active(accounts, count, now=None):
    now = time.time() if now is None else now
    eligible, skipped = [], []
    for account in accounts:
        used = timestamp(account.get('last_used_at'))
        if unavailable(account, now):
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
