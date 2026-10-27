"""Docker-backed OpenCode sessions for persistent project assignments.

Only a private per-task OpenCode config and a filtered project snapshot are shared
with the host. Project files are copied as safe regular files; the task container
never receives a host project bind mount, Docker socket, or app credentials.
"""
import copy
import errno
import io
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import socket
import subprocess
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingMixIn, UnixStreamServer
from urllib.parse import unquote, urlsplit

import requests

from agent_opencode import AGENT, OpenCode, SYSTEM
import agent_store

IMAGE_VERSION = (Path(__file__).resolve().parent/'agent_container'/'IMAGE_VERSION').read_text().strip()
IMAGE = 'hearth-agent-opencode:'+IMAGE_VERSION
SESSION_SEPARATOR = '::'
SKIP_PARTS = {'.git', 'node_modules', '.venv', 'venv', '__pycache__'}
MAX_PROJECT_BYTES = 2 * 1024 * 1024 * 1024
SYSTEM_CA_BUNDLES = ('/etc/ssl/certs/ca-certificates.crt',
                    '/etc/pki/tls/certs/ca-bundle.crt')


def _system_ca_bundle():
    """Use the host's installed CA roots, which may include its private model CA."""
    return next((path for path in SYSTEM_CA_BUNDLES if Path(path).is_file()), True)


def parse_jsonc(path):
    import re
    raw = Path(path).read_text()
    raw = re.sub(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/',
                 lambda match: match.group(1) or '', raw, flags=re.S)
    raw = re.sub(r'("(?:\\.|[^"\\])*")|,([\s]*[}\]])',
                 lambda match: match.group(1) or match.group(2), raw)
    return json.loads(raw)


class _ModelProxyHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def do_GET(self): self._proxy()
    def do_POST(self): self._proxy()
    def do_OPTIONS(self): self._proxy()

    def log_message(self, *_args):
        return

    def _proxy(self):
        proxy = self.server.proxy_config
        if not secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer '+proxy['token']):
            self.send_error(403)
            return
        request_path = unquote(urlsplit(self.path).path)
        if (not request_path.startswith('/v1/') or request_path.startswith('//')
                or '\\' in request_path or any(part in ('.', '..') for part in request_path.split('/'))):
            self.send_error(404)
            return
        suffix = self.path[len('/v1'):]
        headers = {}
        for key in ('accept', 'content-type', 'openai-organization', 'openai-project'):
            if self.headers.get(key): headers[key] = self.headers[key]
        headers.update(proxy.get('headers') or {})
        headers['Accept-Encoding'] = 'identity'
        key = proxy.get('api_key')
        if key: headers['Authorization'] = 'Bearer '+key
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length > 128 * 1024 * 1024:
                self.send_error(413)
                return
            body = self.rfile.read(length) if length else None
            response = requests.request(self.command, proxy['base_url']+suffix,
                params=None, headers=headers, data=body, stream=True,
                timeout=(15, 3600), verify=proxy.get('verify_tls', True))
            if response.status_code >= 500:
                logging.warning('Agent model upstream returned HTTP %s for %s (%s bytes).',
                                response.status_code, request_path, length)
            self.send_response(response.status_code)
            for name in ('Content-Type', 'Content-Length', 'Cache-Control', 'Retry-After'):
                if response.headers.get(name): self.send_header(name, response.headers[name])
            self.send_header('Connection', 'close')
            self.close_connection = True
            self.end_headers()
            for chunk in response.iter_content(64 * 1024):
                if chunk:
                    self.wfile.write(chunk)
                    self.wfile.flush()
            response.close()
        except (requests.RequestException, OSError, ValueError) as exc:
            logging.warning('Agent model relay failed for %s (%s bytes): %s: %s',
                            request_path, length if 'length' in locals() else 0,
                            type(exc).__name__, str(exc)[:500])
            try: self.send_error(502)
            except OSError: pass


class _ModelProxy:
    def __init__(self, info):
        self.info = info
        self.server = None
        self.thread = None

    def start(self):
        if self.server:
            return self.info['socket_path']
        path = Path(self.info['socket_path'])
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.exists():
            # A running Hearth process may already own this relay socket. Never unlink
            # its filesystem entry: doing so leaves its listener alive but unreachable,
            # which makes container requests fail with ECONNRESET. Reuse a live socket;
            # remove only stale socket paths left after a process shutdown.
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(1)
                probe.connect(str(path))
            except OSError as exc:
                if exc.errno not in (errno.ENOENT, errno.ECONNREFUSED, errno.ENOTSOCK):
                    raise RuntimeError('Could not safely check the existing agent model relay socket.') from exc
                path.unlink(missing_ok=True)
            else:
                return str(path)
            finally:
                probe.close()
        server_type = type('ThreadedUnixHTTPServer', (ThreadingMixIn, UnixStreamServer),
                           {'daemon_threads': True, 'allow_reuse_address': True})
        self.server = server_type(str(path), _ModelProxyHandler)
        path.chmod(0o666)
        self.server.proxy_config = self.info
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True,
                                       name='agent-model-proxy')
        self.thread.start()
        return str(path)

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
            Path(self.info['socket_path']).unlink(missing_ok=True)


class _EndpointOpenCode(OpenCode):
    def __init__(self, url, password):
        self.endpoint = url.rstrip('/')
        self.password = password

    def call(self, method, path, data=None, params=None, read_timeout=30):
        response = requests.request(method, self.endpoint+path, json=data, params=params,
            auth=('opencode', self.password), timeout=(5, read_timeout))
        if not response.ok:
            raise RuntimeError(f'OpenCode {response.status_code}: {response.text[:400]}')
        return response.json() if response.content else {}


class ContainerOpenCode(OpenCode):
    """OpenCode API adapter whose coding sessions execute in locked-down Docker tasks."""
    def __init__(self, runtime_directory):
        super().__init__()
        self.runtime_directory = Path(runtime_directory)
        self.runtime_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.runtime_directory.chmod(0o700)
        self._lock = threading.RLock()
        self._proxies = {}

    def migrate_legacy_runtime(self):
        """Move old host JSON metadata into PostgreSQL and discard its container copy."""
        for path in self.runtime_directory.glob('hearth-agent-*.json'):
            try:
                info = json.loads(path.read_text())
                task_id = info['task_id']
                if agent_store.get_task(task_id):
                    agent_store.save_runtime(task_id, info)
                try: self._docker('rm', '--force', info['container'], timeout=60)
                except RuntimeError as exc:
                    if 'No such container' not in str(exc):
                        logging.warning('Could not remove legacy agent container %s: %s', info.get('container'), exc)
                path.unlink(missing_ok=True)
                shutil.rmtree(self.runtime_directory/task_id, ignore_errors=True)
            except (ValueError, KeyError, OSError):
                logging.exception('Could not migrate legacy agent runtime metadata from %s', path)
        for path in self.runtime_directory.iterdir():
            if path.is_dir() and re.fullmatch(r'[0-9a-f]{32}', path.name):
                # A task directory contains the model relay socket and identifies a
                # live/recoverable container. Preserve it when PostgreSQL still has
                # both the assignment and its runtime metadata; this is normal after
                # a Hearth restart, when active tasks have just become interrupted.
                # Only remove directories whose durable records are gone.
                try:
                    task = agent_store.get_task(path.name)
                    runtime = agent_store.get_runtime(path.name)
                except Exception:
                    logging.exception('Could not verify agent runtime %s; leaving it untouched.', path.name)
                    continue
                if task and runtime:
                    continue
                name = self._container_name(path.name)
                try: self._docker('rm', '--force', name, timeout=60)
                except RuntimeError as exc:
                    if 'No such container' not in str(exc):
                        logging.warning('Could not remove orphaned agent container %s: %s', name, exc)
                shutil.rmtree(path, ignore_errors=True)

    def ensure_agent(self):
        # The isolated runtime receives a private agent definition in its generated config.
        # Image construction happens in the durable worker so the UI keeps showing progress.
        return None

    def _docker(self, *args, timeout=120, input_data=None):
        result = subprocess.run(['docker', *map(str, args)], input=input_data,
            capture_output=True, timeout=timeout)
        if result.returncode:
            message = (result.stderr or result.stdout).decode('utf-8', 'replace')[-2000:]
            raise RuntimeError('Docker agent runtime failed: '+message)
        return result.stdout.decode('utf-8', 'replace').strip()

    def _ensure_image(self, job=None):
        try:
            if self._docker('image', 'inspect', IMAGE, timeout=20): return
        except (RuntimeError, FileNotFoundError, subprocess.TimeoutExpired):
            pass
        context = Path(__file__).resolve().parent/'agent_container'
        self._report(job, 'Building the isolated OpenCode image with Python, Java, Node.js, and build tools.')
        self._docker('build', '--pull', '--tag', IMAGE, '--build-arg', 'OPENCODE_VERSION='+IMAGE_VERSION,
                     context, timeout=1800)
        self._report(job, 'Agent runtime image built successfully.')

    def _report(self, job, text):
        callback = getattr(self, 'progress', None)
        if callback and job:
            callback(job, text)

    def models(self):
        return super().models()

    def _container_name(self, task_id):
        if not re.fullmatch(r'[0-9a-f]{32}', task_id):
            raise ValueError('Invalid agent assignment ID.')
        return 'hearth-agent-'+task_id[:24]

    def _runtime_path(self, name):
        return self.runtime_directory/(name+'.json')

    def _write_runtime(self, name, info):
        agent_store.save_runtime(info['task_id'], info)

    def _read_runtime(self, name):
        info = agent_store.get_runtime_by_container(name)
        if not info:
            # One-time import for installations upgrading from file-backed metadata.
            path = self._runtime_path(name)
            if not path.is_file():
                raise RuntimeError('Agent container metadata is missing from PostgreSQL.')
            info = json.loads(path.read_text())
            if agent_store.get_task(info.get('task_id')):
                agent_store.save_runtime(info['task_id'], info)
            path.unlink(missing_ok=True)
        if not isinstance(info, dict) or info.get('container') != name:
            raise RuntimeError('Agent container metadata is invalid.')
        proxy = info.get('model_proxy')
        if isinstance(proxy, dict):
            # Older assignments stored boolean True, which makes requests use
            # certifi instead of the host trust store containing private model CAs.
            # Migrate persisted metadata while keeping certificate checks enabled.
            ca_bundle = _system_ca_bundle()
            if proxy.get('verify_tls') != ca_bundle:
                proxy['verify_tls'] = ca_bundle
                self._write_runtime(name, info)
        return info

    def _host_provider(self, provider_id, model_id, socket_path):
        config_path = Path.home()/'.config/opencode/opencode.jsonc'
        config = parse_jsonc(config_path)
        source = (config.get('provider') or {}).get(provider_id)
        if not isinstance(source, dict):
            raise ValueError('The selected model provider is not configured in OpenCode.')
        models = source.get('models') or {}
        model = models.get(model_id)
        if not isinstance(model, dict):
            # Some providers expose aliases in their model inventory; do not broaden the
            # container config to unrelated models if the exact model is missing.
            raise ValueError('The selected model is missing from the OpenCode provider configuration.')
        options = source.get('options') or {}
        base_url = str(options.get('baseURL') or '').rstrip('/')
        parsed = urlsplit(base_url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or not parsed.path.endswith('/v1'):
            raise ValueError('The selected model provider has an unsupported API endpoint.')
        proxy_info = {'base_url': base_url, 'api_key': options.get('apiKey'),
                      'headers': options.get('headers') or {}, 'verify_tls': _system_ca_bundle(),
                      'socket_path': str(socket_path), 'token': secrets.token_urlsafe(32)}
        provider = copy.deepcopy(source)
        provider['models'] = {model_id: copy.deepcopy(model)}
        provider_options = dict(provider.get('options') or {})
        provider_options.update({'baseURL': '', 'apiKey': proxy_info['token']})
        provider_options.pop('headers', None)
        provider['options'] = provider_options
        return config, provider, proxy_info

    def _agent_config(self, original, provider_id, model_id, provider, opencode_steps=40):
        # This provider option is understood by compatible chat-template providers
        # such as Lemonade. Preserve other provider options and custom extraBody keys.
        extra_body = provider['options'].get('extraBody')
        if not isinstance(extra_body, dict):
            extra_body = {}
        else:
            extra_body = dict(extra_body)
        template_kwargs = extra_body.get('chat_template_kwargs')
        if not isinstance(template_kwargs, dict):
            template_kwargs = {}
        else:
            template_kwargs = dict(template_kwargs)
        enable_thinking = original.get('enable_thinking', True)
        # Lemonade's current llama.cpp path uses the top-level request control to
        # reach native reasoning settings; retain the template option for providers
        # that consume Qwen-style chat-template kwargs.
        if provider_id == 'lemonade':
            extra_body['enable_thinking'] = enable_thinking
        extra_body['chat_template_kwargs'] = {**template_kwargs,
            'enable_thinking': enable_thinking}
        provider['options']['extraBody'] = extra_body
        provider['options']['baseURL'] = 'http://127.0.0.1:4097/v1'
        return {'$schema': 'https://opencode.ai/config.json', 'model': f'{provider_id}/{model_id}',
                'provider': {provider_id: provider},
                'agents': {AGENT: {'description': 'Isolated persistent Hearth project worker',
                    'mode': 'primary', 'system': SYSTEM,
                    'steps': opencode_steps}},
                'compaction': original.get('compaction', {'auto': True})}

    def _make_proxy(self, name, info):
        with self._lock:
            proxy = self._proxies.get(name)
            if proxy:
                return proxy
            proxy = _ModelProxy(info)
            proxy.start()
            self._proxies[name] = proxy
            return proxy

    def _start_container(self, name, info, task_dir, job=None):
        self._report(job, 'Starting the isolated Docker container and OpenCode service.')
        password = info['password']
        memory = (os.environ.get('HEARTH_AGENT_MEMORY_LIMIT') or '8g').strip()
        cpus = (os.environ.get('HEARTH_AGENT_CPU_LIMIT') or '4').strip()
        pids = (os.environ.get('HEARTH_AGENT_PID_LIMIT') or '512').strip()
        memory_match = re.fullmatch(r'([1-9][0-9]{0,8})([kKmMgG]?)', memory)
        if not memory_match:
            raise ValueError('HEARTH_AGENT_MEMORY_LIMIT must be a Docker memory value such as 8g.')
        memory_bytes = int(memory_match.group(1))
        memory_unit = memory_match.group(2).lower()
        memory_bytes *= {'': 1, 'k': 1024, 'm': 1024**2, 'g': 1024**3}[memory_unit]
        if not 512 * 1024**2 <= memory_bytes <= 256 * 1024**3:
            raise ValueError('HEARTH_AGENT_MEMORY_LIMIT must be between 512m and 256g.')
        try:
            cpu_value = float(cpus)
        except ValueError as exc:
            raise ValueError('HEARTH_AGENT_CPU_LIMIT must be between 0.5 and 64.') from exc
        if not 0.5 <= cpu_value <= 64:
            raise ValueError('HEARTH_AGENT_CPU_LIMIT must be between 0.5 and 64.')
        if not re.fullmatch(r'[1-9][0-9]{1,4}', pids) or not 64 <= int(pids) <= 4096:
            raise ValueError('HEARTH_AGENT_PID_LIMIT must be between 64 and 4096.')
        startup = ('umask 0002; mkdir -p "$HOME/agents/project"; '
                   'printf "%s" "$HEARTH_OPENCODE_CONFIG" > "$OPENCODE_CONFIG"; '
                   'socat TCP-LISTEN:4097,bind=127.0.0.1,reuseaddr,fork UNIX-CONNECT:/relay/model.sock & '
                   'exec opencode serve --hostname 0.0.0.0 --port 4096')
        self._docker('run', '--detach', '--name', name, '--label', 'hearth.agent-task='+info['task_id'],
            '--restart', 'unless-stopped', '--network', 'bridge',
            '--publish', '127.0.0.1::4096', '--memory', memory, '--cpus', str(cpu_value), '--pids-limit', pids,
            '--cap-drop', 'ALL', '--cap-add', 'CHOWN', '--cap-add', 'DAC_OVERRIDE', '--cap-add', 'FOWNER',
            '--cap-add', 'FSETID', '--cap-add', 'SETGID', '--cap-add', 'SETUID', '--cap-add', 'SYS_CHROOT',
            '--security-opt', 'no-new-privileges:true', '--tmpfs', '/tmp:rw,nosuid,nodev,size=2g',
            '--env', 'HOME=/home/node', '--env', 'OPENCODE_CONFIG=/home/node/agents/opencode.json',
            '--env', 'HEARTH_OPENCODE_CONFIG='+json.dumps(info['opencode_config'], separators=(',', ':')),
            '--env', 'OPENCODE_SERVER_USERNAME=opencode', '--env', 'OPENCODE_SERVER_PASSWORD='+password,
            '--mount', f'type=bind,src={task_dir},dst=/home/node/agents',
            '--mount', f'type=bind,src={task_dir},dst=/relay,readonly',
            '--entrypoint', '/bin/sh', IMAGE, '-lc', startup, timeout=180)
        port = ''
        deadline = time.time()+45
        while time.time() < deadline:
            port = self._docker('port', name, '4096/tcp', timeout=10).split(':')[-1]
            try:
                response = requests.get(f'http://127.0.0.1:{port}/api/agent',
                    auth=('opencode', password), timeout=2)
                if response.ok: break
            except requests.RequestException: pass
            time.sleep(.5)
        else:
            raise RuntimeError('The OpenCode service did not start inside the agent container.')
        info.update(port=int(port))
        self._write_runtime(name, info)
        self._report(job, 'OpenCode is ready inside the container; syncing the project workspace.')

    def create(self, job):
        try:
            return self._create_container_session(job)
        except Exception:
            try: self.cleanup(job)
            except Exception: logging.exception('Could not clean up failed agent container startup')
            raise

    def _create_container_session(self, job):
        self._ensure_image(job)
        task_id = job['id']
        name = self._container_name(task_id)
        task_dir = self.runtime_directory/task_id
        task_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        task_dir.chmod(0o700)
        (task_dir/'project').mkdir(mode=0o700, exist_ok=True)
        proxy_path = task_dir/'model.sock'
        original, provider, proxy_info = self._host_provider(job['model']['providerID'], job['model']['id'], proxy_path)
        config = self._agent_config(original, job['model']['providerID'], job['model']['id'], provider,
                                    job.get('opencode_steps', 40))
        existing = agent_store.get_runtime(task_id)
        if existing:
            can_reuse = self._can_reuse_container(existing, job, config, proxy_info)
            if can_reuse:
                try:
                    image = self._docker('inspect', '-f', '{{.Config.Image}}', name, timeout=15)
                    can_reuse = image == IMAGE
                except RuntimeError as exc:
                    if 'No such object' not in str(exc) and 'No such container' not in str(exc):
                        raise
                    can_reuse = False
            if can_reuse:
                self._report(job, 'Reusing the saved Docker container and OpenCode installation.')
                session = self._endpoint(name, existing)
                data = {'title': 'Hearth: '+job['title'], 'agent': AGENT,
                        'model': job['model'], 'location': {'directory': '/home/node/agents/project'},
                        'permissions': self._permissions(job)}
                result = session.call('POST', '/api/session', data)['data']['id']
                self._docker('exec', name, 'sh', '-lc',
                             'rm -rf -- /home/node/agents/project && mkdir -p /home/node/agents/project')
                self._sync_to_container(name, job['owner'], workspace_id=task_id)
                existing['last_sync'] = 0
                self._write_runtime(name, existing)
                self._report(job, 'Saved project workspace synced into the reused container.')
                return name+SESSION_SEPARATOR+result
            reason = ('Container settings changed' if existing.get('model') != job.get('model') or
                      not self._can_reuse_container(existing, job, config, proxy_info)
                      else 'The previous container or image is unavailable')
            self._report(job, reason+'; replacing it before starting this run.')
            self.cleanup(job)
        proxy = self._make_proxy(name, proxy_info)
        info = {'container': name, 'task_id': task_id, 'password': secrets.token_urlsafe(32),
                'owner': job['owner'], 'model': job['model'], 'model_proxy': proxy.info,
                'opencode_config': config,
                'last_sync': 0, 'manifest': [], 'project_directory': '/home/node/agents/project'}
        self._write_runtime(name, info)
        self._start_container(name, info, task_dir, job)
        session = _EndpointOpenCode(f"http://127.0.0.1:{info['port']}", info['password'])
        data = {'title': 'Hearth: '+job['title'], 'agent': AGENT,
                'model': job['model'], 'location': {'directory': '/home/node/agents/project'},
                'permissions': self._permissions(job)}
        result = session.call('POST', '/api/session', data)['data']['id']
        self._sync_to_container(name, job['owner'], workspace_id=job['id'])
        self._report(job, 'Project synced. The agent is ready to start its first work cycle.')
        return name+SESSION_SEPARATOR+result

    @staticmethod
    def _can_reuse_container(existing, job, config, proxy_info):
        """Reuse only when the stopped service has the same model and provider settings."""
        if existing.get('model') != job.get('model'):
            return False
        old_proxy = existing.get('model_proxy') or {}
        proxy_fields = ('base_url', 'api_key', 'headers', 'verify_tls')
        if any(old_proxy.get(key) != proxy_info.get(key) for key in proxy_fields):
            return False
        old_config = copy.deepcopy(existing.get('opencode_config') or {})
        new_config = copy.deepcopy(config)
        provider_id = job['model']['providerID']
        try:
            old_config['provider'][provider_id]['options']['apiKey'] = 'relay-token'
            new_config['provider'][provider_id]['options']['apiKey'] = 'relay-token'
        except (KeyError, TypeError):
            return False
        return old_config == new_config

    def park(self, job):
        """Stop a completed task container but retain it and its runtime for its next run."""
        task_id = job.get('id')
        if not task_id:
            return
        name = self._container_name(task_id)
        with self._lock:
            proxy = self._proxies.pop(name, None)
        try:
            if proxy:
                proxy.close()
            try:
                self._docker('stop', '--time', '5', name, timeout=60)
            except RuntimeError as exc:
                if 'No such container' not in str(exc):
                    raise
        except Exception:
            logging.exception('Could not park agent container %s', name)
            raise

    def resume(self, job):
        """Reattach to the persistent session or recreate it from the synced project."""
        session_id = job.get('session_id')
        if not session_id or SESSION_SEPARATOR not in str(session_id):
            return self.create(job)
        try:
            self.snapshot(session_id)
            return session_id
        except Exception as exc:
            if 'container is missing' not in str(exc):
                raise
        # A removed Docker container also removed its private OpenCode session DB.
        # PostgreSQL workspace files are the durable checkpoint for a fresh session.
        self.cleanup(job)
        recreated = self.create(job)
        self._report(job, 'Previous container was unavailable; resumed from the latest synchronized project files.')
        return recreated

    def _permissions(self, job):
        # OpenCode 2.1.2 applies last matching rule and evaluates multi-file
        # operations across all resources. Keep explicit denials last.
        permissions = [
            {'action': 'external_directory', 'resource': '/home/node/agents/project/*', 'effect': 'allow'},
            {'action': '*', 'resource': '*', 'effect': 'allow'}]
        permissions.append({'action': 'shell', 'resource': '*',
                            'effect': 'allow' if job.get('auto_commands') else 'ask'})
        permissions += [
            {'action': 'read', 'resource': '.env', 'effect': 'deny'},
            {'action': 'read', 'resource': '.env.*', 'effect': 'deny'},
            {'action': 'edit', 'resource': '.env', 'effect': 'deny'},
            {'action': 'edit', 'resource': '.env.*', 'effect': 'deny'},
            {'action': 'external_directory', 'resource': '*', 'effect': 'deny'},
            {'action': 'subagent', 'resource': '*', 'effect': 'deny'}]
        return permissions

    def _split(self, session_id):
        name, separator, raw_session = str(session_id).partition(SESSION_SEPARATOR)
        if not separator or not re.fullmatch(r'hearth-agent-[0-9a-f]{24}', name) or not raw_session:
            raise RuntimeError('Agent container session reference is invalid.')
        return name, raw_session, self._read_runtime(name)

    def _endpoint(self, name, info):
        self._make_proxy(name, info['model_proxy'])
        try:
            running = self._docker('inspect', '-f', '{{.State.Running}}', name, timeout=15)
        except RuntimeError as exc:
            message = str(exc).casefold()
            if 'no such container' in message or 'no such object' in message:
                raise RuntimeError('The isolated agent container is missing.') from exc
            raise
        if running != 'true':
            self._docker('start', name, timeout=60)
        return _EndpointOpenCode(f"http://127.0.0.1:{info['port']}", info['password'])

    @staticmethod
    def workspace_directory(_client, _job):
        return '/home/node/agents/project'

    def prompt(self, session, text, message_id):
        if SESSION_SEPARATOR not in str(session): return super().prompt(session, text, message_id)
        name, raw, info = self._split(session); return self._endpoint(name, info).prompt(raw, text, message_id)
    def snapshot(self, session):
        if SESSION_SEPARATOR not in str(session): return super().snapshot(session)
        name, raw, info = self._split(session); return self._endpoint(name, info).snapshot(raw)
    def live_messages(self, session):
        if SESSION_SEPARATOR not in str(session): return super().live_messages(session)
        name, raw, info = self._split(session); return self._endpoint(name, info).live_messages(raw)
    def interrupt(self, session):
        if SESSION_SEPARATOR not in str(session): return super().interrupt(session)
        name, raw, info = self._split(session); return self._endpoint(name, info).interrupt(raw)
    def reply(self, session, kind, item, value):
        if SESSION_SEPARATOR not in str(session): return super().reply(session, kind, item, value)
        name, raw, info = self._split(session); return self._endpoint(name, info).reply(raw, kind, item, value)

    def message(self, session, message):
        if SESSION_SEPARATOR not in str(session): return super().message(session, message)
        name, raw, info = self._split(session)
        result = self._endpoint(name, info).message(raw, message)
        return result

    def verify(self, session, command, message_id):
        if SESSION_SEPARATOR not in str(session): return super().verify(session, command, message_id)
        name, raw, info = self._split(session); return self._endpoint(name, info).verify(raw, command, message_id)
    def stop_verification(self, session, message_id):
        if SESSION_SEPARATOR not in str(session): return super().stop_verification(session, message_id)
        name, raw, info = self._split(session); return self._endpoint(name, info).stop_verification(raw, message_id)

    def _sync_to_container(self, name, owner, workspace_id=''):
        process = subprocess.Popen(['docker', 'exec', '-i', name, 'tar', '-C', '/home/node/agents/project', '-xf', '-'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        total = 0
        try:
            with tarfile.open(fileobj=process.stdin, mode='w|') as tar:
                for item in agent_store.list_workspace_files(owner, workspace_id=workspace_id):
                    rel = PurePosixPath(item['path'])
                    if any(part in SKIP_PARTS for part in rel.parts): continue
                    info = tarfile.TarInfo(rel.as_posix())
                    if item['is_dir']:
                        info.type = tarfile.DIRTYPE
                        info.mode = 0o755
                        tar.addfile(info)
                    else:
                        content = bytes(item['content'])
                        total += len(content)
                        if total > MAX_PROJECT_BYTES: raise RuntimeError('Project import exceeded the 2 GiB PostgreSQL workspace limit.')
                        info.size = len(content)
                        info.mode = 0o755 if item.get('executable') else 0o644
                        tar.addfile(info, io.BytesIO(content))
            process.stdin.close()
            code = process.wait(timeout=300)
            error = process.stderr.read().decode('utf-8', 'replace')[-1000:]
            if code: raise RuntimeError('Could not copy the project into the isolated container: '+error)
        except Exception:
            process.kill(); process.wait(timeout=5)
            raise

    def sync(self, job):
        if not job.get('session_id') or SESSION_SEPARATOR not in str(job['session_id']): return
        name, _raw, info = self._split(job['session_id'])
        now = time.time()
        if now-info.get('last_sync', 0) < 10 and job.get('status') not in ('completed', 'stopped'):
            return
        tar_args = ['docker', 'exec', name, 'tar', '-C', '/home/node/agents/project']
        for part in sorted(SKIP_PARTS):
            tar_args.append('--exclude='+part)
        tar_args.extend(['-cf', '-', '.'])
        process = subprocess.Popen(tar_args,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def finish_export():
            code = process.wait(timeout=300)
            error = process.stderr.read().decode('utf-8', 'replace')[-1000:]
            if code:
                raise RuntimeError('Could not synchronize the isolated project: '+error)
        try:
            agent_store.replace_workspace_from_tar(job['owner'], process.stdout, finish_export,
                                                   workspace_id=job['id'])
        except Exception:
            process.kill(); process.wait(timeout=5)
            raise
        info['last_sync'] = now
        self._write_runtime(name, info)

    def cleanup(self, job):
        task_id = job.get('id')
        if not task_id: return
        name = self._container_name(task_id)
        with self._lock:
            proxy = self._proxies.pop(name, None)
        try:
            if proxy: proxy.close()
            # Stop explicitly before removal so a stop/delete action cannot leave
            # a task container running, including if OpenCode failed to interrupt.
            for command in (('stop', '--time', '5', name), ('rm', '--force', name)):
                try:
                    self._docker(*command, timeout=60)
                except RuntimeError as exc:
                    if 'No such container' not in str(exc):
                        raise
        finally:
            shutil.rmtree(self.runtime_directory/task_id, ignore_errors=True)
            agent_store.delete_runtime(task_id)
