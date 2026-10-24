"""Durable, single-lane supervision of real OpenCode project sessions."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import logging
from pathlib import Path
import shutil
import tarfile
import threading
import time
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent_opencode import OpenCode, SYSTEM
import agent_store
import database

WORKING = {'queued', 'running', 'waiting_input', 'verifying'}
ACTIVE = WORKING | {'pausing', 'stopping'}
RESUMABLE = {'paused', 'blocked', 'interrupted'}
MARKER = '[HEARTH_TASK_COMPLETE]'


def text_of(message):
    return '\n'.join(p.get('text', '') for p in message.get('content', []) if p.get('type') == 'text')


def fingerprint(message):
    parts = [{'name': p.get('name'), 'state': p.get('state')} for p in message.get('content', []) if p.get('type') == 'tool']
    return hashlib.sha256(json.dumps([text_of(message), parts], sort_keys=True).encode()).hexdigest()


class Manager:
    def __init__(self, directory, client=None, prepare_model=None):
        self.directory = Path(directory)
        self.client = client or OpenCode()
        self.prepare_model = prepare_model or (lambda job: None)
        self.lock = threading.RLock()
        self.dispatch_lock = threading.RLock()
        self.jobs = {job['id']: job for job in agent_store.list_tasks()}
        self.thread = None
        self.lease = None
        legacy_projects = set()
        for path in self.directory.glob('*.json'):
            try:
                job = json.loads(path.read_text())
                project = Path(job.get('project', ''))
                agents_root = Path.home().resolve()/'agents'
                if not agent_store.user_exists(job.get('owner')):
                    path.unlink(missing_ok=True)
                    if project.is_relative_to(agents_root) and project.is_dir() and not project.is_symlink():
                        legacy_projects.add(project)
                    continue
                if project.is_relative_to(agents_root) and project.is_dir():
                    if agent_store.import_legacy_workspace(job['owner'], project):
                        legacy_projects.add(project)
                job['project'] = 'PostgreSQL private workspace'
                if not agent_store.get_task(job['id']):
                    agent_store.save_task(job)
                path.unlink(missing_ok=True)
            except (ValueError, KeyError, OSError):
                pass
        agents_root = Path.home().resolve()/'agents'
        for owner in agent_store.list_user_ids():
            owner_key = hashlib.sha256(str(owner).encode()).hexdigest()[:24]
            project = agents_root/'users'/owner_key/'project'
            if project.is_dir() and not project.is_symlink():
                if agent_store.import_legacy_workspace(owner, project):
                    legacy_projects.add(project)
        shared_legacy_project = agents_root/'project'
        if shared_legacy_project.is_dir() and not shared_legacy_project.is_symlink():
            task_owners = {job['owner'] for job in agent_store.list_tasks()}
            if len(task_owners) == 1:
                owner = next(iter(task_owners))
                if agent_store.import_legacy_workspace(owner, shared_legacy_project):
                    legacy_projects.add(shared_legacy_project)
            else:
                logging.error('Legacy shared agent workspace was left unmigrated because its owner cannot be determined safely.')
        self.jobs = {job['id']: job for job in agent_store.list_tasks()}
        for job in self.jobs.values():
            if job.get('project') != 'PostgreSQL private workspace':
                job['project'] = 'PostgreSQL private workspace'
                agent_store.save_task(job)
        for project in legacy_projects:
            try: shutil.rmtree(project)
            except OSError: logging.exception('Could not remove migrated legacy agent project files')
        (self.directory/'worker.lock').unlink(missing_ok=True)
        try: self.directory.rmdir()
        except OSError: pass
        # The supervisor is single-process. Any persisted active state belongs to a
        # worker that was interrupted by a Hearth restart or an unclean shutdown.
        with self.lock:
            for job in self.jobs.values():
                if job.get('status') in ACTIVE:
                    job.update(status='interrupted', resume_session=bool(job.get('session_id')),
                               error='Hearth restarted while this assignment was active. Resume to continue.')
                    self.note(job, 'Assignment interrupted by Hearth restart; it can be resumed.')
                    self.save(job)

    def save(self, job):
        job['updated_at'] = time.time()
        agent_store.save_task(job)

    def note(self, job, text):
        job['activity'] = (job.get('activity', [])+[{'time': time.time(), 'text': text}])[-200:]

    def report_progress(self, job, text):
        with self.lock:
            current = self.jobs.get(job.get('id'))
            if current:
                self.note(current, text)
                self.save(current)

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
            cleanup = getattr(type(self.client), 'cleanup', None)
            if cleanup:
                cleanup(self.client, snapshot)
            with self.lock:
                del self.jobs[key]
                agent_store.delete_task(key)
        return snapshot

    def delete_owner(self, owner_id):
        """Remove every assignment/container before the owning account is deleted."""
        with self.dispatch_lock:
            owned = [self.get(job['id']) for job in self.list() if job.get('owner') == owner_id]
            cleanup = getattr(type(self.client), 'cleanup', None)
            for job in owned:
                if cleanup:
                    cleanup(self.client, job)
                with self.lock:
                    self.jobs.pop(job['id'], None)
                    agent_store.delete_task(job['id'])

    def download(self, key):
        job = self.get(key)
        if not job:
            raise KeyError(key)
        if job['status'] != 'completed':
            raise ValueError('Project download is available after the assignment completes.')
        files = agent_store.list_workspace_files(job['owner'])
        if not files:
            raise ValueError('The completed project folder is no longer available.')
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w:gz') as archive:
            for item in files:
                path = Path(item['path'])
                if item['is_dir'] or any(part in ('.git', 'node_modules', '.venv', 'venv', '__pycache__') for part in path.parts):
                    continue
                info = tarfile.TarInfo(str(Path('project')/path))
                info.size = len(item['content'])
                info.mtime = item['updated_at']
                archive.addfile(info, io.BytesIO(item['content']))
        output.seek(0)
        return output, 'project'

    def _normalize_assignment(self, values):
        title, goal = str(values.get('title', '')).strip(), str(values.get('goal', '')).strip()
        if 'project' in values or 'create_project' in values:
            raise ValueError('Project path is managed by the server and cannot be supplied.')
        if not title or len(title) > 120 or not goal or len(goal) > 20000:
            raise ValueError('Enter a title (up to 120 characters) and a goal (up to 20,000 characters).')
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
        return {'title': title, 'goal': goal, 'model': model, 'max_cycles': cycles,
                'verification_timeout': timeout, 'criteria': criteria,
                'verification_command': command, 'assignee': assignee, 'shell_mode': shell_mode}

    def create(self, values, owner):
        assignment = self._normalize_assignment(values)
        self.client.ensure_agent()
        with self.lock:
            if sum(j['status'] in ACTIVE for j in self.jobs.values()) >= 20:
                raise ValueError('The agent queue already has 20 active assignments.')
            job = {'id': uuid.uuid4().hex, 'owner': owner, **assignment,
                'project': 'PostgreSQL private workspace', 'auto_commands': assignment['shell_mode'] == 'auto',
                'cycles': 0, 'status': 'queued', 'session_id': None, 'created_at': time.time(),
                'activity': [], 'messages': [], 'forms': [], 'permissions': [], 'error': None,
                'pending_prompt': None, 'handled_message': None, 'errors': 0, 'stalls': 0}
            self.note(job, 'Assignment queued. Project files are stored in this account’s private PostgreSQL workspace.')
            self.jobs[job['id']] = job
            self.save(job)
            return copy.deepcopy(job)

    def create_schedule(self, values, owner):
        interval = values.get('interval_seconds')
        if type(interval) is not int or not 300 <= interval <= 31536000:
            raise ValueError('Schedule interval must be between 5 minutes and 365 days.')
        unit = values.get('interval_unit')
        value = values.get('interval_value')
        unit_seconds = {'minutes': 60, 'hours': 3600, 'days': 86400, 'weeks': 604800}
        if unit not in unit_seconds or type(value) is not int or value < 1 or value*unit_seconds[unit] != interval:
            raise ValueError('Choose a valid repeat interval.')
        timezone_name = str(values.get('timezone', 'UTC')).strip() or 'UTC'
        try:
            schedule_timezone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('Choose a valid timezone.')
        run_time = str(values.get('run_time', '')).strip()
        if run_time:
            try:
                hour, minute = (int(part) for part in run_time.split(':', 1))
                if len(run_time.split(':', 1)) != 2 or not (0 <= hour <= 23 and 0 <= minute <= 59):
                    raise ValueError
            except ValueError:
                raise ValueError('Choose a valid local run time.')
            if unit not in {'days', 'weeks'}:
                raise ValueError('A run time of day requires a daily or weekly interval.')
            now_local = datetime.now(schedule_timezone)
            next_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if next_local <= now_local:
                next_local += timedelta(days=value * (7 if unit == 'weeks' else 1))
            first_run = next_local.timestamp()
        else:
            first_run = time.time() + interval
        comment = str(values.get('comment', '')).strip()
        if len(comment) > 10000:
            raise ValueError('Schedule comments can be at most 10,000 characters.')
        assignment = self._normalize_assignment(values)
        schedule = {'id': uuid.uuid4().hex, 'owner': owner, 'assignment': assignment,
                    'comment': comment, 'interval_seconds': interval, 'interval_value': value,
                    'interval_unit': unit, 'run_time': run_time or None,
                    'timezone': timezone_name, 'enabled': True,
                    'created_at': time.time(), 'next_run_at': first_run,
                    'last_run_at': None, 'last_task_id': None, 'last_error': None}
        agent_store.save_schedule(schedule)
        return schedule

    def list_schedules(self, owner):
        return agent_store.list_schedules(owner)

    def set_schedule_enabled(self, schedule_id, owner, enabled):
        schedule = next((x for x in agent_store.list_schedules(owner) if x['id'] == schedule_id), None)
        if not schedule:
            return None
        schedule['enabled'] = enabled
        if enabled and schedule['next_run_at'] <= time.time():
            if schedule.get('run_time') and schedule.get('timezone'):
                zone = ZoneInfo(schedule['timezone'])
                hour, minute = (int(part) for part in schedule['run_time'].split(':', 1))
                now_local = datetime.now(zone)
                next_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if next_local.timestamp() <= time.time():
                    next_local += timedelta(days=schedule['interval_value'] * (7 if schedule['interval_unit'] == 'weeks' else 1))
                schedule['next_run_at'] = next_local.timestamp()
            else:
                schedule['next_run_at'] = time.time()+schedule['interval_seconds']
        agent_store.save_schedule(schedule)
        return schedule

    def delete_schedule(self, schedule_id, owner):
        return agent_store.delete_schedule(schedule_id, owner)

    def _run_due_schedule(self):
        due = agent_store.list_due_schedules(time.time(), limit=1)
        if not due:
            return
        schedule = due[0]
        now = time.time()
        scheduled_at = schedule['next_run_at']
        if schedule.get('run_time') and schedule.get('timezone') and schedule.get('interval_unit') in {'days', 'weeks'}:
            zone = ZoneInfo(schedule['timezone'])
            hour, minute = (int(part) for part in schedule['run_time'].split(':', 1))
            next_local = datetime.fromtimestamp(schedule['next_run_at'], zone)
            while next_local.timestamp() <= now:
                next_local += timedelta(days=schedule['interval_value'] * (7 if schedule['interval_unit'] == 'weeks' else 1))
            schedule['next_run_at'] = next_local.replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp()
        else:
            while schedule['next_run_at'] <= now:
                schedule['next_run_at'] += schedule['interval_seconds']
        # Claim this occurrence before creating the task so a service crash cannot
        # create the same scheduled assignment twice on restart.
        agent_store.save_schedule(schedule)
        if not agent_store.user_exists(schedule['owner']):
            schedule['enabled'] = False
            schedule['last_error'] = 'Owner account no longer exists.'
            agent_store.save_schedule(schedule)
            return
        if not agent_store.user_agent_access_enabled(schedule['owner']):
            schedule['last_error'] = 'Owner’s Agent access is disabled; this scheduled run was skipped.'
            agent_store.save_schedule(schedule)
            return
        previous = self.get(schedule.get('last_task_id')) if schedule.get('last_task_id') else None
        if previous and previous.get('status') not in ('completed', 'stopped'):
            schedule['last_error'] = 'Run skipped because the previous scheduled assignment is still active.'
            agent_store.save_schedule(schedule)
            return
        try:
            assignment = dict(schedule['assignment'])
            if schedule.get('comment'):
                assignment['goal'] += '\n\nScheduled agent comment:\n'+schedule['comment']
            assignment['goal'] += '\n\nScheduled occurrence (UTC): '+datetime.fromtimestamp(scheduled_at, timezone.utc).isoformat()
            job = self.create(assignment, schedule['owner'])
            job['schedule_id'] = schedule['id']
            job['scheduled_run_at'] = scheduled_at
            with self.lock:
                self.jobs[job['id']].update(schedule_id=schedule['id'], scheduled_run_at=scheduled_at)
                self.save(self.jobs[job['id']])
            schedule['last_task_id'] = job['id']
            schedule['last_run_at'] = now
            schedule['last_error'] = None
        except Exception as exc:
            schedule['last_error'] = str(exc)[:1000]
            logging.exception('Could not start scheduled agent assignment %s', schedule['id'])
        agent_store.save_schedule(schedule)

    def control(self, key, action):
        if action not in ('pause', 'resume', 'stop'):
            raise ValueError('Unknown agent control.')
        with self.dispatch_lock:
            with self.lock:
                job = self.jobs[key]
                if action == 'resume':
                    if job['status'] not in RESUMABLE:
                        raise ValueError('Only paused, blocked, or interrupted assignments can resume.')
                    if job['cycles'] >= job['max_cycles']:
                        job['max_cycles'] += 50
                    job.update(status='queued', errors=0, stalls=0, error=None, next_retry=0,
                               awaiting_since=0, idle_polls=0, model_prepared=False)
                    if job.get('session_id'):
                        job['resume_session'] = True
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
            result = self.get(key)
            if action == 'stop' and result:
                if result['status'] != 'stopped':
                    with self.lock:
                        job = self.jobs[key]
                        job.update(status='stopped', session_id=None)
                        self.note(job, 'Agent container was stopped and removed.')
                        self.save(job)
                    result = self.get(key)
                cleanup = getattr(type(self.client), 'cleanup', None)
                if cleanup:
                    cleanup(self.client, result)
            return result

    def change_model(self, key, model):
        with self.dispatch_lock:
            with self.lock:
                job = self.jobs[key]
                if job.get('status') not in RESUMABLE:
                    raise ValueError('Pause or review the assignment before changing its model.')
                if not isinstance(model, dict) or not any(
                    model == {'providerID': m['providerID'], 'id': m['id']}
                    for m in self.client.models() if m.get('usable', True)
                ):
                    raise ValueError('Choose a configured local OpenCode model with at least 16K context for this worker.')
                if model == job.get('model'):
                    return copy.deepcopy(job)
                old_model = job.get('model')
                job['model'] = model
                # OpenCode sessions keep their model. Continue from the durable
                # workspace checkpoint in a newly created session instead.
                job.update(session_id=None, resume_session=False, verification_id=None,
                           verification_admitted=False, model_prepared=False)
                job['pending_prompt'] = {'id': 'msg_'+uuid.uuid4().hex,
                    'text': 'Continue the existing assignment from the current project files in /workspace. Preserve completed work, inspect the current project state, and continue any unfinished work. Previous agent summary:\n'+str(job.get('summary', ''))}
                self.note(job, f"Model changed from {old_model.get('providerID')}/{old_model.get('id')} to {model['providerID']}/{model['id']}; continuation will use the latest workspace checkpoint.")
                self.save(job)
                return copy.deepcopy(job)

    def _interrupt(self, job):
        self.client.interrupt(job['session_id'])
        if job.get('verification_id'):
            self.client.stop_verification(job['session_id'], job['verification_id'])
        with self.lock:
            if job['status'] in ('pausing', 'stopping'):
                target = job.pop('after_interrupt', 'paused' if job['status'] == 'pausing' else 'stopped')
                job.update(status=target, error=job.get('error') if target in ('blocked', 'interrupted') else None,
                           resume_session=target == 'interrupted' and bool(job.get('session_id')))
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
        self._run_due_schedule()
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
                    self.note(job, 'Preparing the selected model for the isolated agent.')
                    self.save(job)
                    self.prepare_model(job)
                    with self.lock:
                        job['model_prepared'] = True
                        self.save(job)
            if job.get('resume_session'):
                resume = getattr(type(self.client), 'resume', None)
                if resume:
                    with self.dispatch_lock:
                        if job['status'] not in WORKING:
                            return
                        self.note(job, 'Reconnecting to the isolated assignment session.')
                        self.save(job)
                        sid = resume(self.client, job)
                        with self.lock:
                            replaced = sid != job.get('session_id')
                            job.update(session_id=sid, resume_session=False)
                            if replaced:
                                job.update(verification_id=None, verification_admitted=False)
                                checkpoint = job.get('summary', '')
                                job['pending_prompt'] = {'id': 'msg_'+uuid.uuid4().hex,
                                    'text': 'This assignment resumed in a fresh container because its previous container was unavailable. '
                                    'Continue the existing goal from the current project files in /workspace. Preserve completed work, inspect '
                                    'the current project state, and continue any unfinished work. Previous agent summary:\n'+checkpoint}
                                self.note(job, 'A new container session was created from the latest synchronized project checkpoint.')
                            self.save(job)
                else:
                    with self.lock:
                        job['resume_session'] = False
                        self.save(job)
            if not job['session_id']:
                with self.dispatch_lock:
                    if job['status'] not in WORKING:
                        return
                    self.note(job, 'Building and starting the isolated Docker agent environment.')
                    self.save(job)
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
                            state = part.get('state') or {}
                            content = state.get('content') or []
                            output = '\n'.join(str(item.get('text', '')) for item in content
                                               if isinstance(item, dict) and item.get('type') == 'text')
                            parts.append({'type': 'tool', 'name': part.get('name'),
                                'status': state.get('status'),
                                'input': json.dumps(state.get('input') or {})[:2000],
                                'output': output[-8000:]})
                    recent.append({'id': message['id'], 'type': message.get('type'),
                                   'time': message.get('time') or message.get('createdAt'), 'content': parts})
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
                workspace_directory = getattr(type(self.client), 'workspace_directory', None)
                project_directory = workspace_directory(self.client, job) if workspace_directory else job['project']
                self._send(job, SYSTEM+'\n\nAssigned agent: '+job['assignee']+'\nProject directory: '+project_directory+'\nVerification command: '+(job['verification_command'] or 'Choose appropriate verification and report evidence.'))
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
        finally:
            if job is not None:
                try:
                    sync = getattr(type(self.client), 'sync', None)
                    if sync:
                        sync(self.client, job)
                except Exception as exc:
                    with self.lock:
                        self.note(job, 'Project synchronization issue: '+str(exc)[:400])
                        self.save(job)
                if job['status'] in ('completed', 'stopped'):
                    cleanup = getattr(type(self.client), 'cleanup', None)
                    if cleanup:
                        try: cleanup(self.client, job)
                        except Exception: logging.exception('Could not clean up completed agent container')

    def start_worker(self, available=lambda: True):
        if self.thread and self.thread.is_alive():
            return
        lease = database.connect(autocommit=True)
        try:
            acquired = lease.execute('SELECT pg_try_advisory_lock(74192610, 1)').fetchone()[0]
        except Exception:
            lease.close()
            raise
        if not acquired:
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
