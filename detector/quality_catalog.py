"""Shared, versioned questions and grading for account and user checks."""
from copy import deepcopy
import hashlib
import json
import re

from .console_config import ConsoleError, boolean, integer, text
from .question_bank import ORIGINAL, grade


def defaults():
    candy = {k: deepcopy(ORIGINAL[k]) for k in ('prompt', 'instructions', 'expected')}
    base = dict(enabled=True, description='', rounds=1, instructions='请严格遵循题目要求。', expected='', choices=[], unit='')
    return [
        {**base, **candy, 'id':'candy', 'title':'糖果双次校验', 'kind':'number', 'rounds':2, 'unit':'个'},
        {**base, 'id':'drawing', 'title':'鹈鹕骑自行车', 'kind':'drawing',
         'description':'生成作品供人工比较，结构完整不代表画面质量通过。',
         'prompt':'创建一个HTML，内容是SVG绘制一只鹈鹕骑自行车的2D动画',
         'instructions':'仅返回完整 HTML，使用内联 SVG 绘制动画，不引用外部资源。'},
        {**base, 'id':'choice', 'title':'逻辑选择题', 'kind':'choice', 'expected':'3', 'choices':['1','2','3'],
         'prompt':'有3个盒子，只有一个有球。甲说球在1号，乙说球不在1号，丙说球在2号。只有一人说真话，球在哪个盒子？最后一行严格写“最终答案：X”，X只写1、2或3。'},
        {**base, 'id':'numeric', 'title':'数值计算', 'kind':'number', 'expected':'323',
         'prompt':'计算17×19，最后一行严格写“最终答案：N”，N为整数。'},
        {**base, 'id':'json', 'title':'JSON 指令遵循', 'kind':'json', 'expected':{'answer':42,'unit':'items'},
         'prompt':'只返回一个JSON对象，不要解释、Markdown或额外字符：{"answer":42,"unit":"items"}'},
        {**base, 'id':'fingerprint', 'title':'指纹观察', 'kind':'reference',
         'description':'参考观察，不根据模型自报判定身份，不参与自动停调度。',
         'prompt':'请简要描述你的知识截止时间，并按顺序解释两种不同的推理检验方法。不要猜测未向你提供的运行环境信息。'},
    ]


def version(question):
    value={k:v for k,v in question.items() if k not in ('version','enabled')}
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:16]


def validate(value):
    if not isinstance(value,list) or not 1 <= len(value) <= 30:
        raise ConsoleError('题库应包含 1—30 道题')
    fields=set(defaults()[0])
    seen=set()
    out=[]
    for raw in value:
        if not isinstance(raw,dict) or set(raw)-{'version'} != fields:
            raise ConsoleError('题目字段不完整或包含未知字段')
        q=deepcopy(raw); q.pop('version',None)
        ident=q['id']
        if not isinstance(ident,str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}',ident) or ident in seen:
            raise ConsoleError('题目 ID 无效或重复')
        seen.add(ident)
        boolean(q['enabled'],'题目开关'); integer(q['rounds'],'题目请求次数',1,3)
        for key,limit in (('title',80),('prompt',12000),('instructions',2000)):
            q[key]=text(q[key],key,limit)
        if not isinstance(q['description'],str) or len(q['description'])>500 or not isinstance(q['unit'],str) or len(q['unit'])>16:
            raise ConsoleError('题目说明或单位无效')
        if q['kind'] not in ('number','choice','json','drawing','reference'):
            raise ConsoleError('判题类型无效')
        if not isinstance(q['choices'],list) or len(q['choices'])>20 or any(not isinstance(c,str) or len(c)!=1 for c in q['choices']):
            raise ConsoleError('选项须为单字符列表')
        if q['kind']=='choice' and (not q['choices'] or q['expected'] not in q['choices']):
            raise ConsoleError('选择题答案必须属于选项')
        if q['kind']=='json' and (not isinstance(q['expected'],dict) or len(json.dumps(q['expected']))>4000):
            raise ConsoleError('JSON 题目需要对象形式的期望答案')
        if q['kind']=='number':
            from fractions import Fraction
            try: Fraction(q['expected'])
            except (ValueError,TypeError,ZeroDivisionError): raise ConsoleError('数值题答案无效') from None
        q['version']=version(q); out.append(q)
    return out


def select(store, ids):
    catalog={q['id']:q for q in store.document('questions')['value'] if q['enabled']}
    if not isinstance(ids,list) or not ids or any(not isinstance(i,str) for i in ids) or len(ids)!=len(set(ids)) or any(i not in catalog for i in ids):
        raise ConsoleError('请选择已启用的统一题库题目')
    return [deepcopy(catalog[i]) for i in ids]


def evaluate(q, response):
    if q['kind']=='reference': return 'reference', None
    if q['kind']=='drawing':
        return ('pass' if re.search(r'<svg\b',response,re.I) and re.search(r'</svg\s*>',response,re.I) else 'fail'),None
    status,answer=grade(q,response)
    return ('unknown' if status=='unknown' else status),answer


def verdict(statuses):
    if not statuses: return 'error'
    for state in ('error','quota','fail','unknown','reference'):
        if state in statuses: return state
    return 'pass' if all(state=='pass' for state in statuses) else 'unknown'
