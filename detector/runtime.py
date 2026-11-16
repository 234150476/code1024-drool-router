"""Local job control. Only the worker process receives gateway credentials."""
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from . import config


class Runtime:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.Lock()
        self.job = {'running': False, 'action': None, 'finished_at': None, 'ok': None}
        self.stop = threading.Event()
        self.scheduler = None

    def credential_ready(self):
        path = os.environ.get('SUB2API_ADMIN_KEY_FILE', '')
        try:
            key = Path(path).read_text().strip() if path else os.environ.get('SUB2API_ADMIN_KEY', '')
            return len(key) >= 16
        except OSError:
            return False

    def status(self):
        with self.lock:
            job = dict(self.job)
        session_auth = bool(config.get('web.session_auth_url'))
        return {
            'configured': bool(config.get('base_url') and (self.credential_ready() or session_auth)),
            'gateway_configured': bool(config.get('base_url')),
            'credential_configured': self.credential_ready(),
            'session_auth': session_auth,
            'schedule_enabled': bool(config.get('schedule.enabled', False)),
            'regular_minutes': config.get('schedule.regular_minutes'),
            'quiet_minutes': config.get('schedule.quiet_minutes'),
            'timezone': config.get('schedule.timezone'),
            'frame_ancestors': config.get('web.frame_ancestors', []),
            'routing_writes': {'priority': config.write_enabled('priority'),
                               'callable': config.write_enabled('callable')},
            'platforms': [{'id': name, 'model': spec.get('model'),
                           'effort': spec.get('effort'), 'group_ids': spec.get('group_ids', [])}
                          for name, spec in config.enabled_platforms().items()],
            'job': job,
        }

    def start(self, action, session_token=None):
        if action not in ('sync', 'run'):
            raise ValueError('INVALID_ACTION')
        if not self.status()['configured'] or not (self.credential_ready() or session_token):
            raise ValueError('GATEWAY_NOT_CONFIGURED')
        with self.lock:
            if self.job['running']:
                return False
            self.job = {'running': True, 'action': action, 'started_at': time.time(),
                        'finished_at': None, 'ok': None}
        threading.Thread(target=self._execute, args=(action, session_token), daemon=True).start()
        return True

    def _execute(self, action, session_token=None):
        args = [sys.executable, '-m', 'detector.monitor', '--data', str(self.root),
                '--base-url', config.get('base_url'), '--source', 'timer']
        if action == 'sync':
            args.append('--metadata-only')
        # Routing writes require the config gate AND these explicit operator flags.
        if action == 'run':
            if config.write_enabled('priority'):
                args.append('--enable-priority-routing')
            if config.write_enabled('callable'):
                args.append('--enable-quality-routing')
        key_file = os.environ.get('SUB2API_ADMIN_KEY_FILE') if not session_token else None
        if key_file:
            args.extend(['--key-file', key_file])
        ok = False
        worker_env = dict(os.environ)
        if session_token:
            worker_env['DROOL_ADMIN_BEARER_TOKEN'] = session_token
            worker_env.pop('SUB2API_ADMIN_KEY_FILE', None)
            worker_env.pop('SUB2API_ADMIN_KEY', None)
        try:
            log = self.root / 'private' / 'worker.log'
            log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with log.open('w') as stream:
                os.chmod(log, 0o600)
                result = subprocess.run(args, cwd=Path(__file__).resolve().parent.parent,
                                        stdout=stream, stderr=stream, timeout=3600, check=False, env=worker_env)
                ok = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            with self.lock:
                self.job.update(running=False, finished_at=time.time(), ok=ok)

    def start_scheduler(self):
        if not config.get('schedule.enabled', False):
            return
        def loop():
            while not self.stop.wait(30):
                if self.status()['configured'] and self.credential_ready():
                    self.start('run')
        self.scheduler = threading.Thread(target=loop, daemon=True)
        self.scheduler.start()
