"""Read an administrator-exported snapshot without granting export to a machine key."""
import json
from pathlib import Path
import time


def read(path):
    from .upstream import ProbeError
    try:
        value = json.loads(Path(path).read_text())
        if value.get('version') != 1 or not isinstance(value.get('accounts'), dict):
            raise ValueError('format')
        generated = value['generated_at']
        if type(generated) not in (int, float) or not 0 <= time.time() - generated <= 600:
            raise ValueError('stale')
        return value['accounts']
    except (OSError, KeyError, ValueError, TypeError, AttributeError):
        raise ProbeError('ACCOUNT_SNAPSHOT_UNAVAILABLE', '账号凭据快照缺失、无效或已超过十分钟，未请求模型；请检查管理员凭据同步服务。') from None


def entry(path, account):
    from .upstream import ProbeError
    row = read(path).get(str(account['id']))
    if (not isinstance(row, dict) or row.get('name') != account['name'] or
            row.get('platform') != account['platform'] or row.get('type') != account['type'] or
            not set(account.get('group_ids', [])) <= set(row.get('group_ids', []))):
        raise ProbeError('ACCOUNT_SNAPSHOT_IDENTITY_CHANGED', '账号名称、类型、平台或分组已变化，等待重新导出；未请求模型。')
    return row


def metadata(row):
    return {key: row[key] for key in ('group_ids', 'last_used_at', 'extra', 'proxy_id', '_quota_policy') if key in row}
