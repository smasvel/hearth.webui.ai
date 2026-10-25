"""Persistent, browser-independent validation jobs with bounded verified repair."""
import copy
import json
import os
import threading
import time
import uuid
from pathlib import Path

import model_test_profiles as profiles
from tools.test_flm_model import Cancelled, run_suite

ACTIVE = {'loading', 'running', 'repairing', 'cancelling'}


class Registry:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.lock = threading.RLock()
        self.jobs = {}
        self.cancels = {}
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        paths = sorted(self.directory.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in paths[30:]:
            old.unlink(missing_ok=True)
        for path in paths[:30]:
            try:
                job = json.loads(path.read_text())
                if job['status'] in ACTIVE:
                    try:
                        pid = job.get('owner_pid')
                        if not isinstance(pid, int) or pid <= 0:
                            raise ProcessLookupError()
                        os.kill(pid, 0)
                    except (ProcessLookupError, PermissionError):
                        job.update(status='interrupted', finished_at=time.time(), error='Hearth restarted; start a new run to continue.')
                        self.save(job)
                self.jobs[job['id']] = job
            except (ValueError, KeyError, OSError):
                continue

    def save(self, job):
        path = self.directory / (job['id'] + '.json')
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(job, indent=2))
        temporary.replace(path)

    def list(self):
        with self.lock:
            return copy.deepcopy(sorted(self.jobs.values(), key=lambda j: j['started_at'], reverse=True)[:30])

    def get(self, job_id):
        with self.lock:
            return copy.deepcopy(self.jobs.get(job_id))

    def cancel(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            if job['status'] in ACTIVE:
                if job_id not in self.cancels:
                    raise ValueError('This run belongs to another Hearth process; cancel it in the process that started it.')
                self.cancels[job_id].set()
                job['status'] = 'cancelling'
                self.save(job)
            return copy.deepcopy(job)

    def delete(self, job_id):
        """Delete one saved run; active runs must finish or be cancelled first."""
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            if job['status'] in ACTIVE:
                raise ValueError('Cancel the active run and wait for it to finish before deleting its report.')
            self.jobs.pop(job_id)
            self.cancels.pop(job_id, None)
            (self.directory / (job_id + '.json')).unlink(missing_ok=True)
            return True

    def start(self, targets, repair, load, connection, context, *, background=True):
        with self.lock:
            if any(j['status'] in ACTIVE for j in self.jobs.values()):
                raise ValueError('A model test run is already active. Wait or cancel it first.')
            job = {'id': uuid.uuid4().hex, 'status': 'loading', 'owner_pid': os.getpid(), 'started_at': time.time(),
                   'finished_at': None, 'auto_repair': repair, 'targets': copy.deepcopy(targets),
                   'results': [], 'repairs': [], 'current_model': None, 'current_check': None, 'error': None}
            self.jobs[job['id']] = job
            cancel = self.cancels[job['id']] = threading.Event()
            self.save(job)
            # Retain only bounded history; preserve the new active job.
            for old in sorted(self.jobs.values(), key=lambda j: j['started_at'], reverse=True)[30:]:
                self.jobs.pop(old['id'], None)
                self.cancels.pop(old['id'], None)
                (self.directory / (old['id'] + '.json')).unlink(missing_ok=True)
        def update(**values):
            with self.lock:
                job.update(values)
                self.save(job)
        def execute():
            try:
                for target in targets:
                    if cancel.is_set():
                        raise Cancelled()
                    backend, model = target['backend'], target['id']
                    update(status='loading', current_model=target, current_check=None)
                    try:
                        load(target)
                        url, served_model, headers = connection(target)
                        window = context(target)
                        def record(result, attempt=1):
                            with self.lock:
                                job['results'].append({**result, 'backend': backend, 'model': model, 'attempt': attempt})
                                self.save(job)
                        update(status='running')
                        results = run_suite(served_model, url, context=window, headers=headers,
                            cancel=cancel, on_result=record, on_case=lambda case: update(current_check=case), guarded=backend == 'fastflowlm',
                            profile=lambda body: profiles.apply(body, backend))
                        failures = {(r['name'], r.get('stream', False)) for r in results if not r['passed']}
                        if repair and failures:
                            if any(r.get('backend_unavailable') for r in results):
                                with self.lock:
                                    job['repairs'].append({'model': model, 'backend': backend,
                                        'outcome': 'Backend unavailable; restore the runtime before retrying. No compatibility recipe attempted.'})
                                    self.save(job)
                                continue
                            previous = profiles.get(backend, served_model)
                            if previous == profiles.RECIPE:
                                with self.lock:
                                    job['repairs'].append({'model': model, 'backend': backend, 'outcome': 'No additional supported repair; needs model/runtime investigation.'})
                                    self.save(job)
                                continue
                            update(status='repairing')
                            committed = False
                            try:
                                retried = run_suite(served_model, url, context=window, headers=headers,
                                    cancel=cancel, on_result=lambda r: record(r, 2), on_case=lambda case: update(current_check=case),
                                    guarded=backend == 'fastflowlm', profile=lambda body: profiles.apply_recipe(body, profiles.RECIPE))
                                # Keep a recipe only when at least one original assertion now passes.
                                remaining = {(r['name'], r.get('stream', False)) for r in retried if not r['passed']}
                                committed = len(retried) == len(results) and remaining < failures
                                if committed:
                                    profiles.set_profile(backend, served_model, profiles.RECIPE)
                                with self.lock:
                                    job['repairs'].append({'model': model, 'backend': backend,
                                        'recipe': profiles.RECIPE, 'previous': previous,
                                        'outcome': 'Applied and verified' if committed else 'No improvement; rolled back',
                                        'fixed': len(failures - remaining) if committed else 0, 'remaining': len(remaining) if committed else len(failures)})
                                    self.save(job)
                            finally:
                                if not committed:
                                    profiles.set_profile(backend, served_model, previous)
                                    with self.lock:
                                        for result in job['results']:
                                            if result['backend'] == backend and result['model'] == model and result['attempt'] == 2:
                                                result['rolled_back'] = True
                                        self.save(job)
                    except Cancelled:
                        raise
                    except Exception as exc:
                        self._setup_error(job, target, exc)
                update(status='completed', current_check=None, finished_at=time.time())
            except Cancelled:
                update(status='cancelled', current_check=None, finished_at=time.time())
            except Exception as exc:
                update(status='error', error=str(exc)[:1500], finished_at=time.time())
        if background:
            threading.Thread(target=execute, daemon=True, name='model-tests').start()
        else:
            execute()
        return self.get(job['id'])

    def _setup_error(self, job, target, exc):
        with self.lock:
            job['results'].append({'backend': target['backend'], 'model': target['id'],
                                  'name': 'model-setup', 'passed': False, 'error': str(exc)[:1500], 'attempt': 1})
            self.save(job)
