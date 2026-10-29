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
import live_requests

WORKING = {'queued', 'running', 'waiting_input', 'verifying'}
ACTIVE = WORKING | {'pausing', 'stopping'}
INTERRUPTED_ON_RESTART = {'running', 'waiting_input', 'verifying', 'pausing', 'stopping'}
RESUMABLE = {'paused', 'blocked', 'interrupted'}
MARKER = '[HEARTH_TASK_COMPLETE]'
MAX_STEPS_MARKER = 'CRITICAL - MAXIMUM STEPS REACHED'
FIRST_TOKEN_TIMEOUT = 3600
PROGRESS_TIMEOUT = 1800


def text_of(message):
    return '\n'.join(p.get('text', '') for p in message.get('content', []) if p.get('type') == 'text')


def fingerprint(message):
    parts = [{'name': p.get('name'), 'state': p.get('state')} for p in message.get('content', []) if p.get('type') == 'tool']
    return hashlib.sha256(json.dumps([text_of(message), parts], sort_keys=True).encode()).hexdigest()


def response_wait_timeout(job):
    """Allow slow local-model prefill more time before any output token arrives."""
    progress = job.get('agent_model_progress') or {}
    try:
        has_first_token = int(progress.get('tokens') or 0) > 0
    except (TypeError, ValueError):
        has_first_token = False
    return PROGRESS_TIMEOUT if has_first_token else FIRST_TOKEN_TIMEOUT


def is_premature_completion(message):
    """Reject a completion marker attached to a plan, placeholder, or future intent."""
    text = text_of(message).lower()
    unfinished_phrases = (
        'i will resume by', 'i will inspect', 'i will list the files',
        'first, i will', 'first i will', 'next, i will', 'next i will',
        'i will proceed to write', 'will proceed to implement',
        '[complete full runnable solution', '[todo', '[placeholder',
    )
    return any(phrase in text for phrase in unfinished_phrases)


def is_provider_transport_error(error):
    """Recognize transient provider socket failures from exceptions or OpenCode payloads."""
    if isinstance(error, dict):
        error = json.dumps(error, sort_keys=True)
    message = str(error).lower()
    return ('provider.transport' in message or 'econnreset' in message or
            'socket connection was closed unexpectedly' in message)


def is_interrupted_agent_response(error):
    """Recognize an OpenCode turn that ended before producing an answer."""
    if isinstance(error, str):
        try:
            decoded = json.loads(error)
        except (TypeError, ValueError):
            decoded = None
        if isinstance(decoded, dict):
            error = decoded
    if isinstance(error, dict):
        error_type = str(error.get('type') or '').casefold()
        message = str(error.get('message') or '').strip().casefold()
    else:
        error_type = ''
        message = str(error).strip().casefold()
    return message == 'step interrupted' and error_type in ('', 'aborted')


class Manager:
    def __init__(self, directory, client=None, prepare_model=None):
        self.directory = Path(directory)
        self.client = client or OpenCode()
        self.prepare_model = prepare_model or (lambda job: None)
        self.lock = threading.RLock()
        self.dispatch_lock = threading.RLock()
        self.processing: set[str] = set()
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
                    if agent_store.import_legacy_workspace(job['owner'], project, workspace_id=job['id']):
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
        owners = {job['owner'] for job in self.jobs.values()}
        for owner in owners:
            owned = [job for job in self.jobs.values() if job.get('owner') == owner]
            if len(owned) == 1:
                agent_store.assign_legacy_workspace(owner, owned[0]['id'])
        for job in self.jobs.values():
            changed = False
            if job.get('project') != 'PostgreSQL private workspace':
                job['project'] = 'PostgreSQL private workspace'
                changed = True
            for message in job.get('messages', []):
                raw_time = message.get('time')
                if isinstance(raw_time, dict):
                    fallback_time = raw_time.get('completed') or raw_time.get('streamed') or raw_time.get('created')
                else:
                    fallback_time = raw_time
                for part in message.get('content', []):
                    if part.get('type') == 'tool' and part.get('time') is None and fallback_time is not None:
                        part['time'] = fallback_time
                        changed = True
            if changed:
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
                if job.get('status') in INTERRUPTED_ON_RESTART:
                    job.update(status='interrupted', resume_session=bool(job.get('session_id')),
                               error='Hearth restarted while this assignment was active. Resume to continue.')
                    self.note(job, 'Assignment interrupted by Hearth restart; it can be resumed.')
                    self.save(job)

    def save(self, job):
        job['updated_at'] = time.time()
        agent_store.save_task(job)

    def park_container(self, job):
        """Release container resources while retaining this task's reusable runtime."""
        park = getattr(type(self.client), 'park', None)
        if park:
            park(self.client, job)
            return
        cleanup = getattr(type(self.client), 'cleanup', None)
        if cleanup:
            cleanup(self.client, job)

    def note(self, job, text):
        job['activity'] = (job.get('activity', [])+[{'time': time.time(), 'text': text}])[-200:]

    def _record_live_progress(self, job, snap):
        """Make ongoing OpenCode work visible in Activity without exposing task content."""
        latest_tool = None
        for message in snap.get('messages', []):
            for index, part in reversed(list(enumerate(message.get('content', [])))):
                if part.get('type') == 'tool':
                    latest_tool = (str(message.get('id', '')), index, part)
                    break
            if latest_tool:
                break
        if latest_tool:
            message_id, index, part = latest_tool
            state = part.get('state') or {}
            status = state.get('status')
            key = f'{message_id}:{index}:{part.get("name", "tool")}'
            if status in ('running', 'completed', 'error'):
                seen = job.get('activity_tool_state') or {}
                if seen.get(key) != status:
                    seen[key] = status
                    job['activity_tool_state'] = dict(list(seen.items())[-100:])
                    job['agent_last_progress_at'] = time.time()
                    name = str(part.get('name') or 'agent')
                    if status == 'running':
                        self.note(job, f'Agent started a {name} step.')
                    elif status == 'completed':
                        self.note(job, f'Agent completed a {name} step.')
                    else:
                        self.note(job, f'Agent {name} step needs attention.')
                    return
        # The model relay tracks streamed tokens independently of OpenCode's
        # message persistence. Surface that progress so long reasoning turns do
        # not look indistinguishable from a dead session in the task Activity.
        model_request = next((row for row in live_requests.snapshot(limit=100)
                              if row.get('status') == 'streaming'
                              and row.get('source') == job.get('assignee')), None)
        if model_request:
            now = time.time()
            previous = dict(job.get('agent_model_progress') or {})
            request_id = model_request.get('id')
            tokens = int(model_request.get('tokens') or 0)
            if previous.get('id') != request_id:
                previous = {'id': request_id, 'tokens': tokens,
                            'last_progress_at': now, 'noted_at': 0}
            elif tokens > int(previous.get('tokens') or 0):
                previous['tokens'] = tokens
                previous['last_progress_at'] = now
            token_progressed = tokens > int(previous.get('noted_tokens') or 0)
            should_note = (not previous.get('noted_at') or
                           now - float(previous.get('noted_at') or 0) >= 90 or
                           tokens - int(previous.get('noted_tokens') or 0) >= 200)
            if should_note:
                if token_progressed:
                    text = f'Local model response is progressing: {tokens:,} tokens generated so far.'
                elif tokens:
                    text = f'Waiting for the local model to continue; {tokens:,} tokens received so far.'
                else:
                    text = 'Waiting for the local model to send its first response token.'
                self.note(job, text)
                previous['noted_tokens'] = tokens
                previous['noted_at'] = now
            job['agent_model_progress'] = previous
            job['agent_last_progress_at'] = previous.get('last_progress_at', now)
            return
        activity = job.get('activity') or []
        last_update = activity[-1].get('time', 0) if activity else 0
        if time.time() - last_update >= 90:
            self.note(job, 'Agent session is active; waiting for its next response or tool update.')

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
            agent_store.delete_workspace(owner_id)

    def download(self, key):
        job = self.get(key)
        if not job:
            raise KeyError(key)
        if job['status'] != 'completed':
            raise ValueError('Project download is available after the assignment completes.')
        files = agent_store.list_workspace_files(job['owner'], workspace_id=job['id'])
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

    def _normalize_assignment(self, values, existing_model=None):
        title, goal = str(values.get('title', '')).strip(), str(values.get('goal', '')).strip()
        if 'project' in values or 'create_project' in values:
            raise ValueError('Project path is managed by the server and cannot be supplied.')
        if not title or len(title) > 120 or not goal or len(goal) > 20000:
            raise ValueError('Enter a title (up to 120 characters) and a goal (up to 20,000 characters).')
        model = values.get('model')
        same_as_existing = (
            isinstance(model, dict) and isinstance(existing_model, dict) and
            model.get('providerID') == existing_model.get('providerID') and
            model.get('id') == existing_model.get('id')
        )
        if not isinstance(model, dict) or (not same_as_existing and not any(
            model == {'providerID': m['providerID'], 'id': m['id']}
            for m in self.client.models() if m.get('usable', True)
        )):
            raise ValueError('Choose a configured local OpenCode model with at least 16K context for this worker.')
        model = {'providerID': model['providerID'], 'id': model['id']}
        cycles = values.get('max_cycles', 50)
        opencode_steps = values.get('opencode_steps', 40)
        timeout = values.get('verification_timeout', 300)
        if type(cycles) is not int or not 1 <= cycles <= 1000:
            raise ValueError('Assignment work-cycle budget must be 1–1000.')
        if type(opencode_steps) is not int or not 1 <= opencode_steps <= 10000:
            raise ValueError('OpenCode step limit must be 1–10000.')
        if type(timeout) is not int or not 10 <= timeout <= 3600:
            raise ValueError('Verification timeout must be 10–3600 seconds.')
        criteria = str(values.get('criteria', '')).strip()
        command = str(values.get('verification_command', '')).strip()
        assignee = str(values.get('assignee', '')).strip()
        if not assignee or len(assignee) > 120 or any(ord(char) < 32 for char in assignee):
            raise ValueError('Enter an agent name (up to 120 characters).')
        if len(criteria) > 10000 or len(command) > 4000:
            raise ValueError('Acceptance criteria or verification command is too long.')
        shell_mode = values.get('shell_mode', 'auto' if values.get('auto_commands') is True else 'ask')
        if shell_mode not in ('ask', 'auto'):
            raise ValueError('Choose whether shell commands should ask or run automatically.')
        result_label = str(values.get('result_label', '')).strip()
        result_platform = str(values.get('result_platform', 'web')).strip().lower()
        if len(result_label) > 120:
            raise ValueError('Result label must be at most 120 characters.')
        if result_platform not in ('web', 'report', 'document', 'general'):
            raise ValueError('Choose a supported result platform.')
        enable_thinking = values.get('enable_thinking', True)
        if type(enable_thinking) is not bool:
            raise ValueError('Choose whether model thinking is enabled.')
        return {'title': title, 'goal': goal, 'model': model, 'max_cycles': cycles,
                'opencode_steps': opencode_steps,
                'verification_timeout': timeout, 'criteria': criteria,
                'verification_command': command, 'assignee': assignee, 'shell_mode': shell_mode,
                'enable_thinking': enable_thinking, 'result_label': result_label,
                'result_platform': result_platform}

    def create(self, values, owner):
        assignment = self._normalize_assignment(values)
        self.client.ensure_agent()
        with self.lock:
            if sum(j['status'] in ACTIVE for j in self.jobs.values()) >= 20:
                raise ValueError('The agent queue already has 20 active assignments.')
            job = {'id': uuid.uuid4().hex, 'owner': owner, **assignment,
                'project': 'PostgreSQL private workspace', 'auto_commands': assignment['shell_mode'] == 'auto',
                'cycles': 0, 'run_count': 1, 'started': False, 'status': 'queued', 'session_id': None, 'created_at': time.time(),
                'activity': [], 'messages': [], 'forms': [], 'permissions': [], 'error': None,
                'pending_prompt': None, 'handled_message': None, 'errors': 0, 'stalls': 0}
            self.note(job, 'Assignment queued. Project files are stored in this account’s private PostgreSQL workspace.')
            self.jobs[job['id']] = job
            self.save(job)
            return copy.deepcopy(job)

    def create_schedule(self, values, owner):
        task_id = str(values.get('task_id', '')).strip()
        task = self.get(task_id) if task_id else None
        if not task or task.get('owner') != owner:
            raise ValueError('Choose an existing assignment you own or can manage.')
        if task.get('status') not in ACTIVE | {'completed', 'stopped'}:
            raise ValueError('Choose an active, completed, or stopped assignment.')
        timing = self._normalize_schedule_timing(values)
        schedule = {'id': uuid.uuid4().hex, 'owner': owner,
                    'task_id': task_id, 'task_title': task['title'],
                    **{key: value for key, value in timing.items() if key != 'first_run'},
                    'enabled': True, 'created_at': time.time(), 'next_run_at': timing['first_run'],
                    'last_run_at': None, 'last_task_id': None, 'last_error': None}
        agent_store.save_schedule(schedule)
        return schedule

    @staticmethod
    def _normalize_schedule_timing(values):
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
        return {'interval_seconds': interval, 'interval_value': value,
                'interval_unit': unit, 'run_time': run_time or None,
                'timezone': timezone_name, 'first_run': first_run}

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

    def update_schedule(self, schedule_id, owner, values):
        schedule = next((x for x in agent_store.list_schedules(owner) if x['id'] == schedule_id), None)
        if not schedule:
            return None
        timing = self._normalize_schedule_timing(values)
        task = self.get(schedule.get('task_id'))
        if not task or task.get('owner') != owner:
            return None
        schedule.update({key: value for key, value in timing.items() if key != 'first_run'})
        schedule['task_title'] = task['title']
        schedule['next_run_at'] = timing['first_run']
        schedule['last_error'] = None
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
        previous_restart_recovery = (previous and previous.get('status') == 'interrupted'
                                      and previous.get('schedule_id') == schedule['id']
                                      and previous.get('scheduled_run_at') is not None)
        if previous and previous.get('status') not in ('completed', 'stopped') and not previous_restart_recovery:
            schedule['last_error'] = 'Run skipped because the previous scheduled assignment is still active.'
            agent_store.save_schedule(schedule)
            return
        try:
            selected_task_id = (schedule.get('task_id') or schedule.get('project_task_id')
                                or schedule.get('last_task_id'))
            if selected_task_id:
                job = self.get(selected_task_id)
                if not job or job.get('owner') != schedule['owner']:
                    schedule['enabled'] = False
                    schedule['last_error'] = 'The scheduled assignment was deleted or is no longer owned by this account.'
                    agent_store.save_schedule(schedule)
                    return
                restart_recovery = (job.get('status') == 'interrupted'
                                    and job.get('schedule_id') == schedule['id']
                                    and job.get('scheduled_run_at') is not None)
                if job.get('status') not in ('completed', 'stopped') and not restart_recovery:
                    schedule['last_error'] = f"Run skipped because the selected assignment is {job.get('status', 'unavailable')}. It will be checked again at the next interval."
                    agent_store.save_schedule(schedule)
                    return
                job = self.rerun(job['id'], schedule_id=schedule['id'], scheduled_at=scheduled_at)
                schedule['task_id'] = job['id']
                schedule['task_title'] = job['title']
                schedule['last_task_id'] = job['id']
            else:
                # Keep schedules created by older Hearth versions working.
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
        if action not in ('pause', 'resume', 'stop', 'reset-cycles'):
            raise ValueError('Unknown agent control.')
        with self.dispatch_lock:
            with self.lock:
                job = self.jobs[key]
                if action == 'reset-cycles':
                    if job['status'] not in ('running', 'waiting_input', 'paused', 'blocked', 'interrupted'):
                        raise ValueError('Reset cycles while reviewing a running, waiting, paused, blocked, or interrupted assignment.')
                    job.update(cycles=0, started=True, errors=0, stalls=0, idle_polls=0,
                               next_retry=0, fingerprint=None, error=None)
                    self.note(job, 'Work-cycle counter reset after human review. Continue when ready.')
                    self.save(job)
                    return copy.deepcopy(job)
                if action == 'resume':
                    if job['status'] not in RESUMABLE:
                        raise ValueError('Only paused, blocked, or interrupted assignments can resume.')
                    if job['cycles'] >= job['max_cycles']:
                        job['max_cycles'] += 50
                    max_steps_message = next((message for message in reversed(job.get('messages', []))
                        if message.get('type') == 'assistant'
                        and MAX_STEPS_MARKER in text_of(message).upper()), None)
                    resume_after_step_limit = bool(max_steps_message)
                    if resume_after_step_limit:
                        job['max_steps_reviewed_message'] = max_steps_message['id']
                    job.update(status='queued', errors=0, stalls=0, error=None, next_retry=0,
                               awaiting_since=0, idle_polls=0, model_prepared=False,
                               interrupted_response_retries=0)
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
            if sid and (action != 'resume' or resume_after_step_limit):
                try:
                    if action == 'resume':
                        self.client.interrupt(sid)
                    else:
                        self._interrupt(job)
                except Exception as exc:
                    with self.lock:
                        if action == 'resume':
                            self.note(job, 'Could not stop the exhausted OpenCode session; continuing from the saved workspace in a fresh session.')
                            checkpoint = job.get('summary', '')
                            job.update(
                                session_id=None,
                                resume_session=False,
                                pending_prompt={
                                    'id': 'msg_'+uuid.uuid4().hex,
                                    'text': 'Continue this assignment from the latest synchronized project files. Preserve completed work, inspect the current state, and continue unfinished work. Previous response before the step limit:\n'+checkpoint,
                                },
                            )
                        else:
                            target = 'blocked' if job.get('after_interrupt') == 'blocked' else 'interrupted'
                            job.pop('after_interrupt', None)
                            job.update(
                                status=target,
                                resume_session=bool(sid),
                                error='OpenCode did not acknowledge the interruption. Review the saved work, then continue to resume.'
                            )
                            self.note(job, 'OpenCode did not acknowledge the interruption; the assignment is ready for review and can be continued.')
                        self.save(job)
            result = self.get(key)
            if action == 'stop' and result:
                if result['status'] not in ('stopped', 'interrupted', 'blocked'):
                    with self.lock:
                        job = self.jobs[key]
                        job.update(status='stopped', session_id=None)
                        self.note(job, 'Agent container stopped and retained for the next run.')
                        self.save(job)
                    result = self.get(key)
                sync = getattr(type(self.client), 'sync', None)
                if sync and result.get('session_id'):
                    sync(self.client, result)
                self.park_container(result)
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
                    'text': 'Continue the existing assignment from the current project files in /home/node/agents/project. Preserve completed work, inspect the current project state, and continue any unfinished work. Previous agent summary:\n'+str(job.get('summary', ''))}
                self.note(job, f"Model changed from {old_model.get('providerID')}/{old_model.get('id')} to {model['providerID']}/{model['id']}; continuation will use the latest workspace checkpoint.")
                self.save(job)
                return copy.deepcopy(job)

    def rerun(self, key, *, schedule_id=None, scheduled_at=None):
        """Queue another run from its saved workspace, including recovery of a restarted scheduled run."""
        rerunnable = ('completed', 'stopped', 'interrupted', 'blocked')
        with self.dispatch_lock:
            with self.lock:
                job = self.jobs[key]
                restart_recovery = (schedule_id and job.get('status') == 'interrupted'
                                    and job.get('schedule_id') == schedule_id
                                    and job.get('scheduled_run_at') is not None)
                if job.get('status') not in rerunnable and not restart_recovery:
                    raise ValueError('Only completed, stopped, interrupted, or blocked assignments can be run again.')
                if key in self.processing:
                    raise ValueError('The completed assignment is still saving its project. Try again shortly.')
                if sum(item.get('status') in ACTIVE for item in self.jobs.values()) >= 20:
                    raise ValueError('The agent queue already has 20 active assignments.')
                snapshot = copy.deepcopy(job)
                previous_summary = job.get('summary', '')
                previous_messages = copy.deepcopy(job.get('messages', []))
            if snapshot.get('status') in ('interrupted', 'blocked') and snapshot.get('session_id'):
                sync = getattr(type(self.client), 'sync', None)
                if sync:
                    try:
                        sync(self.client, snapshot)
                    except Exception as exc:
                        logging.warning('Could not sync the interrupted assignment before resubmitting: %s', exc)
            self.park_container(snapshot)
            with self.lock:
                job = self.jobs[key]
                restart_recovery = (schedule_id and job.get('status') == 'interrupted'
                                    and job.get('schedule_id') == schedule_id
                                    and job.get('scheduled_run_at') is not None)
                if job.get('status') not in rerunnable and not restart_recovery:
                    raise ValueError('Only completed, stopped, interrupted, or blocked assignments can be run again.')
                job.update(
                    status='queued', cycles=0, started=False, errors=0, stalls=0,
                    idle_polls=0, next_retry=0, awaiting_since=0, error=None,
                    completion=None, finished_at=None, summary='', fingerprint=None,
                    handled_message=None, session_id=None, resume_session=False,
                    model_prepared=False, verification=None, verification_id=None,
                    verification_started=0, verification_admitted=False,
                    pending_prompt=None, steering=[],
                    forms=[], permissions=[], messages=[], transport_retries=0,
                    retry_current_cycle=False,
                )
                if schedule_id:
                    job.update(schedule_id=schedule_id, scheduled_run_at=scheduled_at)
                    refinements = job.get('instructions', [])
                    prompt = (
                        'This is a scheduled continuation of an existing assignment. The project workspace contains code and files '
                        'built during earlier runs. Inspect and reuse that work; do not start over or recreate completed parts. '
                        'Continue from the current project state, preserve working code, and make only the changes needed for this occurrence.\n\n'
                        'Assignment goal:\n'+job['goal']+'\n\nAcceptance criteria:\n'+
                        (job.get('criteria') or 'Complete and verify the requested goal.')+
                        ('\n\nSaved progress summary from the previous run:\n'+previous_summary if previous_summary else '')+
                        ('\n\nUser refinements:\n'+'\n'.join(refinements) if refinements else '')
                    )
                    job['summary'] = previous_summary
                    job['messages'] = previous_messages
                    job['pending_prompt'] = {'id': 'msg_'+uuid.uuid4().hex, 'text': prompt}
                else:
                    job.pop('scheduled_run_at', None)
                job['run_count'] = max(1, int(job.get('run_count', 1))) + 1
                self.note(job, 'Scheduled continuation queued from the saved project workspace.' if schedule_id else
                          'Rerun queued from the completed assignment using its latest saved project files.')
                self.save(job)
                return copy.deepcopy(job)

    def update_assignment(self, key, values):
        """Save edited task settings without starting another run."""
        with self.lock:
            existing_model = self.jobs[key].get('model')
        assignment = self._normalize_assignment(values, existing_model=existing_model)
        with self.dispatch_lock:
            with self.lock:
                job = self.jobs[key]
                if job.get('status') not in ('completed', 'stopped', 'interrupted', 'blocked', 'paused'):
                    raise ValueError('Only completed, stopped, interrupted, blocked, or paused assignments can be edited.')
                if key in self.processing:
                    raise ValueError('The assignment is still saving its project. Try again shortly.')
                steps_changed = job.get('opencode_steps', 40) != assignment['opencode_steps']
                sync_snapshot = copy.deepcopy(job) if steps_changed and job.get('status') in ('interrupted', 'blocked') else None
            if sync_snapshot and sync_snapshot.get('session_id'):
                sync = getattr(type(self.client), 'sync', None)
                if sync:
                    sync(self.client, sync_snapshot)
            with self.lock:
                job = self.jobs[key]
                job.update(assignment, auto_commands=assignment['shell_mode'] == 'auto')
                if steps_changed and job.get('status') in ('interrupted', 'blocked'):
                    checkpoint = job.get('summary', '')
                    job.update(session_id=None, resume_session=False,
                        pending_prompt={'id': 'msg_'+uuid.uuid4().hex,
                            'text': 'Continue this assignment from the latest synchronized project files. Preserve completed work, inspect the current state, and continue unfinished work. Previous agent summary:\n'+checkpoint})
                self.note(job, 'Task details saved; the assignment was not resubmitted.')
                self.save(job)
                return copy.deepcopy(job)

    def edit_and_rerun(self, key, values):
        """Save edited settings and queue the assignment from its existing workspace."""
        with self.dispatch_lock:
            self.update_assignment(key, values)
            return self.rerun(key)

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
            if job['status'] != 'running' or not job.get('started', bool(job['cycles'])):
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

    @staticmethod
    def _verification_guidance(job):
        command = str(job.get('verification_command') or '').strip()
        if not command:
            return ('Verification plan: No independent verification command is configured. Identify and run the '
                    'project checks required by the acceptance criteria, and include any necessary verifier files '
                    'or fixtures in the saved project workspace before claiming completion.')
        return (
            'Verification plan (part of this implementation):\n'
            'Hearth will independently run this exact command after your completion claim:\n'
            '```sh\n'+command+'\n```\n'
            'Before implementation, inspect the command and identify every referenced verifier script, test, '
            'fixture, configuration file, generated input, and dependency. Create or update any required files '
            'inside the project workspace and keep them in the deliverable. Check that the command uses tools '
            'and interpreters available in this isolated container; update the command only when needed to invoke '
            'the equivalent available tool. Run the same command yourself after implementation, fix failures, '
            'and repeat it. Do not claim completion until the configured command passes independently.'
        )

    def _send(self, job, text):
        with self.dispatch_lock:
            with self.lock:
                if job['status'] not in WORKING:
                    return
                retrying_cycle = bool(job.get('retry_current_cycle'))
                if job['cycles'] >= job['max_cycles'] and not retrying_cycle:
                    job.update(status='blocked', error='Cycle budget reached. Resume to grant 50 more cycles.')
                    self.save(job)
                    return
                if not job.get('pending_prompt'):
                    scope = 'Assignment goal:\n'+job['goal']+'\nAcceptance criteria:\n'+(job['criteria'] or 'Complete and verify the requested goal.')
                    refinements = job.get('instructions', [])
                    if refinements:
                        scope += '\nUser refinements (latest takes precedence):\n'+'\n'.join(refinements)
                    job['pending_prompt'] = {'id': 'msg_'+uuid.uuid4().hex, 'text': scope+'\n\n'+text}
                verification_guidance = self._verification_guidance(job)
                if verification_guidance not in job['pending_prompt']['text']:
                    job['pending_prompt']['text'] += '\n\n'+verification_guidance
                self.save(job)
                pending = copy.deepcopy(job['pending_prompt'])
            self.client.prompt(job['session_id'], pending['text'], pending['id'])
            with self.lock:
                retry_reason = job.get('retry_current_cycle_reason')
                job.update(pending_prompt=None, started=True, status='running', errors=0,
                           transport_retries=job.get('transport_retries', 0) if retrying_cycle else 0,
                           retry_current_cycle=False, error=None, next_retry=0, idle_polls=0,
                           awaiting_since=time.time(), agent_last_progress_at=time.time(),
                           agent_model_progress=None,
                           cycles=job['cycles'] if retrying_cycle else job['cycles']+1)
                job.pop('retry_current_cycle_reason', None)
                self.note(job, (f'Agent work cycle {job["cycles"]} retried after '
                                f'{retry_reason or "a provider connection reset"}.'
                                if retrying_cycle else f'Agent work cycle {job["cycles"]} dispatched.'))
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
            # A task waiting for user input consumes no worker lane. Leaving it at
            # the head of this single-lane supervisor would starve every queued task
            # (including an explicit rerun) until the user answered it.
            job = next((j for j in sorted(self.jobs.values(), key=lambda x: (x['status'] == 'queued', x['created_at']))
                        if j['status'] in ACTIVE and j['status'] != 'waiting_input'), None)
            if not job or time.time() < job.get('next_retry', 0):
                return
            self.processing.add(job['id'])
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
                                    'Continue the existing goal from the current project files in /home/node/agents/project. Preserve completed work, inspect '
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
                    self.note(job, 'Starting or reusing the isolated Docker agent environment.')
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
                    raw_time = message.get('time') or message.get('createdAt')
                    for part in message.get('content', []):
                        if part.get('type') == 'text':
                            parts.append({'type': 'text', 'text': part.get('text', '')[-8000:]})
                        elif part.get('type') == 'tool':
                            state = part.get('state') or {}
                            content = state.get('content') or []
                            output = '\n'.join(str(item.get('text', '')) for item in content
                                               if isinstance(item, dict) and item.get('type') == 'text')
                            tool_error = state.get('error')
                            if tool_error is not None and not isinstance(tool_error, str):
                                tool_error = json.dumps(tool_error, sort_keys=True)
                            parts.append({'type': 'tool', 'name': part.get('name'),
                                'status': state.get('status'),
                                'time': ((raw_time.get('completed') or raw_time.get('streamed') or raw_time.get('created'))
                                         if isinstance(raw_time, dict) else raw_time),
                                'input': json.dumps(state.get('input') or {})[:2000],
                                'output': output[-8000:],
                                'error': tool_error[-4000:] if tool_error else None})
                    recent.append({'id': message['id'], 'type': message.get('type'),
                                   'time': raw_time, 'content': parts})
                job.update(messages=recent, forms=snap['forms'], permissions=snap['permissions'])
                if snap['forms'] or snap['permissions']:
                    job['status'] = 'waiting_input'
                    self.save(job)
                    return
                max_steps_message = next((m for m in snap['messages']
                    if m.get('type') == 'assistant'
                    and MAX_STEPS_MARKER in text_of(m).upper()), None)
                if (max_steps_message and max_steps_message['id']
                        != job.get('max_steps_reviewed_message')):
                    job.update(
                        status='blocked',
                        error='OpenCode reached its maximum step limit. Review the saved work, then continue to resume the assignment.',
                        handled_message=max_steps_message['id'],
                        max_steps_reviewed_message=max_steps_message['id'],
                        awaiting_since=0,
                    )
                    self.note(job, 'OpenCode reached its maximum step limit; the assignment is ready to continue after review.')
                    self.save(job)
                    return
                latest = next((m for m in snap['messages'] if m.get('type') == 'assistant'), None)
                latest_time = (latest or {}).get('time') or (latest or {}).get('createdAt')
                latest_finished = (latest is not None and
                                   latest.get('id') != job.get('handled_message') and
                                   isinstance(latest_time, dict) and
                                   bool(latest_time.get('completed')))
                # The session active list can briefly lag behind the durable
                # assistant message. Process a completed turn even while that
                # session-level indicator is stale.
                if snap['inbox'] or (snap['active'] and not latest_finished):
                    active_since = job.get('awaiting_since')
                    if not active_since:
                        active_since = time.time()
                        job['awaiting_since'] = active_since
                    last_progress = max(
                        float(active_since),
                        float(job.get('agent_last_progress_at') or active_since),
                    )
                    wait_timeout = response_wait_timeout(job)
                    if time.time() - last_progress >= wait_timeout:
                        timeout_minutes = wait_timeout // 60
                        reason = (
                            f'No first model token arrived within {timeout_minutes} minutes.'
                            if wait_timeout == FIRST_TOKEN_TIMEOUT else
                            f'OpenCode reported an active session for {timeout_minutes} minutes without a new assistant response or tool update.'
                        )
                        job.update(status='blocked', error=reason + ' Review the session and resume when ready.')
                        self.note(job, reason + ' Assignment needs review.')
                        self.save(job)
                        return
                    job.update(status='running', idle_polls=0)
                    self._record_live_progress(job, snap)
                    self.save(job)
                    return
                if not job.get('started', bool(job['cycles'] or job.get('session_id') or job.get('handled_message'))):
                    self.save(job)
                    initial = True
                else:
                    initial = False
                    if not latest or latest['id'] == job.get('handled_message'):
                        if job['status'] == 'queued':
                            pass
                        # OpenCode may briefly report an idle session while a provider
                        # finishes persisting the assistant turn. Keep observing the
                        # durable session instead of converting an ordinary long wait
                        # into a worker failure that eventually requires review.
                        elif time.time()-job.get('awaiting_since', time.time()) < response_wait_timeout(job):
                            return
                        else:
                            wait_timeout = response_wait_timeout(job)
                            timeout_minutes = wait_timeout // 60
                            reason = (
                                f'No first model token arrived within {timeout_minutes} minutes.'
                                if wait_timeout == FIRST_TOKEN_TIMEOUT else
                                f'OpenCode has been idle for {timeout_minutes} minutes without a new assistant result.'
                            )
                            job.update(status='blocked', error=reason + ' Review the session and resume when ready.')
                            self.note(job, reason + ' Assignment needs review.')
                            self.save(job)
                            return
                    else:
                        job['idle_polls'] = job.get('idle_polls', 0)+1
                        if job['idle_polls'] < 2:
                            self.save(job)
                            return
                        if latest.get('error'):
                            if is_provider_transport_error(latest['error']):
                                retries = job.get('transport_retries', 0) + 1
                                job['handled_message'] = latest['id']
                                job['pending_prompt'] = {
                                    'id': 'msg_'+uuid.uuid4().hex,
                                    'text': 'The previous model response failed because the provider connection closed unexpectedly. '
                                    'Retry the current assignment from the existing workspace, preserve completed work, and continue the goal.'}
                                job.update(awaiting_since=0, errors=0, error=str(latest['error'])[:1200],
                                           transport_retries=retries, retry_current_cycle=True,
                                           retry_current_cycle_reason='a provider connection reset',
                                           next_retry=time.time()+min(60, 5*2**min(retries-1, 4)))
                                if retries == 1 or retries % 5 == 0:
                                    self.note(job, 'Provider connection closed during the response; retrying the same work cycle automatically.')
                                self.save(job)
                                return
                            if is_interrupted_agent_response(latest['error']):
                                retries = int(job.get('interrupted_response_retries') or 0) + 1
                                job['handled_message'] = latest['id']
                                if retries >= 3:
                                    job.update(status='blocked', errors=0,
                                               interrupted_response_retries=retries,
                                               resume_session=bool(job.get('session_id')),
                                               pending_prompt={
                                                   'id': 'msg_'+uuid.uuid4().hex,
                                                   'text': 'Continue from the saved project workspace. Preserve completed work, inspect the current state, and finish the remaining acceptance criteria.'},
                                               error='OpenCode interrupted three consecutive responses. Review the saved project, then resume to continue.')
                                    self.note(job, 'OpenCode interrupted three consecutive responses; the assignment needs review before continuing.')
                                    self.save(job)
                                    return
                                scope = ('Assignment goal:\n'+job['goal']+
                                         '\nAcceptance criteria:\n'+(job.get('criteria') or 'Complete and verify the requested goal.'))
                                refinements = job.get('instructions', [])
                                if refinements:
                                    scope += '\nUser refinements (latest takes precedence):\n'+'\n'.join(refinements)
                                job.update(
                                    pending_prompt={
                                        'id': 'msg_'+uuid.uuid4().hex,
                                        'text': scope+'\n\nThe previous model response was interrupted before it produced a complete answer. Continue from the current project workspace, preserve completed work, inspect the current state, and finish the remaining acceptance criteria. Do not repeat completed work.'},
                                    awaiting_since=0, errors=0, error=None,
                                    retry_current_cycle=True,
                                    retry_current_cycle_reason='an interrupted model response',
                                    interrupted_response_retries=retries,
                                    next_retry=time.time()+min(60, 5*2**(retries-1)),
                                )
                                self.note(job, 'The previous model response was interrupted; Hearth will continue from the saved project in the same work cycle.')
                                self.save(job)
                                return
                            raise RuntimeError(str(latest['error']))
                        job['interrupted_response_retries'] = 0
                        job.update(error=None, transport_retries=0)
                        job['handled_message'] = latest['id']
                        fp = fingerprint(latest)
                        job['stalls'] = job['stalls']+1 if fp == job.get('fingerprint') else 0
                        job['fingerprint'] = fp
                        text = text_of(latest).strip()
                        job['summary'] = text[-8000:]
                        if latest.get('finish') == 'stop' and text.splitlines() and text.splitlines()[-1].strip() == MARKER:
                            if is_premature_completion(latest):
                                self.note(job, 'Ignored a completion marker attached to a plan or placeholder; continuing the assignment.')
                                self.save(job)
                            elif job['verification_command']:
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
                scheduled_note = ('\nScheduled occurrence (UTC): '+datetime.fromtimestamp(
                    job['scheduled_run_at'], timezone.utc).isoformat()) if job.get('scheduled_run_at') else ''
                self._send(job, SYSTEM+'\n\nAssigned agent: '+job['assignee']+'\nProject directory: '+project_directory+'\nVerification command: '+(job['verification_command'] or 'Choose appropriate verification and report evidence.')+scheduled_note)
            else:
                self._send(job, 'Continue the assigned goal. Inspect the current state and remaining todos, implement the next step, and verify. '
                    'Do not repeat completed work or answered questions. If a concrete blocker needs user input, ask through the question tool. '
                    'End with '+MARKER+' only when the entire goal and acceptance criteria are achieved.')
        except Exception as exc:
            with self.lock:
                if job['status'] not in ACTIVE:
                    return
                if 'isolated agent container is missing' in str(exc).lower() and job.get('session_id'):
                    job.pop('after_interrupt', None)
                    job.update(status='queued', resume_session=True, errors=0, error=None,
                               next_retry=time.time()+1)
                    self.note(job, 'The isolated container was removed; rebuilding it from the latest synchronized project checkpoint.')
                    self.save(job)
                    return
                if is_provider_transport_error(exc):
                    # Socket resets can happen while a provider is responding or while
                    # Hearth is polling OpenCode. Keep the same session and, when a
                    # prompt is still pending, its idempotency key; do not turn a
                    # transient network failure into a human-review state.
                    job['errors'] = 0
                    job['error'] = str(exc)[:1200]
                    retries = job.get('transport_retries', 0) + 1
                    job['transport_retries'] = retries
                    job['next_retry'] = time.time()+min(60, 5*2**min(retries-1, 4))
                    if retries == 1 or retries % 5 == 0:
                        self.note(job, 'Provider connection closed; retrying automatically: '+job['error'])
                    self.save(job)
                    return
                if job['status'] in ('pausing', 'stopping'):
                    target = 'blocked' if job.get('after_interrupt') == 'blocked' else 'interrupted'
                    job.pop('after_interrupt', None)
                    job.update(
                        status=target,
                        resume_session=bool(job.get('session_id')),
                        error='OpenCode did not acknowledge the interruption: '+str(exc)[:900],
                    )
                    self.note(job, 'OpenCode did not acknowledge the interruption; the assignment can be continued after review.')
                    self.save(job)
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
                except Exception as exc:
                    with self.lock:
                        job.pop('after_interrupt', None)
                        job.update(
                            status='blocked',
                            resume_session=bool(job.get('session_id')),
                            error='OpenCode did not acknowledge the interruption: '+str(exc)[:900],
                        )
                        self.note(job, 'The worker stopped after repeated errors; its saved work is ready to resume.')
                        self.save(job)
        finally:
            if job is not None:
                try:
                    try:
                        sync = getattr(type(self.client), 'sync', None)
                        if sync:
                            sync(self.client, job)
                    except Exception as exc:
                        with self.lock:
                            self.note(job, 'Project synchronization issue: '+str(exc)[:400])
                            self.save(job)
                    if job['status'] in ('completed', 'stopped'):
                        try: self.park_container(job)
                        except Exception: logging.exception('Could not park completed agent container')
                finally:
                    with self.lock:
                        self.processing.discard(job['id'])

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
