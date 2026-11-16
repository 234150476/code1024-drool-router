"""HTTP boundary for the public allowlist and verified administrator operations."""
import json
import logging
import re
from urllib.parse import parse_qs, urlsplit

from .console_config import ConsoleError
from .session_auth import admin_identity


def dispatch(handler, path):
    service = handler.server.console
    if service is None or not path.startswith(('/api/public/', '/api/admin/')):
        return False
    try:
        if path.startswith('/api/public/'):
            if handler.command not in ('GET', 'HEAD'):
                raise ConsoleError('不支持此方法', 405)
            return public(handler, service, path)
        supplied = handler.headers.get('Authorization', '')
        token = supplied[7:] if supplied.startswith('Bearer ') else ''
        identity = admin_identity(handler.server.session_auth_url, token,
                                  handler.headers.get('User-Agent', ''), handler.headers.get('X-Real-IP', ''))
        if not identity:
            raise ConsoleError('需要有效的管理员登录' if identity is not None else '仅管理员可以管理巡检',
                               401 if identity is not None else 403)
        actor = str(identity['id'])
        body = None
        if handler.command not in ('GET', 'HEAD'):
            if not handler.valid_origin(handler.headers.get('Origin', '')):
                raise ConsoleError('请求来源不被允许', 403)
            if handler.headers.get('Content-Type') != 'application/json' or handler.headers.get('Transfer-Encoding'):
                raise ConsoleError('需要 JSON 请求', 415)
            try:
                length = int(handler.headers.get('Content-Length', '0'))
            except ValueError:
                raise ConsoleError('请求大小无效', 413) from None
            if not 0 < length <= 131072:
                raise ConsoleError('请求大小超出限制', 413)
            handler.connection.settimeout(5)
            try:
                body = json.loads(handler.rfile.read(length))
            except (ValueError, UnicodeError):
                raise ConsoleError('JSON 无效') from None
            if not isinstance(body, dict):
                raise ConsoleError('需要 JSON 对象')
        admin(handler, service, path, body, token, actor)
    except ConsoleError as exc:
        handler.send_json({'error': exc.message}, exc.status)
    except Exception:
        logging.exception('Console HTTP failure: %s', path)
        handler.send_json({'error': '服务暂时无法处理请求，请检查私有服务日志'}, 500)
    return True


def public(handler, service, path):
    if path in ('/api/public/overview', '/api/public/groups'):
        raw = parse_qs(urlsplit(handler.path).query).get('hours', [''])[0]
        if raw and raw not in ('1', '6', '24', '72', '168', '720'):
            raise ConsoleError('无效时间范围')
        handler.send_json(service.public.projection(int(raw) if raw else None))
    elif match := re.fullmatch(r'/api/public/(groups|accounts)/([a-f0-9]{24})/results', path):
        data = service.public.projection()
        rows = data['groups'] if match[1] == 'groups' else [a for g in data['groups'] for a in g['accounts']]
        result = next((row for row in rows if row['id'] == match[2]), None)
        if result is None:
            raise ConsoleError('结果不存在或未公开', 404)
        handler.send_json(result)
    elif match := re.fullmatch(r'/api/public/runs/([a-f0-9]{24})(/artifact)?', path):
        value = service.public.run(match[1], artifact=bool(match[2]))
        if match[2]:
            handler.is_artifact = True
            handler.send_file(value, 'text/html; charset=utf-8')
        else:
            handler.send_json(value)
    else:
        raise ConsoleError('接口不存在', 404)
    return True


def fields(body, required):
    if set(body) != set(required):
        raise ConsoleError('请求字段不完整或包含未知字段')


def admin(h, s, path, body, token, actor):
    method = 'GET' if h.command == 'HEAD' else h.command
    store = s.store
    if method == 'GET':
        if path == '/api/admin/me':
            return h.send_json({'role': 'admin', 'id': actor})
        if path == '/api/admin/bootstrap':
            return h.send_json({'settings': store.document('settings'), 'inventory': store.inventory(),
                                'plans': store.plans(), 'jobs': [s.job_summary(j) for j in store.jobs()],
                                'publication': store.document('publication'), 'capabilities': s.capabilities()})
        if match := re.fullmatch(r'/api/admin/jobs/([a-f0-9]{32})', path):
            return h.send_json({'job': s.job_summary(store.job(match[1])), 'results': s.admin_results(match[1])})
    if path == '/api/admin/inventory/sync' and method == 'POST':
        fields(body, [])
        return h.send_json(s.sync_inventory(token, actor))
    if path in ('/api/admin/settings', '/api/admin/publication') and method == 'PUT':
        fields(body, ['value', 'revision'])
        if path.endswith('/settings'):
            return h.send_json(s.save_settings(body['value'], body['revision'], actor))
        return h.send_json(store.save_document('publication', body['value'], body['revision'], actor))
    if path == '/api/admin/plans' and method == 'POST':
        fields(body, ['value'])
        return h.send_json(s.save_plan(body['value'], actor), 201)
    if match := re.fullmatch(r'/api/admin/plans/([a-f0-9]{32})(/preview)?', path):
        if method == 'PUT' and not match[2]:
            fields(body, ['value', 'revision'])
            return h.send_json(s.save_plan(body['value'], actor, match[1], body['revision']))
        if method == 'POST' and match[2]:
            fields(body, [])
            return h.send_json(store.resolve(store.plan(match[1])))
    if path == '/api/admin/jobs' and method == 'POST':
        return h.send_json(s.enqueue(body, token, actor), 202)
    if match := re.fullmatch(r'/api/admin/jobs/([a-f0-9]{32})/stop', path):
        if method == 'POST':
            fields(body, [])
            return h.send_json(s.stop_job(match[1], actor))
    raise ConsoleError('接口不存在或不支持此方法', 404)
