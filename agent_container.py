"""Docker-backed OpenCode sessions for persistent project assignments.

Only a private per-task OpenCode config and a filtered project snapshot are shared
with the host. Project files are copied as safe regular files; the task container
never receives a host project bind mount, Docker socket, or app credentials.
"""
import copy
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingMixIn, UnixStreamServer
from urllib.parse import unquote, urlsplit

import requests

from agent_opencode import AGENT, OpenCode, SYSTEM

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


def _safe_member(name):
    path = PurePosixPath(name)
    return not path.is_absolute() and all(part not in ('', '.', '..') for part in path.parts)


def _safe_target(root, target):
    if target == root:
        return True
    if target.is_symlink() or not target.resolve().is_relative_to(root):
        return False
    parent = target.parent
    while parent != root:
        if parent.is_symlink():
            return False
        parent = parent.parent
    return True


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
        except (requests.RequestException, OSError, ValueError):
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
        path.unlink(missing_ok=True)
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

    @staticmethod
    def _project_path(value):
        expected_root = Path.home().resolve()/'agents'
        expected = expected_root/'project'
        supplied = Path(value)
        if expected_root.is_symlink() or expected.is_symlink() or supplied != expected:
            raise ValueError('Agent projects must use the server-managed ~/agents/project directory.')
        return expected

    def _runtime_path(self, name):
        return self.runtime_directory/(name+'.json')

    def _write_runtime(self, name, info):
        path = self._runtime_path(name)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(info, indent=2))
        tmp.chmod(0o600)
        tmp.replace(path)

    def _read_runtime(self, name):
        path = self._runtime_path(name)
        info = json.loads(path.read_text())
        if not isinstance(info, dict) or info.get('container') != name:
            raise RuntimeError('Agent container metadata is invalid.')
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

    def _agent_config(self, original, provider_id, model_id, provider):
        provider['options']['baseURL'] = 'http://127.0.0.1:4097/v1'
        return {'$schema': 'https://opencode.ai/config.json', 'model': f'{provider_id}/{model_id}',
                'provider': {provider_id: provider},
                'agents': {AGENT: {'description': 'Isolated persistent Hearth project worker',
                    'mode': 'primary', 'system': SYSTEM, 'steps': 40}},
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
        self._docker('run', '--detach', '--name', name, '--label', 'hearth.agent-task='+info['task_id'],
            '--restart', 'unless-stopped', '--network', 'bridge',
            '--publish', '127.0.0.1::4096', '--memory', memory, '--cpus', str(cpu_value), '--pids-limit', pids,
            '--cap-drop', 'ALL', '--cap-add', 'CHOWN', '--cap-add', 'DAC_OVERRIDE', '--cap-add', 'FOWNER',
            '--cap-add', 'FSETID', '--cap-add', 'SETGID', '--cap-add', 'SETUID', '--cap-add', 'SYS_CHROOT',
            '--security-opt', 'no-new-privileges:true', '--tmpfs', '/tmp:rw,nosuid,nodev,size=2g',
            '--env', 'OPENCODE_CONFIG=/relay/opencode.json',
            '--env', 'OPENCODE_SERVER_USERNAME=opencode', '--env', 'OPENCODE_SERVER_PASSWORD='+password,
            '--mount', f'type=bind,src={task_dir},dst=/relay,readonly',
            IMAGE, timeout=180)
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
            self.cleanup(job)
            raise

    def _create_container_session(self, job):
        self._ensure_image(job)
        task_id = job['id']
        name = self._container_name(task_id)
        task_dir = self.runtime_directory/task_id
        task_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        task_dir.chmod(0o700)
        config_path = task_dir/'opencode.json'
        proxy_path = task_dir/'model.sock'
        original, provider, proxy_info = self._host_provider(job['model']['providerID'], job['model']['id'], proxy_path)
        proxy = self._make_proxy(name, proxy_info)
        config = self._agent_config(original, job['model']['providerID'], job['model']['id'], provider)
        config_path.write_text(json.dumps(config, indent=2)+'\n')
        config_path.chmod(0o600)
        project = self._project_path(job['project'])
        info = {'container': name, 'task_id': task_id, 'password': secrets.token_urlsafe(32),
                'model_proxy': proxy.info, 'project': str(project),
                'last_sync': 0, 'manifest': []}
        self._write_runtime(name, info)
        self._start_container(name, info, task_dir, job)
        session = _EndpointOpenCode(f"http://127.0.0.1:{info['port']}", info['password'])
        data = {'title': 'Hearth: '+job['title'], 'agent': AGENT,
                'model': job['model'], 'location': {'directory': '/workspace'},
                'permissions': self._permissions(job)}
        result = session.call('POST', '/api/session', data)['data']['id']
        self._sync_to_container(name, project)
        self._report(job, 'Project synced. The agent is ready to start its first work cycle.')
        return name+SESSION_SEPARATOR+result

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
        # The host project is the durable checkpoint, so create a fresh session in
        # the same per-assignment container and continue from those files.
        self.cleanup(job)
        recreated = self.create(job)
        self._report(job, 'Previous container was unavailable; resumed from the latest synchronized project files.')
        return recreated

    def _permissions(self, job):
        permissions = [{'action': '*', 'resource': '*', 'effect': 'ask'}]
        for action in ('read', 'glob', 'grep', 'edit', 'write', 'patch', 'todowrite', 'question'):
            permissions.append({'action': action, 'resource': '*', 'effect': 'allow'})
        permissions += [
            {'action': 'shell', 'resource': '*', 'effect': 'allow' if job.get('auto_commands') else 'ask'},
            {'action': 'external_directory', 'resource': '*', 'effect': 'ask'},
            {'action': 'doom_loop', 'resource': '*', 'effect': 'ask'},
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
        except RuntimeError:
            raise RuntimeError('The isolated agent container is missing. Stop this assignment and create a new one.')
        if running != 'true':
            self._docker('start', name, timeout=60)
        return _EndpointOpenCode(f"http://127.0.0.1:{info['port']}", info['password'])

    @staticmethod
    def workspace_directory(_client, _job):
        return '/workspace'

    def prompt(self, session, text, message_id):
        if SESSION_SEPARATOR not in str(session): return super().prompt(session, text, message_id)
        name, raw, info = self._split(session); return self._endpoint(name, info).prompt(raw, text, message_id)
    def snapshot(self, session):
        if SESSION_SEPARATOR not in str(session): return super().snapshot(session)
        name, raw, info = self._split(session); return self._endpoint(name, info).snapshot(raw)
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

    def _sync_to_container(self, name, project):
        root = self._project_path(project).resolve()
        process = subprocess.Popen(['docker', 'exec', '-i', name, 'tar', '-C', '/workspace', '-xf', '-'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        total = 0
        try:
            with tarfile.open(fileobj=process.stdin, mode='w|') as tar:
                for path in root.rglob('*'):
                    rel = path.relative_to(root)
                    if any(part in SKIP_PARTS for part in rel.parts) or path.is_symlink(): continue
                    if path.is_dir():
                        tar.add(path, arcname=rel.as_posix(), recursive=False)
                    elif path.is_file():
                        total += path.stat().st_size
                        if total > MAX_PROJECT_BYTES: raise RuntimeError('Project import exceeded the 2 GiB safety limit.')
                        tar.add(path, arcname=rel.as_posix(), recursive=False)
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
        tar_args = ['docker', 'exec', name, 'tar', '-C', '/workspace']
        for part in sorted(SKIP_PARTS):
            tar_args.append('--exclude='+part)
        tar_args.extend(['-cf', '-', '.'])
        process = subprocess.Popen(tar_args,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        manifest = set()
        total = 0
        root = self._project_path(job['project']).resolve()
        try:
            with tarfile.open(fileobj=process.stdout, mode='r|') as tar:
                for member in tar:
                    if not _safe_member(member.name): continue
                    rel = Path(*PurePosixPath(member.name).parts)
                    if any(part in SKIP_PARTS for part in rel.parts): continue
                    target = root/rel
                    if not _safe_target(root, target): continue
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        manifest.add(rel.as_posix()+'/')
                    elif member.isfile():
                        total += member.size
                        if total > MAX_PROJECT_BYTES: raise RuntimeError('Agent project export exceeded the 2 GiB safety limit.')
                        target.parent.mkdir(parents=True, exist_ok=True)
                        source = tar.extractfile(member)
                        if source is None: continue
                        fd, temporary_name = tempfile.mkstemp(prefix='.hearth-sync-', dir=target.parent)
                        temporary = Path(temporary_name)
                        with os.fdopen(fd, 'wb') as output:
                            shutil.copyfileobj(source, output, 1024*1024)
                        temporary.chmod(0o755 if member.mode & 0o111 else 0o644)
                        temporary.replace(target)
                        manifest.add(rel.as_posix())
            code = process.wait(timeout=300)
            error = process.stderr.read().decode('utf-8', 'replace')[-1000:]
            if code: raise RuntimeError('Could not synchronize the isolated project: '+error)
        except Exception:
            process.kill(); process.wait(timeout=5)
            raise
        old = set(info.get('manifest') or [])
        for stale in sorted(old-manifest,
                            key=lambda item: (item.rstrip('/').count('/'), item), reverse=True):
            is_dir = stale.endswith('/')
            path = root/Path(stale.rstrip('/'))
            try:
                if _safe_target(root, path):
                    if is_dir: path.rmdir()
                    elif path.is_file(): path.unlink()
            except (OSError, RuntimeError): pass
        info['manifest'] = sorted(manifest)
        info['last_sync'] = now
        self._write_runtime(name, info)

    def cleanup(self, job):
        task_id = job.get('id')
        if not task_id: return
        name = self._container_name(task_id)
        with self._lock:
            proxy = self._proxies.pop(name, None)
        if proxy: proxy.close()
        try: self._docker('rm', '--force', name, timeout=60)
        except Exception: pass
        shutil.rmtree(self.runtime_directory/task_id, ignore_errors=True)
        self._runtime_path(name).unlink(missing_ok=True)
