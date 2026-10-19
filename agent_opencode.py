"""Local OpenCode v2 adapter. Never expose service credentials to the browser."""
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urlsplit

import requests
import flm_context

AGENT = 'hearth-worker'
SYSTEM = '''You are Hearth's persistent project agent. Work toward the assigned goal until
its acceptance criteria are met. Read project instructions and relevant files before
editing. Maintain a plan, make focused changes, run verification, and repair failures.
Use actual tools for actions; do not stop at promises or claim unrun tests passed.
Respect user decisions and permissions. Do not contact other people or publish work
unless the task explicitly asks for it. Ask questions only when necessary, through
the question tool. Preserve unrelated work. When the whole goal is achieved, give a
concise result with actual verification evidence, then end with this exact line:
[HEARTH_TASK_COMPLETE]
Otherwise explain the remaining work and continue using tools.'''


class OpenCode:
    def start_service(self):
        subprocess.run([str(Path.home()/'.opencode/bin/opencode'), 'service', 'start'],
                       check=True, capture_output=True, timeout=30)

    def call(self, method, path, data=None, params=None, read_timeout=30):
        state_path = Path.home()/'.local/state/opencode/service.json'
        if not state_path.exists():
            self.start_service()
        state = json.loads(state_path.read_text())
        config = json.loads((Path.home()/'.config/opencode/service.json').read_text())
        base = state['url'].rstrip('/')
        if urlsplit(base).hostname not in ('127.0.0.1', 'localhost', '::1'):
            raise ValueError('OpenCode must run on this machine.')
        try:
            response = requests.request(method, base+path, json=data, params=params,
                auth=('opencode', config.get('password', '')), timeout=(5, read_timeout))
        except requests.ConnectionError:
            if method != 'GET':
                raise
            self.start_service()
            state = json.loads(state_path.read_text())
            base = state['url'].rstrip('/')
            if urlsplit(base).hostname not in ('127.0.0.1', 'localhost', '::1'):
                raise ValueError('OpenCode must run on this machine.')
            config = json.loads((Path.home()/'.config/opencode/service.json').read_text())
            response = requests.request(method, base+path, json=data, params=params,
                auth=('opencode', config.get('password', '')), timeout=(5, read_timeout))
        if not response.ok:
            raise RuntimeError(f'OpenCode {response.status_code}: {response.text[:400]}')
        return response.json() if response.content else {}

    def models(self):
        local = {'fastflowlm', 'lemonade', 'lmstudio', 'vllm', 'kobold', 'sglang'}
        models = []
        for x in self.call('GET', '/api/model')['data']:
            if x.get('providerID') not in local:
                continue
            context = flm_context.context_limit(x['id']) if x['providerID'] == 'fastflowlm' else (x.get('limit') or {}).get('context')
            usable = not isinstance(context, int) or context >= 16384
            models.append({'providerID': x['providerID'], 'id': x['id'], 'name': x.get('name', x['id']),
                'context': context, 'usable': usable,
                'reason': '' if usable else 'This OpenCode worker needs at least 16K context; this model has '+str(context)+'.'})
        return models


    def ensure_agent(self):
        if any(x['id'] == AGENT for x in self.call('GET', '/api/agent')['data']):
            return
        path = Path.home()/'.config/opencode/opencode.jsonc'
        # JSONC comments and trailing commas occur in hand-edited OpenCode configs.
        raw = path.read_text()
        raw = re.sub(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/',
                     lambda m: m.group(1) or '', raw, flags=re.S)
        raw = re.sub(r'("(?:\\.|[^"\\])*")|,([\s]*[}\]])',
                     lambda m: m.group(1) or m.group(2), raw)
        data = json.loads(raw)
        agents = data.setdefault('agents', {})
        if AGENT not in agents:
            backup = path.with_name(path.name+'.before-hearth-agent')
            if not backup.exists():
                shutil.copy2(path, backup)
            agents[AGENT] = {'description': 'Persistent Hearth project worker', 'mode': 'primary',
                             'system': SYSTEM, 'steps': 40}
            temporary = path.with_name(path.name+'.hearth.tmp')
            temporary.write_text(json.dumps(data, indent=2)+'\n')
            os.chmod(temporary, path.stat().st_mode & 0o777)
            temporary.replace(path)
            subprocess.run([str(Path.home()/'.opencode/bin/opencode'), 'reload'],
                           check=True, capture_output=True, timeout=30)
        if not any(x['id'] == AGENT for x in self.call('GET', '/api/agent')['data']):
            raise RuntimeError('OpenCode did not load the Hearth worker agent. Reload its configuration.')

    def create(self, job):
        permissions = [{'action': '*', 'resource': '*', 'effect': 'ask'}]
        for action in ('read', 'glob', 'grep', 'edit', 'write', 'patch', 'todowrite', 'question'):
            permissions.append({'action': action, 'resource': '*', 'effect': 'allow'})
        permissions += [
            {'action': 'bash', 'resource': '*', 'effect': 'allow' if job['auto_commands'] else 'ask'},
            {'action': 'external_directory', 'resource': '*', 'effect': 'ask'},
            {'action': 'doom_loop', 'resource': '*', 'effect': 'ask'},
            {'action': 'task', 'resource': '*', 'effect': 'deny'}]
        return self.call('POST', '/api/session', {'title': 'Hearth: '+job['title'], 'agent': AGENT,
            'model': job['model'], 'location': {'directory': job['project']}, 'permissions': permissions})['data']['id']

    def prompt(self, session, text, message_id):
        try:
            return self.call('POST', f'/api/session/{session}/prompt', {'text': text, 'id': message_id})
        except RuntimeError as exc:
            # The input might already have been durably admitted before a network/restart
            # failure. Reusing its ID never schedules a duplicate continuation.
            try:
                self.call('GET', f'/api/session/{session}/message/{message_id}')
                return
            except RuntimeError:
                pending = self.call('GET', f'/api/session/{session}/inbox')['data']
                if any(x.get('id') == message_id for x in pending):
                    return
            raise exc

    def snapshot(self, session):
        path = '/api/session/'+session
        return {'active': session in self.call('GET', '/api/session/active')['data'],
                'forms': self.call('GET', path+'/form')['data'],
                'permissions': self.call('GET', path+'/permission')['data'],
                'inbox': self.call('GET', path+'/inbox')['data'],
                'messages': self.call('GET', path+'/message', params={'limit': '50', 'order': 'desc'})['data']}

    def interrupt(self, session):
        self.call('POST', f'/api/session/{session}/interrupt', params={'resume': 'false'})

    def reply(self, session, kind, item, value):
        if kind == 'form':
            self.call('POST', f'/api/session/{session}/form/{item}/reply', {'answer': value})
        elif kind == 'dismiss':
            self.call('DELETE', f'/api/session/{session}/form/{item}')
        else:
            self.call('POST', f'/api/session/{session}/permission/{item}/reply', {'decision': value})

    def message(self, session, message):
        result = self.call('GET', f'/api/session/{session}/message/{message}')['data']
        if result.get('type') != 'shell':
            return result
        # Session.shell's HTTP waiter can disconnect while the durable process keeps
        # running. Read the process itself rather than trusting that waiter's projection.
        directory = self.call('GET', '/api/session/'+session)['data']['location']['directory']
        scope = {'location[directory]': directory}
        try:
            process = self.call('GET', '/api/shell/'+result['shellID'], params=scope)['data']
        except RuntimeError as exc:
            if 'OpenCode 404:' not in str(exc):
                raise
            if result.get('status') == 'running':
                result.update(status='killed', exit=None)
            return result
        output = self.call('GET', '/api/shell/'+result['shellID']+'/output', params={**scope, 'limit': '8192'})['data']
        if output['size'] > 8192:
            output = self.call('GET', '/api/shell/'+result['shellID']+'/output',
                params={**scope, 'cursor': str(output['size']-8192), 'limit': '8192'})['data']
        result.update(status=process['status'], exit=process.get('exit'), output=output)
        return result

    def verify(self, session, command, message_id):
        try:
            self.call('POST', f'/api/session/{session}/shell', {'command': command, 'id': message_id}, read_timeout=1)
        except (RuntimeError, requests.RequestException):
            # Admission is durable; the shell keeps running after the HTTP waiter
            # disconnects. The supervisor polls/interrupts its native process record.
            self.message(session, message_id)

    def stop_verification(self, session, message_id):
        try:
            message = self.message(session, message_id)
        except RuntimeError as exc:
            if 'OpenCode 404:' in str(exc):
                return
            raise
        if message.get('status') == 'running':
            directory = self.call('GET', '/api/session/'+session)['data']['location']['directory']
            self.call('DELETE', '/api/shell/'+message['shellID'], params={'location[directory]': directory})
