"""Authenticated user-owned quality checks through the Sub2API gateway.

The browser only ever receives masked key metadata. The raw key is fetched with
the user's short-lived session and kept in worker memory for one job; it is
never written to the console database, a URL, or a log.
"""
from copy import deepcopy
import logging
import threading
import time
import uuid

import requests

from . import config
from .console_config import ConsoleError
from .console_store import ConsoleStore
from .prompts import CANDY_PROMPT, DRAWING_PROMPT, INSTRUCTIONS
from .question_bank import ORIGINAL, grade
from .session_auth import user_identity


CATALOG = [
    {'id': 'candy', 'title': '糖果双次校验', 'description': '原始逻辑题，连续两次独立回答都正确才算通过。', 'rounds': 2},
    {'id': 'choice', 'title': '逻辑选择题', 'description': '固定选项题，检查基本约束推理和格式遵循。', 'rounds': 1},
    {'id': 'numeric', 'title': '数值计算题', 'description': '固定算式题，检查计算和最终答案格式。', 'rounds': 1},
    {'id': 'json', 'title': 'JSON 指令遵循', 'description': '要求只返回固定 JSON，检查结构化输出遵循。', 'rounds': 1},
    {'id': 'drawing', 'title': 'SVG 完整性', 'description': '生成 HTML/SVG 作品并检查是否包含可渲染 SVG。', 'rounds': 1},
]

QUESTIONS = {
    'candy': ORIGINAL,
    'choice': {'id': 'logic-choice', 'title': '逻辑选择题', 'prompt': '有 3 个盒子，只有一个盒子里有球。甲说球在 1 号，乙说球不在 1 号，丙说球在 2 号。已知三人中只有一人说真话，球在哪个盒子？请最后一行严格使用“最终答案：X”，X 只写 1、2 或 3。', 'instructions': INSTRUCTIONS['candy'], 'kind': 'choice', 'choices': ['1', '2', '3'], 'expected': '3', 'version': 'self-choice-v1'},
    'numeric': {'id': 'numeric-17x19', 'title': '数值计算题', 'prompt': '计算 17 × 19。请最后一行严格使用“最终答案：N”，N 为整数。', 'instructions': INSTRUCTIONS['candy'], 'kind': 'number', 'expected': '323', 'unit': '', 'version': 'self-numeric-v1'},
    'json': {'id': 'json-contract', 'title': 'JSON 指令遵循', 'prompt': '只返回一个 JSON 对象，不要 Markdown、解释或额外字符：{"answer":42,"unit":"items"}', 'instructions': '严格只返回题目指定的 JSON 对象。', 'kind': 'json', 'expected': {'answer': 42, 'unit': 'items'}, 'version': 'self-json-v1'},
    'drawing': {'id': 'svg-flamingos', 'title': 'SVG 完整性', 'prompt': DRAWING_PROMPT, 'instructions': INSTRUCTIONS['drawing'], 'kind': 'drawing', 'version': 'self-svg-v1'},
}


def _session_url():
    return config.get('web.session_auth_url') or config.get('base_url')


def _masked(value):
    value = str(value or '')
    return value[:4] + '••••' + value[-4:] if len(value) > 10 else '••••'


class SelfService:
    def __init__(self, store: ConsoleStore, start=True):
        self.store = store
        self.tokens = {}
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.RLock()
        self.thread = None
        if start:
            self.thread = threading.Thread(target=self.loop, daemon=True, name='self-quality-worker')
            self.thread.start()

    def _request(self, token, method, path, *, key=None, json=None, params=None):
        base = str(config.get('base_url') or '').rstrip('/')
        if not base:
            raise ConsoleError('检测服务尚未配置 code1024 地址', 503)
        headers = {'Accept': 'application/json'}
        headers['Authorization'] = 'Bearer ' + (key or token)
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.request(method, base + path, headers=headers, json=json, params=params,
                                           timeout=(5, 45), allow_redirects=False)
                if response.status_code != 200:
                    raise ConsoleError('code1024 返回 HTTP %d' % response.status_code, 502)
                body = response.json()
        except ConsoleError:
            raise
        except (requests.RequestException, ValueError, TypeError):
            raise ConsoleError('无法连接 code1024，请稍后重试', 502) from None
        if body.get('code', 0) != 0:
            raise ConsoleError('code1024 拒绝了本次请求', 502)
        return body.get('data', body)

    def identity(self, token, user_agent='', client_ip=''):
        if not token:
            raise ConsoleError('需要登录后才能使用自助检测', 401)
        identity = user_identity(_session_url(), token, user_agent, client_ip)
        if identity is False:
            raise ConsoleError('无法验证登录状态', 502)
        if not identity:
            raise ConsoleError('需要登录后才能使用自助检测', 401)
        return identity

    def keys(self, token):
        self.identity(token)
        data = self._request(token, 'GET', '/api/v1/api-keys', params={'page': 1, 'page_size': 100})
        entries = data.get('items', []) if isinstance(data, dict) else data
        if not isinstance(entries, list):
            raise ConsoleError('code1024 返回的 Key 清单格式无效', 502)
        result = []
        for item in entries:
            if not isinstance(item, dict) or not isinstance(item.get('id'), int):
                continue
            group = item.get('group') if isinstance(item.get('group'), dict) else {}
            result.append({'id': item['id'], 'name': str(item.get('name') or '未命名 Key')[:120],
                           'masked': _masked(item.get('key')), 'status': item.get('status'),
                           'group_id': item.get('group_id'), 'group_name': group.get('name'),
                           'quota': item.get('quota'), 'quota_used': item.get('quota_used'),
                           'expires_at': item.get('expires_at')})
        return result

    def models(self, token, key_id):
        keys = self.keys(token)
        selected = next((k for k in keys if k['id'] == int(key_id)), None)
        if not selected or selected.get('status') != 'active':
            raise ConsoleError('只能选择本人有效的 API Key', 403)
        raw = self._request(token, 'GET', '/api/v1/api-keys/' + str(int(key_id)))
        raw_key = raw.get('key') if isinstance(raw, dict) else ''
        if not raw_key:
            raise ConsoleError('无法读取所选 Key', 403)
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.get(str(config.get('base_url') or '').rstrip('/') + '/v1/models',
                                       headers={'Authorization': 'Bearer ' + raw_key}, timeout=(5, 30), allow_redirects=False)
                if response.status_code != 200:
                    return {'key': selected, 'models': []}
                data = response.json()
        except (requests.RequestException, ValueError, TypeError):
            data = {}
        entries = data.get('data', []) if isinstance(data, dict) else data
        models = []
        for item in entries if isinstance(entries, list) else []:
            if isinstance(item, dict) and isinstance(item.get('id'), str) and item['id']:
                models.append({'id': item['id'][:160], 'owned_by': str(item.get('owned_by') or '')[:80]})
        return {'key': selected, 'models': sorted(models, key=lambda x: x['id'])[:500]}

    def create(self, token, user_id, payload):
        if not isinstance(payload, dict) or set(payload) != {'api_key_id', 'model', 'effort', 'kinds', 'public'}:
            raise ConsoleError('自助检测参数不完整')
        keys = self.keys(token)
        selected = next((k for k in keys if k['id'] == payload['api_key_id']), None)
        if not selected or selected.get('status') != 'active':
            raise ConsoleError('只能选择本人有效的 API Key', 403)
        model = payload['model'].strip() if isinstance(payload['model'], str) else ''
        if not model or len(model) > 160:
            raise ConsoleError('模型名称无效')
        effort = payload['effort'] if payload['effort'] in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max') else 'medium'
        kinds = payload['kinds']
        if not isinstance(kinds, list) or not kinds or len(kinds) > len(CATALOG) or any(k not in QUESTIONS for k in kinds):
            raise ConsoleError('请选择有效检测题目')
        if type(payload['public']) is not bool:
            raise ConsoleError('公开选项无效')
        job = self.store.create_self_job(user_id, selected['id'], selected['name'], model, effort, kinds, payload['public'])
        self.tokens[job['id']] = token
        self.wake.set()
        return self.summary(job)

    def summary(self, job):
        rows = job['results']
        return {k: job[k] for k in ('id', 'status', 'model', 'effort', 'key_name', 'public', 'created_at', 'started_at', 'finished_at', 'message')} | {
            'api_key_id': job['api_key_id'], 'kinds': job['kinds'], 'results': rows,
            'total': sum(2 if k == 'candy' else 1 for k in job['kinds']),
            'completed': len(rows), 'public_id': job['id'] if job['public'] else None}

    def jobs(self, user_id):
        return [self.summary(j) for j in self.store.self_jobs(user_id=user_id)]

    def job(self, ident, user_id):
        job = self.store.self_job(ident)
        if str(job['user_id']) != str(user_id):
            raise ConsoleError('只能查看自己的自助检测记录', 403)
        return self.summary(job)

    def publish(self, ident, user_id, value):
        job = self.store.self_job(ident)
        if str(job['user_id']) != str(user_id):
            raise ConsoleError('只能修改自己的检测结果', 403)
        if type(value) is not bool:
            raise ConsoleError('公开选项无效')
        self.store.update_self_job(ident, public=value)
        return self.summary(self.store.self_job(ident))

    def stop_job(self, ident, user_id):
        job = self.store.self_job(ident)
        if str(job['user_id']) != str(user_id):
            raise ConsoleError('只能停止自己的检测任务', 403)
        if job['status'] in ('queued', 'running'):
            self.store.update_self_job(ident, status='cancelled', finished_at=time.time(), message='用户停止了检测')
            self.tokens.pop(ident, None)
        return self.summary(self.store.self_job(ident))

    def loop(self):
        while not self.stop.is_set():
            queued = [j for j in self.store.self_jobs() if j['status'] == 'queued']
            if queued:
                try:
                    self.execute(queued[0])
                except Exception:
                    logging.exception('Self quality job failed: %s', queued[0]['id'])
                    self.store.update_self_job(queued[0]['id'], status='failed', finished_at=time.time(), message='执行失败，请稍后重试')
                continue
            self.wake.wait(1.5)
            self.wake.clear()

    @staticmethod
    def _content(body):
        choices = body.get('choices') if isinstance(body, dict) else None
        message = choices[0].get('message', {}) if isinstance(choices, list) and choices else {}
        content = message.get('content', '') if isinstance(message, dict) else ''
        if isinstance(content, list):
            content = ''.join(str(part.get('text', '')) for part in content if isinstance(part, dict))
        return str(content)

    def _call(self, key, model, effort, question, request_id):
        base = str(config.get('base_url') or '').rstrip('/')
        payload = {'model': model, 'stream': False, 'messages': [
            {'role': 'system', 'content': question['instructions']},
            {'role': 'user', 'content': question['prompt']},
        ]}
        if effort != 'none':
            payload['reasoning_effort'] = effort
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.post(base + '/v1/chat/completions', headers={'Authorization': 'Bearer ' + key,
                    'Content-Type': 'application/json', 'X-Client-Request-Id': request_id}, json=payload,
                    timeout=(12, 300), allow_redirects=False)
                raw = response.text[:4000]
                if response.status_code != 200:
                    message = raw
                    try:
                        value = response.json(); err = value.get('error', value)
                        message = err.get('message', message) if isinstance(err, dict) else str(err)
                    except (ValueError, TypeError):
                        pass
                    raise ConsoleError('HTTP %d：%s' % (response.status_code, message[:500]), 502)
                body = response.json()
                text = self._content(body)
                if not text:
                    raise ConsoleError('模型返回空回答', 502)
                return text
        except ConsoleError:
            raise
        except (requests.RequestException, ValueError, TypeError):
            raise ConsoleError('模型请求失败，请检查 Key、模型权限和额度', 502) from None

    def execute(self, job):
        ident = job['id']
        with self.lock:
            current = self.store.self_job(ident)
            if current['status'] != 'queued':
                return
            self.store.update_self_job(ident, status='running', started_at=time.time())
            session_token = self.tokens.pop(ident, None)
        if not session_token:
            self.store.update_self_job(ident, status='failed', finished_at=time.time(), message='登录会话已过期，请重新登录后重试')
            return
        try:
            identity = self.identity(session_token)
            key_list = self._request(session_token, 'GET', '/api/v1/api-keys', params={'page': 1, 'page_size': 100})
            entries = key_list.get('items', []) if isinstance(key_list, dict) else []
            selected = next((x for x in entries if isinstance(x, dict) and x.get('id') == job['api_key_id']), None)
            key = selected.get('key') if selected else ''
            if not key:
                raise ConsoleError('无法读取所选 Key，请刷新后重试', 403)
            rows = []
            for kind in job['kinds']:
                q = deepcopy(QUESTIONS[kind])
                count = 2 if kind == 'candy' else 1
                answers = []
                for round_no in range(count):
                    try:
                        text = self._call(key, job['model'], job['effort'], q, uuid.uuid4().hex)
                        if kind == 'drawing':
                            status = 'pass' if '<svg' in text.lower() and '</svg' in text.lower() else 'fail'
                            answer, error = None, None
                        else:
                            status, answer = grade(q, text)
                            error = None if status in ('pass', 'fail') else '未识别明确答案'
                        answers.append({'round': round_no + 1, 'status': status, 'answer': answer,
                                        'response': text[:12000], 'error': error})
                    except ConsoleError as exc:
                        answers.append({'round': round_no + 1, 'status': 'error', 'error_code': 'REQUEST_FAILED', 'error': exc.message})
                statuses = [a['status'] for a in answers]
                final = 'pass' if all(s == 'pass' for s in statuses) else 'fail' if any(s == 'fail' for s in statuses) else 'error'
                rows.append({'kind': kind, 'title': q['title'], 'status': final, 'rounds': answers,
                             'error_code': next((a.get('error_code') for a in answers if a.get('error_code')), None)})
            self.store.update_self_job(ident, status='completed', finished_at=time.time(), results=rows,
                                       message='检测完成；请求故障不会判定为答错')
        except ConsoleError as exc:
            self.store.update_self_job(ident, status='failed', finished_at=time.time(), message=exc.message)
        finally:
            key = None
