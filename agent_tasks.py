"""Durable, single-lane supervision of real OpenCode project sessions."""
import copy
import fcntl
import hashlib
import io
import json
import logging
from pathlib import Path
import tarfile
import threading
import time
import uuid

from agent_opencode import OpenCode, SYSTEM

WORKING = {'queued', 'running', 'waiting_input', 'verifying'}
ACTIVE = WORKING | {'pausing', 'stopping'}
MARKER = '[HEARTH_TASK_COMPLETE]'


def text_of(message):
    return '\n'.join(p.get('text', '') for p in message.get('content', []) if p.get('type') == 'text')


def fingerprint(message):
    parts = [{'name': p.get('name'), 'state': p.get('state')} for p in message.get('content', []) if p.get('type') == 'tool']
    return hashlib.sha256(json.dumps([text_of(message), parts], sort_keys=True).encode()).hexdigest()


class Manager:
    def __init__(self, directory, client=None, prepare_model=None):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self.client = client or OpenCode()
        self.prepare_model = prepare_model or (lambda job: None)
        self.lock = threading.RLock()
        self.dispatch_lock = threading.RLock()
        self.jobs = {}
        self.thread = None
        self.lease = None
        for path in self.directory.glob('*.json'):
            try:
                job = json.loads(path.read_text())
                self.jobs[job['id']] = job
            except (ValueError, KeyError, OSError):
                pass

    def save(self, job):
        job['updated_at'] = time.time()
        path = self.directory/(job['id']+'.json')
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(job, indent=2))
        temporary.chmod(0o600)
        temporary.replace(path)

    def note(self, job, text):
        job['activity'] = (job.get('activity', [])+[{'time': time.time(), 'text': text}])[-200:]

    def list(self):
        with self.lock:
            return copy.deepcopy(sorted(self.jobs.values(), key=lambda x: x['created_at'], reverse=True))

    def get(self, key):
        with self.lock:
            return copy.deepcopy(self.jobs.get(key))

    def busy(self):
        with self.lock:
            return any(j['status'] in ACTIVE for j in self.jobs.values())

    def delete(self, key):
        with self.dispatch_lock:
            with self.lock:
                job = self.jobs[key]
                if job['status'] in ACTIVE:
                    raise ValueError('Stop or pause the assignment before deleting it.')
                snapshot = copy.deepcopy(job)
            with self.lock:
                del self.jobs[key]
                (self.directory/(key+'.json')).unlink(missing_ok=True)
        return snapshot

    def download(self, key):
        job = self.get(key)
        if not job:
            raise KeyError(key)
        if job['status'] != 'completed':
            raise ValueError('Project download is available after the assignment completes.')
        project = Path(job['project']).resolve()
        if not project.is_dir():
            raise ValueError('The completed project folder is no longer available.')
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w:gz') as archive:
            for path in project.rglob('*'):
                if any(part in ('.git', 'node_modules', '.venv', 'venv', '__pycache__') for part in path.parts):
                    continue
                if path.is_file() and not path.is_symlink():
                    archive.add(path, arcname=str(Path(project.name)/path.relative_to(project)), recursive=False)
        output.seek(0)
        return output, project.name

    def create(self, values, owner):
        title, goal = str(values.get('title', '')).strip(), str(values.get('goal', '')).strip()
        if 'project' in values or 'create_project' in values:
            raise ValueError('Project path is managed by the server and cannot be supplied.')
        project = Path.home() / 'agents' / 'project'
        if not title or len(title) > 120 or not goal or len(goal) > 20000:
            raise ValueError('Enter a title (up to 120 characters) and a goal (up to 20,000 characters).')
        if project.exists() and not project.is_dir():
            raise ValueError('The configured agent project path exists and is not a directory.')
        model = values.get('model')
        if not isinstance(model, dict) or not any(model == {'providerID': m['providerID'], 'id': m['id']} for m in self.client.models() if m.get('usable', True)):
            raise ValueError('Choose a configured local OpenCode model with at least 16K context for this worker.')
        cycles = values.get('max_cycles', 50)
        timeout = values.get('verification_timeout', 300)
        if type(cycles) is not int or not 1 <= cycles <= 1000 or type(timeout) is not int or not 10 <= timeout <= 3600:
            raise ValueError('Cycle budget must be 1–1000 and verification timeout 10–3600 seconds.')
        criteria = str(values.get('criteria', '')).strip()
        command = str(values.get('verification_command', '')).strip()
        assignee = str(values.get('assignee', 'Project agent')).strip()
        if len(criteria) > 10000 or len(command) > 4000 or not assignee or len(assignee) > 120:
            raise ValueError('Acceptance criteria, verification command or agent name is too long.')
        shell_mode = values.get('shell_mode', 'auto' if values.get('auto_commands') is True else 'ask')
        if shell_mode not in ('ask', 'auto'):
            raise ValueError('Choose whether shell commands should ask or run automatically.')
        self.client.ensure_agent()
        with self.lock:
            if sum(j['status'] in ACTIVE for j in self.jobs.values()) >= 20:
                raise ValueError('The agent queue already has 20 active assignments.')
            project.mkdir(parents=True, exist_ok=True)
            job = {'id': uuid.uuid4().hex, 'owner': owner, 'title': title, 'goal': goal,
                'project': str(project.resolve()), 'assignee': assignee, 'model': model,
                'criteria': criteria, 'verification_command': command, 'verification_timeout': timeout,
                'auto_commands': shell_mode == 'auto', 'shell_mode': shell_mode, 'max_cycles': cycles,
                'cycles': 0, 'status': 'queued', 'session_id': None, 'created_at': time.time(),
                'activity': [], 'messages': [], 'forms': [], 'permissions': [], 'error': None,
                'pending_prompt': None, 'handled_message': None, 'errors': 0, 'stalls': 0}
            self.note(job, 'Assignment queued. Files may be edited in the selected project.')
            self.jobs[job['id']] = job
            self.save(job)
            return copy.deepcopy(job)

    def control(self, key, action):
        if action not in ('pause', 'resume', 'stop'):
            raise ValueError('Unknown agent control.')
        with self.dispatch_lock:
            with self.lock:
                job = self.jobs[key]
                if action == 'resume':
                    if job['status'] not in ('paused', 'blocked'):
                        raise ValueError('Only paused or blocked assignments can resume.')
                    if job['cycles'] >= job['max_cycles']:
                        job['max_cycles'] += 50
                    job.update(status='queued', errors=0, stalls=0, error=None, next_retry=0,
                               awaiting_since=0, idle_polls=0, model_prepared=False)
                elif job['status'] == 'completed':
                    raise ValueError('This assignment is already completed.')
                else:
                    job.pop('after_interrupt', None)
                    job['status'] = ('pausing' if action == 'pause' else 'stopping') if job['session_id'] else ('paused' if action == 'pause' else 'stopped')
                self.note(job, 'User requested '+action+'.')
                self.save(job)
                sid, verification = job['session_id'], job.get('verification_id')
            if action != 'resume' and sid:
                try:
                    self._interrupt(job)
                except Exception as exc:
                    with self.lock:
                        self.note(job, 'Interruption is pending: '+str(exc)[:400])
                        job['error'] = 'Waiting for OpenCode to acknowledge interruption; execution may still be active.'
                        self.save(job)
            return self.get(key)

    def _interrupt(self, job):
        self.client.interrupt(job['session_id'])
        if job.get('verification_id'):
            self.client.stop_verification(job['session_id'], job['verification_id'])
        with self.lock:
            if job['status'] in ('pausing', 'stopping'):
                target = job.pop('after_interrupt', 'paused' if job['status'] == 'pausing' else 'stopped')
                job.update(status=target, error=job.get('error') if target == 'blocked' else None)
                self.save(job)

    def reply(self, key, kind, item, value):
        if kind not in ('form', 'dismiss', 'permission'):
            raise ValueError('Unknown reply type.')
        with self.dispatch_lock:
            with self.lock:
                job = self.jobs[key]
                if job['status'] != 'waiting_input':
                    raise ValueError('This assignment is not waiting for input.')
                pending = job['permissions'] if kind == 'permission' else job['forms']
                if not any(x['id'] == item for x in pending):
                    raise ValueError('This prompt no longer belongs to the assignment.')
                if kind == 'permission' and value not in ('once', 'always', 'reject'):
                    raise ValueError('Choose allow once, allow always, or reject.')
                if kind == 'form' and not isinstance(value, dict):
                    raise ValueError('Form answers must be a field/value object.')
                sid = job['session_id']
            self.client.reply(sid, kind, item, value)
            with self.lock:
                job.update(status='running', forms=[], permissions=[], errors=0, idle_polls=0)
                self.note(job, 'User answered a question or permission request.')
                self.save(job)
        return self.get(key)

    def steer(self, key, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 10000:
            raise ValueError('Enter additional instructions up to 10,000 characters.')
        with self.lock:
            job = self.jobs[key]
            if job['status'] != 'running' or not job['cycles']:
                raise ValueError('Resume the agent or answer its pending prompt before adding instructions.')
            pending = job.setdefault('steering', [])
            if len(pending) >= 20:
                raise ValueError('The agent already has 20 queued instructions.')
            if sum(len(x) for x in job.get('instructions', []))+len(text.strip()) > 10000:
                raise ValueError('This assignment has 10,000 characters of refinements. Start a new assignment with a consolidated goal.')
            pending.append({'id': 'msg_'+uuid.uuid4().hex, 'text': text.strip()})
            job['instructions'] = job.get('instructions', [])+[text.strip()]
            self.note(job, 'Additional user instructions: '+text.strip())
            self.save(job)
            return copy.deepcopy(job)

    def _send(self, job, text):
        with self.dispatch_lock:
            with self.lock:
                if job['status'] not in WORKING:
                    return
                if job['cycles'] >= job['max_cycles']:
                    job.update(status='blocked', error='Cycle budget reached. Resume to grant 50 more cycles.')
                    self.save(job)
                    return
                if not job.get('pending_prompt'):
                    scope = 'Assignment goal:\n'+job['goal']+'\nAcceptance criteria:\n'+(job['criteria'] or 'Complete and verify the requested goal.')
                    refinements = job.get('instructions', [])
                    if refinements:
                        scope += '\nUser refinements (latest takes precedence):\n'+'\n'.join(refinements)
                    job['pending_prompt'] = {'id': 'msg_'+uuid.uuid4().hex, 'text': scope+'\n\n'+text}
                    self.save(job)
                pending = copy.deepcopy(job['pending_prompt'])
            self.client.prompt(job['session_id'], pending['text'], pending['id'])
            with self.lock:
                job.update(pending_prompt=None, status='running', errors=0, error=None, next_retry=0, idle_polls=0,
                           awaiting_since=time.time(), cycles=job['cycles']+1)
                self.note(job, f'Agent work cycle {job["cycles"]} dispatched.')
                self.save(job)

    def _verify(self, job):
        with self.dispatch_lock:
            with self.lock:
                if job['status'] not in WORKING:
                    return
                if not job.get('verification_id'):
                    job.update(verification_id='msg_'+uuid.uuid4().hex, verification_started=time.time(),
                               verification_admitted=False, status='verifying')
                    self.note(job, 'Running the assigned verification command independently of the model.')
                    self.save(job)
                sid, mid = job['session_id'], job['verification_id']
            if not job.get('verification_admitted'):
                self.client.verify(sid, job['verification_command'], mid)
                with self.lock:
                    job['verification_admitted'] = True
                    self.save(job)
        message = self.client.message(sid, mid)
        with self.lock:
            if job['status'] not in WORKING:
                return
            if message.get('status') == 'running':
                if time.time()-job['verification_started'] > job['verification_timeout']:
                    self.client.stop_verification(sid, mid)
                    job.update(status='blocked', error='Verification timed out. Inspect it and resume when ready.')
                    self.save(job)
                return
            result = {'command': job['verification_command'], 'status': message.get('status'),
                      'exit': message.get('exit'), 'output': (message.get('output') or {}).get('output', '')[-8000:]}
            job['verification'] = result
            job.update(verification_id=None, verification_admitted=False)
            if result['status'] == 'exited' and type(result['exit']) in (int, float) and result['exit'] == 0:
                job.update(status='completed', completion='Verified by the assigned command.', finished_at=time.time())
                self.note(job, 'Goal complete; verification command passed.')
                self.save(job)
                return
            self.note(job, 'Verification failed; sending the result back for repair.')
            self.save(job)
        self._send(job, 'The completion claim failed independent verification. Fix the remaining issue and verify again.\n'+json.dumps(result))

    def tick(self):
        with self.lock:
            job = next((j for j in sorted(self.jobs.values(), key=lambda x: (x['status'] == 'queued', x['created_at'])) if j['status'] in ACTIVE), None)
            if not job or time.time() < job.get('next_retry', 0):
                return
        try:
            if job['status'] in ('pausing', 'stopping'):
                with self.dispatch_lock:
                    self._interrupt(job)
                return
            if not job.get('model_prepared'):
                with self.dispatch_lock:
                    if job['status'] not in WORKING:
                        return
                    self.prepare_model(job)
                    with self.lock:
                        job['model_prepared'] = True
                        self.save(job)
            if not job['session_id']:
                with self.dispatch_lock:
                    if job['status'] not in WORKING:
                        return
                    sid = self.client.create(job)
                    with self.lock:
                        job['session_id'] = sid
                        self.save(job)
            if job.get('steering'):
                with self.dispatch_lock:
                    if job['status'] not in WORKING:
                        return
                    instruction = job['steering'][0]
                    self.client.prompt(job['session_id'], 'Additional instructions for this assignment:\n'+instruction['text'], instruction['id'])
                    with self.lock:
                        job['steering'].pop(0)
                        self.save(job)
                return
            if job.get('verification_id'):
                self._verify(job)
                return
            if job.get('pending_prompt'):
                self._send(job, job['pending_prompt']['text'])
                return
            snap = self.client.snapshot(job['session_id'])
            with self.lock:
                if job['status'] not in WORKING:
                    return
                # Bound the UI/persistence copy, while the complete history stays in OpenCode.
                recent = []
                for message in reversed(snap['messages'][:50]):
                    parts = []
                    for part in message.get('content', []):
                        if part.get('type') == 'text':
                            parts.append({'type': 'text', 'text': part.get('text', '')[-8000:]})
                        elif part.get('type') == 'tool':
                            parts.append({'type': 'tool', 'name': part.get('name'),
                                'status': (part.get('state') or {}).get('status'),
                                'input': json.dumps((part.get('state') or {}).get('input') or {})[:2000]})
                    recent.append({'id': message['id'], 'type': message.get('type'), 'content': parts})
                job.update(messages=recent, forms=snap['forms'], permissions=snap['permissions'])
                if snap['forms'] or snap['permissions']:
                    job['status'] = 'waiting_input'
                    self.save(job)
                    return
                if snap['active'] or snap['inbox']:
                    job.update(status='running', idle_polls=0)
                    self.save(job)
                    return
                if job['cycles'] == 0:
                    self.save(job)
                    initial = True
                else:
                    initial = False
                    latest = next((m for m in snap['messages'] if m.get('type') == 'assistant'), None)
                    if not latest or latest['id'] == job.get('handled_message'):
                        if job['status'] == 'queued':
                            pass
                        elif time.time()-job.get('awaiting_since', time.time()) < 180:
                            return
                        else:
                            raise RuntimeError('OpenCode became idle without a new assistant result.')
                    else:
                        job['idle_polls'] = job.get('idle_polls', 0)+1
                        if job['idle_polls'] < 2:
                            self.save(job)
                            return
                        if latest.get('error'):
                            raise RuntimeError(str(latest['error']))
                        job['handled_message'] = latest['id']
                        fp = fingerprint(latest)
                        job['stalls'] = job['stalls']+1 if fp == job.get('fingerprint') else 0
                        job['fingerprint'] = fp
                        text = text_of(latest).strip()
                        job['summary'] = text[-8000:]
                        if latest.get('finish') == 'stop' and text.splitlines() and text.splitlines()[-1].strip() == MARKER:
                            if job['verification_command']:
                                job['status'] = 'verifying'
                            else:
                                job.update(status='completed', completion='Agent-reported completion; no independent command was supplied.', finished_at=time.time())
                                self.note(job, job['completion'])
                            self.save(job)
                            if job['status'] == 'completed':
                                return
                        elif job['stalls'] >= 2:
                            job.update(status='blocked', error='The agent repeated the same result three times. Review its blocker and resume.')
                            self.save(job)
                            return
                    self.save(job)
            if job['status'] == 'verifying':
                self._verify(job)
            elif initial:
                self._send(job, SYSTEM+'\n\nAssigned agent: '+job['assignee']+'\nProject directory: '+job['project']+'\nVerification command: '+(job['verification_command'] or 'Choose appropriate verification and report evidence.'))
            else:
                self._send(job, 'Continue the assigned goal. Inspect the current state and remaining todos, implement the next step, and verify. '
                    'Do not repeat completed work or answered questions. If a concrete blocker needs user input, ask through the question tool. '
                    'End with '+MARKER+' only when the entire goal and acceptance criteria are achieved.')
        except Exception as exc:
            with self.lock:
                if job['status'] not in ACTIVE:
                    return
                job['errors'] += 1
                job['error'] = str(exc)[:1200]
                self.note(job, 'Worker error: '+job['error'])
                if job['errors'] >= 3 and job['status'] not in ('pausing', 'stopping'):
                    if job['session_id']:
                        job.update(status='stopping', after_interrupt='blocked')
                    else:
                        job['status'] = 'blocked'
                else:
                    job['next_retry'] = time.time()+min(60, 5*2**min(job['errors'], 4))
                self.save(job)
            if job.get('after_interrupt') == 'blocked':
                try:
                    self._interrupt(job)
                except Exception:
                    pass

    def start_worker(self, available=lambda: True):
        if self.thread and self.thread.is_alive():
            return
        lease = (self.directory/'worker.lock').open('a')
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lease.close()
            return
        self.lease = lease
        def work():
            while True:
                try:
                    if available():
                        self.tick()
                except Exception:
                    logging.exception('Persistent agent supervisor could not update its state')
                time.sleep(2)
        self.thread = threading.Thread(target=work, daemon=True, name='hearth-agent-supervisor')
        self.thread.start()
