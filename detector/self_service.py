"""Authenticated user-owned quality checks through the Sub2API gateway.

The browser only ever receives masked key metadata. The raw key is fetched with
the user's short-lived session and kept in worker memory for one job; it is
never written to the console database, a URL, or a log.
"""
import logging
import threading
import time
import uuid

import requests

from . import config
from .console_config import ConsoleError
from .console_store import ConsoleStore
from .prompts import DRAWING_PROMPT, INSTRUCTIONS
from .question_bank import ORIGINAL
from .quality_routing import retryable_request_error
from .session_auth import user_identity
from .upstream import redact
from .quality_catalog import select, evaluate,verdict


# Only pre-v0.2 jobs lack immutable question snapshots. New jobs always use
# quality_catalog, including the administrator's enabled/disabled choices.
LEGACY_QUESTIONS = {
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
                if 'json' not in response.headers.get('Content-Type', '').lower():
                    raise ConsoleError('code1024 接口未返回 JSON，请检查网关地址与接口路径', 502)
                body = response.json()
        except ConsoleError:
            raise
        except (requests.RequestException, ValueError, TypeError):
            raise ConsoleError('无法连接 code1024，请稍后重试', 502) from None
        if not isinstance(body, dict):
            raise ConsoleError('code1024 返回的数据格式无效', 502)
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
        entries = []
        for page in range(1, 101):
            data = self._request(token, 'GET', '/api/v1/keys', params={'page': page, 'page_size': 100})
            items = data.get('items') if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise ConsoleError('code1024 返回的 Key 清单格式无效', 502)
            entries.extend(items)
            pages = data.get('pages', page)
            if type(pages) is not int or not page <= pages <= 100:
                raise ConsoleError('code1024 返回的 Key 分页信息无效', 502)
            if page >= pages:
                break
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

    def _owned_key(self, token, key_id):
        # The gateway verifies ownership on this user endpoint. Fetch again at
        # execution time so a deleted or disabled key cannot use a stale list.
        raw = self._request(token, 'GET', '/api/v1/keys/' + str(int(key_id)))
        if not isinstance(raw, dict) or raw.get('id') != int(key_id) or raw.get('status') != 'active':
            raise ConsoleError('只能选择本人有效的 API Key', 403)
        if not isinstance(raw.get('key'), str) or not raw['key']:
            raise ConsoleError('无法读取所选 Key', 403)
        return raw

    def models(self, token, key_id):
        keys = self.keys(token)
        selected = next((k for k in keys if k['id'] == int(key_id)), None)
        if not selected or selected.get('status') != 'active':
            raise ConsoleError('只能选择本人有效的 API Key', 403)
        raw_key = self._owned_key(token, key_id)['key']
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.get(str(config.get('base_url') or '').rstrip('/') + '/v1/models',
                                       headers={'Authorization': 'Bearer ' + raw_key}, timeout=(5, 30), allow_redirects=False)
                if response.status_code != 200:
                    raise ConsoleError('模型清单加载失败：code1024 返回 HTTP %d；可手动填写模型名称' % response.status_code, 502)
                data = response.json()
        except (requests.RequestException, ValueError, TypeError):
            raise ConsoleError('模型清单加载失败；可手动填写模型名称', 502) from None
        entries = data.get('data', []) if isinstance(data, dict) else data
        models = []
        for item in entries if isinstance(entries, list) else []:
            if isinstance(item, dict) and isinstance(item.get('id'), str) and item['id']:
                models.append({'id': item['id'][:160], 'owned_by': str(item.get('owned_by') or '')[:80]})
        return {'key': selected, 'models': sorted(models, key=lambda x: x['id'])[:500]}

    def create(self, token, user_id, payload):
        if not isinstance(payload, dict) or set(payload)-{'share_content'} != {'api_key_id', 'model', 'effort', 'kinds', 'public'}:
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
        questions=select(self.store,kinds)
        if type(payload['public']) is not bool:
            raise ConsoleError('公开选项无效')
        if type(payload.get('share_content',False)) is not bool: raise ConsoleError('作品公开选项无效')
        job = self.store.create_self_job(user_id, selected['id'], selected['name'], model, effort, kinds, payload['public'],
            questions=questions,group_id=selected['group_id'],group_name=selected['group_name'],share_content=payload.get('share_content',False))
        self.tokens[job['id']] = token
        self.wake.set()
        return self.summary(job)

    def summary(self, job):
        rows = job['results']
        return {k: job[k] for k in ('id', 'status', 'model', 'effort', 'key_name', 'public', 'created_at', 'started_at', 'finished_at', 'message')} | {
            'api_key_id': job['api_key_id'], 'kinds': job['kinds'], 'results': rows,
            'group_name':job.get('group_name',''),
            'total':len(job['kinds']),
            'completed': len(rows), 'public_id': job['id'] if job['public'] else None}

    def jobs(self, user_id):
        return [self.summary(j) for j in self.store.self_jobs(user_id=user_id)]

    def job(self, ident, user_id):
        job = self.store.self_job(ident)
        if str(job['user_id']) != str(user_id):
            raise ConsoleError('只能查看自己的自助检测记录', 403)
        return self.summary(job)

    def publish(self, ident, user_id, value, share_content=False):
        job = self.store.self_job(ident)
        if str(job['user_id']) != str(user_id):
            raise ConsoleError('只能修改自己的检测结果', 403)
        if type(value) is not bool:
            raise ConsoleError('公开选项无效')
        if type(share_content) is not bool: raise ConsoleError('作品公开选项无效')
        self.store.update_self_job(ident, public=value,share_content=share_content)
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
                    text = str(message)
                    if response.status_code == 400:
                        lowered = text.lower()
                        code = 'MODEL_UNSUPPORTED' if 'model' in lowered and ('not supported' in lowered or 'unsupported' in lowered or 'support' in lowered) else 'INVALID_REQUEST'
                    elif response.status_code in (401, 403):
                        code = 'AUTHENTICATION_FAILED'
                    else:
                        code = 'HTTP_' + str(response.status_code)
                    raise ConsoleError('HTTP %d：%s' % (response.status_code, redact(text, [key], True)[:500]), 502, code)
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
            if str(identity['id']) != str(job['user_id']):
                raise ConsoleError('登录账号已变更，请重新创建检测', 403)
            selected=self._owned_key(session_token, job['api_key_id'])
            if job.get('group_id') is not None and selected.get('group_id')!=job['group_id']:
                raise ConsoleError('Key 所属分组已变更，请重新创建检测',409)
            key = selected['key']
            rows = []
            questions=job.get('questions') or [{**LEGACY_QUESTIONS[k],'id':k,'rounds':2 if k=='candy' else 1} for k in job['kinds']]
            for q in questions:
                if self.store.self_job(ident)['status']=='cancelled': return
                kind=q['id'];count=q['rounds'];started=time.monotonic()
                answers = []
                for round_no in range(count):
                    if self.store.self_job(ident)['status']=='cancelled': return
                    try:
                        text = self._call(key, job['model'], job['effort'], q, uuid.uuid4().hex)
                        status,answer=evaluate(q,text)
                        error='未识别明确答案' if status=='unknown' else None
                        answers.append({'round': round_no + 1, 'status': status, 'answer': answer,
                                        'response': redact(text,[key])[:100000], 'error': error})
                    except ConsoleError as exc:
                        answers.append({'round': round_no + 1, 'status': 'error', 'error_code': exc.code or 'REQUEST_FAILED', 'error': exc.message})
                        if not retryable_request_error(exc.code):
                            break
                statuses = [a['status'] for a in answers]
                final = verdict(statuses)
                rows.append({'kind': kind, 'title': q['title'], 'status': final, 'rounds': answers,
                             'prompt':q['prompt'],'expected':q.get('expected') if q['kind'] in ('number','choice','json') else None,
                             'question_type':q['kind'],'bank_version':q.get('version'),'duration':round(time.monotonic()-started,2),'finished_at':time.time(),
                             'error_code': next((a.get('error_code') for a in answers if a.get('error_code')), None)})
                if self.store.self_job(ident)['status']=='cancelled': return
                self.store.update_self_job(ident,results=rows)
            request_errors = sum(a['status'] == 'error' for row in rows for a in row['rounds'])
            final_status = 'completed_with_errors' if request_errors else 'completed'
            if self.store.self_job(ident)['status']=='cancelled': return
            self.store.update_self_job(ident, status=final_status, finished_at=time.time(), results=rows,
                                       message=('检测完成，但有 %d 项请求故障；请求故障不会判定为答错' % request_errors) if request_errors else '检测完成')
        except ConsoleError as exc:
            self.store.update_self_job(ident, status='failed', finished_at=time.time(), message=exc.message)
        finally:
            key = None
