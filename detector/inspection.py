"""The group inspection contract: one group, a model and versioned questions."""
from copy import deepcopy

from .console_config import ConsoleError, boolean, integer, schedule, text
from .quality_catalog import select, validate


def describe(plan):
    value = plan['value']
    scope = value['scope']
    if scope['mode'] != 'groups' or len(scope['group_ids']) != 1:
        return None
    enabled = [(platform, model) for platform, model in value['models'].items() if model['enabled']]
    if len(enabled) != 1:
        return None
    platform, spec = enabled[0]
    return {'id': plan['id'], 'revision': plan['revision'], 'group_id': scope['group_ids'][0],
            'enabled': value['enabled'], 'model': spec['model'], 'effort': spec['effort'],
            'platform': platform, 'schedule': value['schedule'], 'question_ids': value['kinds'],
            'questions': value.get('questions', []), 'published': value['published']}


def save(service, payload, actor, ident=None, revision=None):
    required = {'group_id', 'enabled', 'model', 'effort', 'question_ids', 'questions', 'schedule'}
    if not isinstance(payload, dict) or set(payload) != required:
        raise ConsoleError('分组巡检配置字段不完整或包含未知字段')
    integer(payload['group_id'], '分组', 1, 2147483647)
    boolean(payload['enabled'], '启用开关')
    text(payload['model'], '模型', 160)
    if payload['effort'] not in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'):
        raise ConsoleError('思考强度无效')
    group = next((g for g in service.store.inventory()['value']['groups'] if g['id'] == payload['group_id']), None)
    if not group:
        raise ConsoleError('分组不存在，请先同步分组')
    platform = group['platform']
    from .config import DEFAULT_PROTOCOL, IMPLIED_PROTOCOLS
    if not platform:
        raise ConsoleError('分组平台信息不完整')
    for plan in service.store.plans():
        current = describe(plan)
        if current and current['group_id'] == group['id'] and plan['id'] != ident:
            raise ConsoleError('此分组已有巡检配置，请编辑已有配置', 409)
    questions = validate(payload['questions']) if payload['questions'] else select(service.store, payload['question_ids'])
    if [q['id'] for q in questions] != payload['question_ids']:
        raise ConsoleError('题目选择与题目配置不一致')
    spec = {'enabled': True, 'model': payload['model'], 'effort': payload['effort'],
            'protocol': IMPLIED_PROTOCOLS.get(platform, DEFAULT_PROTOCOL)}
    value = {'name': group['name'] + ' 定时巡检', 'public_name': group['name'], 'enabled': payload['enabled'],
             'scope': {'mode': 'groups', 'group_ids': [group['id']], 'account_ids': [], 'exclude_ids': []},
             'models': {platform: spec}, 'kinds': payload['question_ids'], 'questions': deepcopy(questions),
             'schedule': schedule(payload['schedule']), 'selection': {'mode': 'recent_active', 'count': 1},
             'published': True, 'show_accounts': False, 'show_answers': True, 'show_artifacts': True,
             'write_priority': False, 'write_callable': False}
    return describe(service.save_plan(value, actor, ident, revision))
