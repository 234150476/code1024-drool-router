"""HTTP boundary for the public allowlist and verified administrator operations."""
import json
import hashlib
import hmac
import logging
import re
import time
from urllib.parse import parse_qs, urlsplit

from .console_config import ConsoleError
from .session_auth import admin_identity, user_identity


def dispatch(handler, path):
    service = handler.server.console
    if service is None or not path.startswith(('/api/public/', '/api/admin/', '/api/user/')):
        return False
    try:
        supplied = handler.headers.get('Authorization', '')
        token = supplied[7:] if supplied.startswith('Bearer ') else ''
        if path.startswith('/api/public/'):
            if handler.command not in ('GET', 'HEAD'):
                raise ConsoleError('不支持此方法', 405)
            private_artwork = path.startswith('/api/public/private-artwork/')
            artwork = private_artwork or bool(re.fullmatch(r'/api/public/(?:records|runs)/[a-f0-9]{24}/artifact', path))
            if artwork and not token:
                grant = parse_qs(urlsplit(handler.path).query).get('preview', [''])[0]
                token = handler.server.preview_sessions.token(grant, path)
            anonymous = service.store.document('access')['value'].get('allow_anonymous_results') is True
            identity = None
            if private_artwork or not anonymous:
                identity = user_identity(handler.server.session_auth_url, token,
                                         handler.headers.get('User-Agent', ''), handler.headers.get('X-Real-IP', ''))
                if not identity:
                    raise ConsoleError('请从 code1024 登录后进入结果页面，当前未开放匿名访问'
                                       if not private_artwork else '请登录后查看自己的作品', 401)
            return public(handler, service, path, token if identity else '', identity)
        if path.startswith('/api/user/'):
            identity = service.self_service.identity(token, handler.headers.get('User-Agent', ''), handler.headers.get('X-Real-IP', ''))
            actor = str(identity['id'])
            body = read_body(handler, 131072) if handler.command not in ('GET', 'HEAD') else None
            user(handler, service, path, body, token, actor)
            return True
        if not token and re.fullmatch(r'/api/admin/records/[a-f0-9]{24}/artifact', path):
            grant = parse_qs(urlsplit(handler.path).query).get('preview', [''])[0]
            token = handler.server.preview_sessions.token(grant, path)
        identity = admin_identity(handler.server.session_auth_url, token,
                                  handler.headers.get('User-Agent', ''), handler.headers.get('X-Real-IP', ''))
        if not identity:
            raise ConsoleError('需要有效的管理员登录' if identity is not None else '仅管理员可以管理巡检',
                               401 if identity is not None else 403)
        actor = str(identity['id'])
        body = read_body(handler, 131072) if handler.command not in ('GET', 'HEAD') else None
        admin(handler, service, path, body, token, actor)
    except ConsoleError as exc:
        handler.send_json({'error': exc.message}, exc.status)
    except Exception:
        logging.exception('Console HTTP failure: %s', path)
        handler.send_json({'error': '服务暂时无法处理请求，请检查私有服务日志'}, 500)
    return True


def read_body(handler, limit):
    if not handler.valid_origin(handler.headers.get('Origin', '')):
        raise ConsoleError('请求来源不被允许', 403)
    if handler.headers.get('Content-Type') != 'application/json' or handler.headers.get('Transfer-Encoding'):
        raise ConsoleError('需要 JSON 请求', 415)
    try:
        length = int(handler.headers.get('Content-Length', '0'))
    except ValueError:
        raise ConsoleError('请求大小无效', 413) from None
    if not 0 < length <= limit:
        raise ConsoleError('请求大小超出限制', 413)
    handler.connection.settimeout(5)
    try:
        body = json.loads(handler.rfile.read(length))
    except (ValueError, UnicodeError):
        raise ConsoleError('JSON 无效') from None
    if not isinstance(body, dict):
        raise ConsoleError('需要 JSON 对象')
    return body


def preview_url(handler, target, token):
    if not token:
        return target
    path = urlsplit(target).path
    grant = handler.server.preview_sessions.issue(token, path)
    return target + ('&' if '?' in target else '?') + 'preview=' + grant


def public(handler, service, path, token='', identity=None):
    if match := re.fullmatch(r'/api/public/records/([a-f0-9]{24})/artifact', path):
        value = service.public.record(match[1])
        if value.get('question_type') != 'drawing' and value.get('kind') != 'drawing':
            raise ConsoleError('这条结果不是绘图作品', 404)
        index = round_index(handler)
        rounds = value['rounds']
        if index >= len(rounds) or not rounds[index].get('response'):
            raise ConsoleError('作品不存在或未公开', 404)
        return render(handler, rounds[index]['response'])
    elif match := re.fullmatch(r'/api/public/private-artwork/([a-f0-9]{32})/([a-z][a-z0-9_-]{0,39})/(\d+)', path):
        query = parse_qs(urlsplit(handler.path).query)
        expires = query.get('expires', [''])[0]
        signature = query.get('signature', [''])[0]
        expected = hmac.new(handler.server.artifact_key, (path+':'+expires).encode(), hashlib.sha256).hexdigest()
        if not expires.isdigit() or not time.time() < int(expires) <= time.time()+601 or not hmac.compare_digest(signature, expected):
            raise ConsoleError('作品链接已过期，请刷新记录', 403)
        job = service.self_service.job(match[1], str(identity['id']))
        row = next((row for row in job['results'] if row['kind']==match[2] and row.get('question_type')=='drawing'), None)
        index = int(match[3])
        if row is None or index >= len(row['rounds']):
            raise ConsoleError('作品不存在', 404)
        return render(handler, row['rounds'][index].get('response',''))
    elif path == '/api/public/self-results':
        return handler.send_json({'jobs': [public_self_job(job) for job in service.store.self_jobs(public_only=True)]})
    elif match := re.fullmatch(r'/api/public/self-results/([a-f0-9]{32})', path):
        job = service.store.self_job(match[1])
        if not job['public']:
            raise ConsoleError('结果不存在或未公开', 404)
        return handler.send_json(public_self_job(job))
    elif path in ('/api/public/overview', '/api/public/groups'):
        raw = parse_qs(urlsplit(handler.path).query).get('hours', [''])[0]
        if raw and raw not in ('1', '6', '24', '72', '168', '720'):
            raise ConsoleError('无效时间范围')
        handler.send_json(service.public.projection(int(raw) if raw else None))
    elif match := re.fullmatch(r'/api/public/groups/([a-f0-9]{24})/results', path):
        data = service.public.projection()
        result = next((row for row in data['groups'] if row['id'] == match[1]), None)
        if result is None:
            raise ConsoleError('结果不存在或未公开', 404)
        handler.send_json(result)
    elif match := re.fullmatch(r'/api/public/groups/([a-f0-9]{24})/history',path):
        handler.send_json({'runs':service.public.history(match[1])})
    elif match := re.fullmatch(r'/api/public/records/([a-f0-9]{24})',path):
        value = service.public.record(match[1])
        if value.get('question_type') == 'drawing' or value.get('kind') == 'drawing':
            value['render_urls'] = [preview_url(handler, path+'/artifact?round='+str(index), token)
                                    for index, _ in enumerate(value.get('rounds', []))]
        handler.send_json(value)
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


def public_self_job(job):
    rows = [{'kind': r.get('kind'), 'title': r.get('title'), 'status': r.get('status'),
             'rounds': [{'round': x.get('round'), 'status': x.get('status')} for x in r.get('rounds', [])]}
            for r in job.get('results', [])]
    return {'id': job['id'], 'model': job['model'], 'effort': job['effort'], 'status': job['status'],
            'created_at': job['created_at'], 'finished_at': job['finished_at'], 'results': rows}


def user(handler, service, path, body, token, actor):
    method = 'GET' if handler.command == 'HEAD' else handler.command
    if method == 'GET':
        if path == '/api/user/me':
            return handler.send_json({'role': 'user', 'id': actor})
        if path == '/api/user/questions':
            return handler.send_json({'questions': [{k:q[k] for k in ('id','title','description','rounds','version')} for q in service.store.document('questions')['value'] if q['enabled']]})
        if path == '/api/user/keys':
            return handler.send_json({'keys': service.self_service.keys(token)})
        if path == '/api/user/jobs':
            return handler.send_json({'jobs': service.self_service.jobs(actor)})
        if match := re.fullmatch(r'/api/user/keys/(\d+)/models', path):
            return handler.send_json(service.self_service.models(token, int(match[1])))
        if match := re.fullmatch(r'/api/user/jobs/([a-f0-9]{32})', path):
            return handler.send_json(service.self_service.job(match[1], actor))
        if match := re.fullmatch(r'/api/user/jobs/([a-f0-9]{32})/render-links', path):
            job = service.self_service.job(match[1], actor)
            expires = str(int(time.time())+600)
            links = {}
            for row in job['results']:
                if row.get('question_type') != 'drawing':
                    continue
                urls = []
                for index, _ in enumerate(row['rounds']):
                    target = '/api/public/private-artwork/'+job['id']+'/'+row['kind']+'/'+str(index)
                    signature = hmac.new(handler.server.artifact_key, (target+':'+expires).encode(), hashlib.sha256).hexdigest()
                    urls.append(preview_url(handler, target+'?expires='+expires+'&signature='+signature, token))
                links[row['kind']] = urls
            return handler.send_json({'links': links})
    if path == '/api/user/jobs' and method == 'POST':
        return handler.send_json(service.self_service.create(token, actor, body), 202)
    match = re.fullmatch(r'/api/user/jobs/([a-f0-9]{32})/stop', path)
    if match and method == 'POST':
        fields(body, [])
        return handler.send_json(service.self_service.stop_job(match[1], actor))
    match = re.fullmatch(r'/api/user/jobs/([a-f0-9]{32})/publication', path)
    if match and method == 'PUT':
        if set(body)-{'share_content'} != {'public'}:
            raise ConsoleError('请求字段不完整或包含未知字段')
        return handler.send_json(service.self_service.publish(match[1], actor, body['public'],body.get('share_content',False)))
    raise ConsoleError('接口不存在或不支持此方法', 404)


def round_index(handler):
    raw = parse_qs(urlsplit(handler.path).query).get('round', ['0'])[0]
    if not raw.isdigit() or len(raw)>2:
        raise ConsoleError('无效回答次数')
    return int(raw)


def render(handler, response):
    content = re.sub(r'^```(?:html|svg|xml)?\s*\n?|\n?```\s*$', '', str(response).strip(), flags=re.I)
    # The HTTP artifact CSP and opaque iframe sandbox own the boundary. Inline
    # scripts can animate SVG but receive no origin, network, forms or navigation.
    data = ('<!doctype html><meta charset="utf-8"><style>body{margin:0;background:#fff}svg{max-width:100%;height:auto}</style>'+content).encode()
    handler.is_artifact = True
    handler.send_response(200)
    handler.send_header('Content-Type', 'text/html; charset=utf-8')
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('Content-Length', str(len(data)))
    handler.end_headers()
    if handler.command != 'HEAD':
        handler.wfile.write(data)
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
        if path == '/api/admin/access':
            return h.send_json(store.document('access'))
        if path == '/api/admin/quarantines':
            return h.send_json(s.quarantine.admin_status())
        if match := re.fullmatch(r'/api/admin/records/([a-f0-9]{24})(/artifact)?', path):
            value = s.public.record(match[1], include_hidden=True)
            if match[2]:
                if value.get('question_type') != 'drawing':
                    raise ConsoleError('这条结果不是绘图作品', 404)
                index = round_index(h)
                if index >= len(value['rounds']) or not value['rounds'][index].get('response'):
                    raise ConsoleError('作品不存在', 404)
                return render(h, value['rounds'][index]['response'])
            if value.get('question_type') == 'drawing':
                value['render_urls'] = [preview_url(h, path+'/artifact?round='+str(index), token)
                                        for index, _ in enumerate(value['rounds'])]
            return h.send_json(value)
        if path == '/api/admin/bootstrap':
            return h.send_json({'settings': store.document('settings'), 'inventory': store.inventory(),
                                'plans': store.plans(), 'jobs': [s.job_summary(j) for j in store.jobs()],
                                'questions':store.document('questions'),
                                'publication': store.document('publication'), 'capabilities': s.capabilities()})
        if path == '/api/admin/inspections':
            from .inspection import describe
            return h.send_json({'inspections': [item for plan in store.plans() if (item := describe(plan))],
                                'groups': store.inventory()['value']['groups'], 'questions': store.document('questions'),
                                'capabilities': s.capabilities(), 'jobs': [s.job_summary(j) for j in store.jobs()]})
        if match := re.fullmatch(r'/api/admin/groups/([a-f0-9]{24})/history', path):
            return h.send_json({'runs': s.public.history(match[1], include_accounts=True)})
        if match := re.fullmatch(r'/api/admin/jobs/([a-f0-9]{32})', path):
            return h.send_json({'job': s.job_summary(store.job(match[1])), 'results': s.admin_results(match[1])})
    if path == '/api/admin/inspections' and method == 'POST':
        from .inspection import save
        fields(body, ['value'])
        return h.send_json(save(s, body['value'], actor), 201)
    if match := re.fullmatch(r'/api/admin/inspections/([a-f0-9]{32})(/run)?', path):
        from .inspection import save
        if method == 'PUT' and not match[2]:
            fields(body, ['value', 'revision'])
            return h.send_json(save(s, body['value'], actor, match[1], body['revision']))
        if method == 'POST' and match[2]:
            fields(body, ['request_key'])
            s.sync_inventory(token, actor)
            preview = store.resolve(store.plan(match[1]))
            return h.send_json(s.enqueue({'plan_id': match[1], 'request_key': body['request_key'], 'confirmed': True,
                **{key: preview[key] for key in ('plan_revision','settings_revision','inventory_revision','questions_revision')}}, token, actor), 202)
    if path == '/api/admin/inventory/sync' and method == 'POST':
        fields(body, [])
        return h.send_json(s.sync_inventory(token, actor))
    if path=='/api/admin/automation' and method=='PUT':
        fields(body,['key'])
        return h.send_json(s.save_automation(body['key'],actor))
    if path == '/api/admin/quarantine-key' and method == 'PUT':
        fields(body, ['key'])
        with s.lock:
            return h.send_json(s.quarantine.save_key(body['key'], actor))
    if match := re.fullmatch(r'/api/admin/quarantines/([1-9][0-9]{0,18})/handover', path):
        if method == 'POST':
            fields(body, [])
            return h.send_json(s.quarantine.handover(int(match[1]), actor))
    if match := re.fullmatch(r'/api/admin/quarantines/([1-9][0-9]{0,18})/retry', path):
        if method == 'POST':
            fields(body, [])
            return h.send_json(s.quarantine.retry(int(match[1]), actor))
    if path=='/api/admin/questions' and method=='PUT':
        fields(body,['value','revision'])
        return h.send_json(store.save_document('questions',body['value'],body['revision'],actor))
    if path == '/api/admin/access' and method == 'PUT':
        fields(body, ['value', 'revision'])
        return h.send_json(store.save_document('access', body['value'], body['revision'], actor))
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
