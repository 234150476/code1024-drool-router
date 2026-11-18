"""Validate editable business settings; connection details stay deployment-owned."""
from copy import deepcopy
import math
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config


class ConsoleError(Exception):
    def __init__(self, message, status=400, code=None):
        self.message, self.status, self.code = message, status, code
        super().__init__(message)


def text(value, name, limit=120):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ConsoleError(name + '不能为空或超过长度限制')
    return value.strip()


def integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ConsoleError('%s应为 %d—%d 的整数' % (name, low, high))
    return value


def boolean(value, name):
    if type(value) is not bool:
        raise ConsoleError(name + '应为布尔值')
    return value


def ids(value, name, limit=200):
    if not isinstance(value, list) or len(value) > limit or any(type(v) is not int or v <= 0 for v in value):
        raise ConsoleError(name + '必须为有效 ID 列表')
    return sorted(set(value))


def schedule(value):
    if not isinstance(value, dict) or set(value)-{'quiet_pause'} != {'enabled', 'timezone', 'regular_minutes', 'quiet_start', 'quiet_end', 'quiet_minutes'}:
        raise ConsoleError('定时配置字段不完整')
    result = deepcopy(value)
    boolean(result['enabled'], '定时开关')
    if 'quiet_pause' in result:
        boolean(result['quiet_pause'], '安静时段暂停')
    try:
        ZoneInfo(result['timezone'])
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise ConsoleError('无效时区') from None
    for key in ('regular_minutes', 'quiet_minutes'):
        integer(result[key], '检测间隔', 5, 1440)
    for key in ('quiet_start', 'quiet_end'):
        if not isinstance(result[key], str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', result[key]):
            raise ConsoleError('时段必须使用 HH:MM')
    return result


def models(value):
    if not isinstance(value, dict) or not value or len(value) > 12:
        raise ConsoleError('至少配置一个平台模型')
    out = {}
    for platform, spec in value.items():
        if not isinstance(platform, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', platform) or not isinstance(spec, dict):
            raise ConsoleError('无效平台配置')
        if set(spec) != {'enabled', 'model', 'protocol', 'effort'}:
            raise ConsoleError('模型配置字段不完整')
        boolean(spec['enabled'], '平台开关')
        text(spec['model'], '模型', 160)
        if spec['protocol'] not in config.PROTOCOLS or spec['effort'] not in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'):
            raise ConsoleError('无效协议或推理等级')
        out[platform] = deepcopy(spec)
    return out


def defaults():
    return {
        'title': config.get('title'),
        'platforms': {name: {'enabled': bool(spec.get('enabled')), 'model': spec.get('model', ''),
                             'protocol': spec.get('protocol') or config.IMPLIED_PROTOCOLS.get(name, 'openai_chat'),
                             'effort': spec.get('effort') or 'medium'}
                      for name, spec in config.CONFIG['platforms'].items()},
        'budgets': deepcopy(config.CONFIG['budgets']),
        'schedule': {key: config.CONFIG['schedule'][key] for key in ('enabled', 'timezone', 'regular_minutes', 'quiet_start', 'quiet_end', 'quiet_minutes', 'quiet_pause')},
        'concurrency': 2, 'max_accounts': 50,
        'weights': deepcopy(config.get('routing.weights')),
        'rounds': int(config.get('routing.rounds', 3)),
        'stability_rounds': int(config.get('routing.stability_rounds', 3)),
        'retention': deepcopy(config.CONFIG['retention']),
    }


def validate_settings(value):
    if not isinstance(value, dict) or set(value) != set(defaults()):
        raise ConsoleError('配置包含未知字段或缺少字段')
    out = deepcopy(value)
    out['title'] = text(out['title'], '页面标题', 80)
    out['platforms'] = models(out['platforms'])
    out['schedule'] = schedule(out['schedule'])
    integer(out['concurrency'], '并发数', 1, 6)
    integer(out['max_accounts'], '每轮账号上限', 1, 200)
    b = out['budgets']
    if not isinstance(b, dict) or set(b) != {'default_seconds', 'idle_seconds', 'gemini_high_idle_seconds', 'drawing_platform_seconds', 'max_attempts'}:
        raise ConsoleError('超时配置字段不完整')
    integer(b['default_seconds'], '总超时', 30, 1800)
    integer(b['idle_seconds'], '空闲超时', 10, 600)
    integer(b['gemini_high_idle_seconds'], 'Gemini 空闲超时', 10, 900)
    integer(b['max_attempts'], '尝试次数', 1, 3)
    if not isinstance(b['drawing_platform_seconds'], dict) or set(b['drawing_platform_seconds']) - set(out['platforms']):
        raise ConsoleError('绘图时限平台无效')
    for value in b['drawing_platform_seconds'].values():
        integer(value, '绘图超时', 30, 1800)
    w = out['weights']
    if not isinstance(w, dict) or set(w) != {'intelligence', 'cost', 'stability', 'speed'}:
        raise ConsoleError('评分权重字段不完整')
    if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in w.values()) or abs(sum(w.values()) - 1) > .00001:
        raise ConsoleError('评分权重必须为 0—1，合计为 1')
    for key in ('rounds', 'stability_rounds'):
        integer(out[key], '参考轮数', 1, 30)
    if not isinstance(out['retention'], dict) or set(out['retention']) != {'artifacts_hours', 'cost_days'}:
        raise ConsoleError('保留期限字段不完整')
    integer(out['retention']['artifacts_hours'], '结果保留小时', 24, 720)
    integer(out['retention']['cost_days'], '成本保留天数', 1, 365)
    return out


def validate_plan(value):
    required = {'name', 'enabled', 'scope', 'models', 'kinds', 'schedule', 'published', 'public_name',
                'show_accounts', 'show_answers', 'show_artifacts', 'write_priority', 'write_callable'}
    if not isinstance(value, dict) or set(value)-{'isolation', 'selection', 'questions', 'quarantine'} != required:
        raise ConsoleError('检测计划包含未知字段或缺少字段')
    out = deepcopy(value)
    out['name'] = text(out['name'], '计划名称', 80)
    out['public_name'] = text(out['public_name'], '公开名称', 80)
    for key in ('enabled', 'published', 'show_accounts', 'show_answers', 'show_artifacts', 'write_priority', 'write_callable'):
        boolean(out[key], key)
    s = out['scope']
    if not isinstance(s, dict) or set(s) != {'mode', 'group_ids', 'account_ids', 'exclude_ids'} or s['mode'] not in ('groups', 'accounts'):
        raise ConsoleError('无效检测范围')
    for key in ('group_ids', 'account_ids', 'exclude_ids'):
        s[key] = ids(s[key], key)
    selected = 'group_ids' if s['mode'] == 'groups' else 'account_ids'
    other = 'account_ids' if selected == 'group_ids' else 'group_ids'
    if not s[selected] or s[other]:
        raise ConsoleError('请选择检测对象，按分组与按账号不能混用')
    if not isinstance(out['kinds'], list) or not out['kinds'] or any(not isinstance(k,str) for k in out['kinds']) or len(out['kinds']) != len(set(out['kinds'])):
        raise ConsoleError('请选择有效检测项目')
    out['models'] = models(out['models'])
    out['schedule'] = schedule(out['schedule'])
    if 'questions' in out:
        from .quality_catalog import validate
        out['questions'] = validate(out['questions'])
        if [q['id'] for q in out['questions']] != out['kinds']:
            raise ConsoleError('计划题目与选择项目不一致')
    if 'selection' in out:
        from .account_selection import MAX_ACCOUNT_ATTEMPTS, inspection_attempt_limit
        selection = out['selection']
        if not isinstance(selection, dict) or set(selection)-{'max_account_attempts'} != {'mode', 'count'} or selection['mode'] not in ('all', 'recent_active'):
            raise ConsoleError('账号抽样配置无效')
        integer(selection['count'], '每轮抽样账号数', 1, 50)
        if 'max_account_attempts' in selection:
            integer(selection['max_account_attempts'], '每轮最多尝试账号数', 1, MAX_ACCOUNT_ATTEMPTS)
            if inspection_attempt_limit(out) is None:
                raise ConsoleError('账号顺延仅支持单分组巡检')
    if 'quarantine' in out:
        policy = out['quarantine']
        if not isinstance(policy, dict) or set(policy)-{'hide_failed_results'} != {'enabled', 'cooldown_minutes'}:
            raise ConsoleError('自动剔除配置字段不完整')
        policy.setdefault('hide_failed_results', True)
        boolean(policy['hide_failed_results'], '隐藏失败结果开关')
        boolean(policy['enabled'], '自动剔除开关')
        integer(policy['cooldown_minutes'], '冷却分钟数', 5, 10080)
        if policy['enabled']:
            if out['scope']['mode'] != 'groups' or len(out['scope']['group_ids']) != 1:
                raise ConsoleError('自动剔除仅支持单分组巡检')
            if not any(q['enabled'] and q['kind'] in ('number', 'choice', 'json') for q in out.get('questions', [])):
                raise ConsoleError('自动剔除需要配置有标准答案的数值、选择或 JSON 题；鹈鹕和指纹题不适用')
    # Cross-plan evidence is not yet a routing policy. Never silently ignore an enabled write.
    if out['write_priority'] or out['write_callable']:
        raise ConsoleError('旧版回写开关已停用，请使用质量隔离策略')
    if 'isolation' in out:
        p=out['isolation']
        if not isinstance(p,dict) or set(p)!={'enabled','failure_rounds','cooldown_minutes','min_available'}:
            raise ConsoleError('隔离策略字段不完整')
        boolean(p['enabled'],'自动隔离')
        if p['enabled']:
            raise ConsoleError('此服务仅检测和展示结果，已移除自动隔离与调度回写')
        integer(p['failure_rounds'],'连续失败轮数',1,10)
        integer(p['cooldown_minutes'],'冷却分钟数',5,10080)
        integer(p['min_available'],'分组最低可用账号数',1,100)
    return out
