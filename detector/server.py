# SPDX-FileCopyrightText: 2026 NoonWake.AI
# SPDX-License-Identifier: LGPL-3.0-or-later

"""INPUT: read-only public projection. OUTPUT: static dashboard and sandboxed HTML. POS: credential-free HTTP service."""
import argparse
import hmac
import hashlib
import secrets
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import os
import time
from urllib.parse import parse_qs, urlsplit

from .controls import ControlConflict, Controls
from .supplier_prices import Prices, project_price
from .session_auth import verify_admin_session
from .preview_sessions import PreviewSessions
from .console_api import dispatch as console_dispatch
APP_CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
ARTIFACT_CSP = "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; media-src data: blob:; connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"


def frame_policy(origins):
    """Only exact origins are accepted; never interpolate arbitrary CSP text."""
    if not isinstance(origins, list):
        raise ValueError('web.frame_ancestors must be an array of exact origins')
    for origin in origins:
        parsed = urlsplit(origin)
        if (parsed.scheme not in ('https', 'http') or not parsed.hostname or
                parsed.username or parsed.password or parsed.path or parsed.query or
                parsed.fragment or parsed.netloc != parsed.netloc.strip() or
                any(c in origin for c in " ;'\"\r\n\t*")):
            raise ValueError('Invalid embedding origin')
    return APP_CSP.replace("frame-ancestors 'none'", "frame-ancestors 'self'" +
                           ''.join(' ' + origin for origin in origins))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self,request,client_address,server):
        super().__init__(request,client_address,server,directory=str(server.dist))

    def log_message(self,*args):
        pass

    def end_headers(self):
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('X-Robots-Tag','noindex, nofollow, noarchive')
        self.send_header('Permissions-Policy','camera=(), microphone=(), geolocation=(), payment=()')
        artifact_policy = ARTIFACT_CSP + ''.join(' ' + origin for origin in self.server.frame_ancestors)
        self.send_header('Content-Security-Policy', artifact_policy if getattr(self,'is_artifact',False) else self.server.app_csp)
        if not getattr(self,'is_artifact',False) and not self.server.frame_ancestors:
            self.send_header('X-Frame-Options','SAMEORIGIN')
        super().end_headers()

    def send_file(self,path,content_type,cache='no-store'):
        try:
            data=path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        if self.server.demo and path == self.server.dist / 'index.html':
            data = data.replace(b'<head>', b'<head><meta name="drool-demo" content="synthetic-preview">', 1)
        self.send_response(200)
        self.send_header('Content-Type',content_type)
        self.send_header('Cache-Control',cache)
        self.send_header('Content-Length',str(len(data)))
        self.end_headers()
        if self.command!='HEAD':
            self.wfile.write(data)

    def send_json(self, value, status=200):
        data=json.dumps(value,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(data)))
        self.end_headers()
        if self.command!='HEAD':
            self.wfile.write(data)

    def do_HEAD(self):
        self.do_GET()

    def authorized(self):
        self.admin_session_token = None
        expected = self.server.access_token
        supplied = self.headers.get('Authorization', '')
        if self.server.session_auth_url and supplied.startswith('Bearer '):
            if verify_admin_session(self.server.session_auth_url, supplied[7:],
                                    self.headers.get('User-Agent', ''),
                                    self.headers.get('X-Real-IP', '')):
                self.admin_session_token = supplied[7:]
                return True
        if self.server.console is not None:
            self.send_json({'error': 'AUTH_REQUIRED'}, 401)
            return False
        if ((expected or self.server.session_auth_url) and
                (not expected or not hmac.compare_digest(supplied.encode(), ('Bearer ' + expected).encode()))):
            self.send_json({'error': 'AUTH_REQUIRED'}, 401)
            return False
        return True

    def artifact_authorized(self, path):
        if not re.fullmatch(r'/artifacts/[a-f0-9]{32}\.html', path):
            return False
        query = parse_qs(urlsplit(self.path).query)
        expires = query.get('expires', [''])[0]
        signature = query.get('signature', [''])[0]
        if not expires.isdigit() or not time.time() < int(expires) <= time.time() + 601:
            return False
        expected = hmac.new(self.server.artifact_key, (path + ':' + expires).encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)

    def do_GET(self):
        self.is_artifact=False
        path=urlsplit(self.path).path
        if not self.server.access_token and urlsplit('http://' + self.headers.get('Host', '')).hostname not in ('localhost', '127.0.0.1', '::1'):
            return self.send_json({'error': 'HOST_REJECTED'}, 403)
        if console_dispatch(self, path):
            return
        if ((path.startswith('/api/') or path.startswith('/artifacts/')) and
                not self.artifact_authorized(path) and not self.authorized()):
            return
        if self.server.console is not None and path.startswith(('/api/', '/artifacts/')):
            return self.send_json({'error':'接口不存在'},404)
        if path=='/healthz':
            data=json.dumps({'status':'ok','state_ready':(self.server.public/'state.json').exists()}).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(len(data)))
            self.end_headers()
            if self.command!='HEAD':
                self.wfile.write(data)
            return
        if path == '/api/service' and self.server.runtime is not None:
            return self.send_json(self.server.runtime.status())
        artifact_link = re.fullmatch(r'/api/artifact-links/([a-f0-9]{32})', path)
        if artifact_link:
            ident = artifact_link[1]
            try:
                detail = json.loads((self.server.public / (ident + '.json')).read_text())
                if detail.get('kind') != 'drawing' or detail.get('status') != 'pass' or detail.get('slot', 0) < time.time() - 86400:
                    return self.send_error(404)
            except (OSError, ValueError):
                return self.send_error(404)
            target = '/artifacts/' + ident + '.html'
            expires = str(int(time.time()) + 600)
            signature = hmac.new(self.server.artifact_key, (target + ':' + expires).encode(), hashlib.sha256).hexdigest()
            return self.send_json({'url': target + '?expires=' + expires + '&signature=' + signature})
        if path=='/api/state':
            try:
                state=json.loads((self.server.public/'state.json').read_text())
                controls=self.server.controls.read()
                prices=self.server.prices.read()
                for account in state['accounts']:
                    account.update(controls.get(account['id'], {'paused':False,'resume_at':0}))
                    account['price']=prices.get(account['id'],project_price())
                state['controls_enabled']=self.server.controls_enabled
                if self.server.runtime is not None:
                    state['service'] = self.server.runtime.status()
            except (ValueError,OSError,KeyError):
                return self.send_json({'error':'STATE_UNAVAILABLE'},503)
            return self.send_json(state)
        match=re.fullmatch(r'/(api/runs|artifacts)/([a-f0-9]{32})(\.html)?',path)
        if match:
            kind,ident,ext=match.groups()
            if (kind=='artifacts') != bool(ext):
                return self.send_error(404)
            detail_path=self.server.public/(ident+'.json')
            try:
                detail=json.loads(detail_path.read_text())
            except (ValueError,OSError):
                return self.send_error(404)
            if detail.get('slot',0)<time.time()-86400:
                return self.send_error(410,'History expired')
            if kind=='artifacts':
                if detail.get('status')!='pass' or detail.get('kind')!='drawing':
                    return self.send_error(404)
                self.is_artifact=True
                return self.send_file(self.server.public/(ident+'.html'),'text/html; charset=utf-8')
            return self.send_file(detail_path,'application/json; charset=utf-8')
        if path in ('/','/index.html'):
            return self.send_file(self.server.dist/'index.html','text/html; charset=utf-8')
        if re.fullmatch(r'/assets/[a-zA-Z0-9._-]+\.(js|css|woff2|png|svg|jpg|jpeg)',path):
            target=self.server.dist/path.lstrip('/')
            return self.send_file(target,self.guess_type(str(target)),'public, max-age=31536000, immutable')
        return self.send_error(404)

    def do_POST(self):
        self.is_artifact=False
        if not self.server.access_token and urlsplit('http://' + self.headers.get('Host', '')).hostname not in ('localhost', '127.0.0.1', '::1'):
            return self.send_json({'error': 'HOST_REJECTED'}, 403)
        if console_dispatch(self, urlsplit(self.path).path):
            return
        if not self.authorized():
            return
        if self.server.console is not None:
            return self.send_json({'error':'接口不存在'},404)
        path = urlsplit(self.path).path
        if path == '/api/jobs' and self.server.runtime is not None:
            return self.start_job()
        match=re.fullmatch(r'/api/accounts/([a-f0-9]{12})/(pause|price)',path)
        if self.command!='POST' or not match or not self.server.controls_enabled:
            return self.send_error(405,'Read-only service')
        origin=self.headers.get('Origin','')
        if not self.valid_origin(origin):
            return self.send_json({'error':'ORIGIN_REJECTED'},403)
        if self.headers.get('Content-Type')!='application/json' or self.headers.get('Transfer-Encoding'):
            return self.send_json({'error':'JSON_REQUIRED'},415)
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=(4096 if match[2]=='price' else 512):
                return self.send_json({'error':'INVALID_BODY_SIZE'},413)
            self.connection.settimeout(5)
            body=json.loads(self.rfile.read(length))
            state=json.loads((self.server.public/'state.json').read_text())
            if match[1] not in {a['id'] for a in state['accounts']}:
                return self.send_json({'error':'UNKNOWN_ACCOUNT'},404)
            if match[2]=='pause':
                if not isinstance(body,dict) or set(body)!={'paused','expected_paused'} or any(type(v) is not bool for v in body.values()):
                    raise ValueError('INVALID_CONTROL')
                result=self.server.controls.set_paused(match[1],body['paused'],body['expected_paused'])
            else:
                if isinstance(body,dict) and set(body)=={'config','expected_revision'}:
                    result=self.server.prices.set_config(match[1],body['config'],body['expected_revision'])
                elif isinstance(body,dict) and set(body)=={'multiplier','expected_multiplier'}:
                    result=self.server.prices.set(match[1],body['multiplier'],body['expected_multiplier'])
                else:
                    raise ValueError('INVALID_PRICE')
            return self.send_json(result)
        except ControlConflict:
            return self.send_json({'error':'CONTROL_CHANGED'},409)
        except (ValueError,TypeError):
            return self.send_json({'error':'INVALID_CONTROL'},400)
        except OSError:
            return self.send_json({'error':'CONTROL_UNAVAILABLE'},503)

    def valid_origin(self, origin):
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return False
        if self.server.public_origin:
            return origin == self.server.public_origin
        return bool(origin and parsed.netloc == self.headers.get('Host') and
                    parsed.scheme in ('http', 'https') and not parsed.path and
                    not parsed.query and not parsed.fragment and not parsed.username)

    def start_job(self):
        if self.command != 'POST':
            return self.send_json({'error': 'METHOD_NOT_ALLOWED'}, 405)
        if not self.valid_origin(self.headers.get('Origin', '')):
            return self.send_json({'error': 'ORIGIN_REJECTED'}, 403)
        if self.headers.get('Content-Type') != 'application/json' or self.headers.get('Transfer-Encoding'):
            return self.send_json({'error': 'JSON_REQUIRED'}, 415)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 512:
                return self.send_json({'error': 'INVALID_BODY_SIZE'}, 413)
            self.connection.settimeout(5)
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict) or set(body) != {'action'}:
                raise ValueError('INVALID_ACTION')
            session_token = getattr(self, 'admin_session_token', None)
            started = (self.server.runtime.start(body['action'], session_token=session_token)
                       if session_token else self.server.runtime.start(body['action']))
            if not started:
                return self.send_json({'error': 'JOB_RUNNING'}, 409)
            return self.send_json(self.server.runtime.status(), 202)
        except (ValueError, TypeError):
            return self.send_json({'error': 'JOB_NOT_READY', 'message': '请先配置网关地址、管理员凭据和检测范围。'}, 400)

    do_PUT=do_POST
    do_DELETE=do_POST
    do_PATCH=do_POST


def make_server(host,port,dist,public,controls_enabled=False, *, access_token='',
                frame_ancestors=None, public_origin='', runtime=None, demo=False, session_auth_url='', console=None):
    if access_token and len(access_token) < 24:
        raise ValueError('DROOL_WEB_TOKEN must have at least 24 characters')
    if host not in ('127.0.0.1', '::1', 'localhost') and len(access_token) < 24 and not demo:
        raise ValueError('Non-loopback binding requires DROOL_WEB_TOKEN (at least 24 characters)')
    if controls_enabled and len(access_token) < 24:
        raise ValueError('Write controls require DROOL_WEB_TOKEN (at least 24 characters)')
    origins = frame_ancestors or []
    policy = frame_policy(origins)
    server=ThreadingHTTPServer((host,port),Handler)
    server.daemon_threads=True
    server.dist=Path(dist)
    server.public=Path(public)
    server.controls=Controls(server.public.parent/'controls')
    server.prices=Prices(server.public.parent/'controls')
    server.controls_enabled=controls_enabled
    server.access_token = access_token
    server.frame_ancestors = origins
    server.app_csp = policy
    server.public_origin = public_origin
    server.runtime = runtime
    server.demo = demo
    server.session_auth_url = session_auth_url
    server.console = console
    server.artifact_key = secrets.token_bytes(32)
    server.preview_sessions = PreviewSessions()
    return server


if __name__=='__main__':
    from . import config
    from .console_service import ConsoleService
    from .monitor import Store
    parser=argparse.ArgumentParser()
    parser.add_argument('--host',default=config.get('web.host') or '127.0.0.1')
    parser.add_argument('--port',type=int,default=int(config.get('web.port') or 4191))
    # The Vite build lands in web/dist; deployments may override this with --dist.
    parser.add_argument('--dist',default=str(Path(__file__).resolve().parent.parent/'web'/'dist'))
    parser.add_argument('--public',default=str(config.data_dir()/'public'))
    parser.add_argument('--enable-public-controls',action='store_true',help='Enable authenticated pause/price controls (requires a dashboard token)')
    args=parser.parse_args()
    if not args.enable_public_controls and config.get('web.public_controls'):
        args.enable_public_controls = True
    console = ConsoleService(Path(args.public).parent)
    store = Store(Path(args.public).parent)
    store.publish_state()
    token_file = os.environ.get('DROOL_WEB_TOKEN_FILE', '')
    token = Path(token_file).read_text().strip() if token_file else os.environ.get('DROOL_WEB_TOKEN', '')
    httpd = make_server(args.host,args.port,args.dist,args.public,args.enable_public_controls,
                        access_token=token,
                        frame_ancestors=config.get('web.frame_ancestors', []),
                        public_origin=config.get('web.public_origin', ''), console=console,
                        session_auth_url=config.get('web.session_auth_url', ''))
    print('AI Drool Router listening on http://%s:%d (schedule=%s)' %
          (args.host, args.port, bool(config.get('schedule.enabled'))), flush=True)
    try:
        httpd.serve_forever()
    finally:
        console.stop.set()
        if getattr(console, 'self_service', None) is not None:
            console.self_service.stop.set()
        httpd.server_close()
