"""Persistent, browser-independent validation jobs with bounded verified repair."""


import copy
import json
import os
import threading
import time
import uuid
from pathlib import Path

from hearth.storage import database
from hearth.storage import model_metadata_store
from hearth.models import model_test_profiles as profiles
from hearth.backends import kobold_client
from tools.test_flm_model import Cancelled, run_suite

ACTIVE = {'loading', 'running', 'repairing', 'cancelling'}
# Suites accepted by the API and offered by the Model tests page tabs. 'core'/'extra'
# are slices of 'all' so slow long-running checks (workflows, context overflow) can be
# skipped without rerunning chat+tool basics; each also maps into run_suite's repairable
# set below.
SUITES = ('all', 'complete', 'core', 'opencode', 'extra', 'tools', 'iq', 'security')
REASONING_CHECKS = {
    'number-sequence', 'logic-syllogism', 'analogy', 'odd-one-out', 'spatial-count',
    'conditional-logic', 'arithmetic', 'letter-pattern', 'deduction',
    'rate-reasoning', 'rule-testing', 'probability',
}


def performance_report(jobs):
    """Compare each model's latest comprehensive completed run.

    Narrow suites are useful during debugging but are not comparable evaluations. The
    recommendation is quality-first; speed and token use only break close quality scores.
    """
    latest = {}
    for job in jobs:
        if job.get('status') != 'completed' or job.get('suite') not in (None, 'all', 'complete'):
            continue
        for target in job.get('targets') or []:
            key = (target.get('backend'), target.get('id'))
            # Hosted OpenCode catalog models are not supported by Hearth model tests.
            # Ignore historical/stale reports too, so they cannot reappear in Metrics.
            if key[0] == 'opencode' or (isinstance(key[1], str) and key[1].startswith('opencode/')):
                continue
            results = [result for result in job.get('results') or []
                       if result.get('backend') == key[0] and result.get('model') == key[1]
                       and not result.get('rolled_back') and not result.get('diagnostic')]
            if not results:
                continue
            if key not in latest or (job.get('started_at') or 0) > (latest[key][0].get('started_at') or 0):
                latest[key] = (job, results)
    rows = []
    for (backend, model_id), (job, results) in latest.items():
        effective = {}
        for result in results:
            check = (result.get('name'), bool(result.get('stream')))
            if check not in effective or result.get('attempt', 1) >= effective[check].get('attempt', 1):
                effective[check] = result
        checks = list(effective.values())
        total = len(checks)
        passed = sum(bool(result.get('passed')) for result in checks)
        skipped = sum(bool(result.get('skipped')) for result in checks)
        failed = total - passed - skipped
        attempted = total - skipped
        pass_rate = round(passed / attempted * 100, 1) if attempted else 0
        reasoning = [r for r in checks
                     if r.get('name') in REASONING_CHECKS and not r.get('skipped')]
        reasoning_score = (round(sum(bool(r.get('passed')) for r in reasoning) / len(reasoning) * 100, 1)
                           if reasoning else None)
        timed = [r for r in checks if isinstance(r.get('seconds'), (int, float)) and r['seconds'] > 0]
        avg_seconds = round(sum(r['seconds'] for r in timed) / len(timed), 2) if timed else None
        measured = [r for r in timed if isinstance((r.get('usage') or {}).get('completion_tokens'), (int, float))]
        avg_tokens = (round(sum((r.get('usage') or {}).get('completion_tokens', 0) for r in measured) / len(measured), 1)
                      if measured else None)
        tok_s = (round(sum((r.get('usage') or {}).get('completion_tokens', 0) for r in measured) /
                       sum(r['seconds'] for r in measured), 1) if measured and sum(r['seconds'] for r in measured) > 0 else None)
        rows.append({'backend': backend, 'model': model_id, 'total': total,
                     'passed': passed, 'failed': failed, 'skipped': skipped,
                     'pass_rate': pass_rate, 'avg_seconds': avg_seconds,
                     'avg_completion_tokens': avg_tokens, 'tok_s': tok_s,
                     'reasoning_score': reasoning_score,
                     'run_id': job.get('id'),
                     'run_date': job.get('started_at')})
    # Relative 0–100 scores make the unlike units comparable. Evaluation quality
    # dominates; throughput and token use can only provide a modest tie-break.
    dimensions = [('pass_rate', True, .7), ('reasoning_score', True, .2),
                  ('tok_s', True, .05), ('avg_completion_tokens', False, .05)]
    for row in rows:
        available = [(key, higher, weight) for key, higher, weight in dimensions if row[key] is not None]
        total_weight = sum(weight for _, _, weight in available)
        row['score'] = 0
        if total_weight:
            score = 0
            for key, higher, weight in available:
                values = [item[key] for item in rows if item[key] is not None]
                low, high, value = min(values), max(values), row[key]
                relative = 100 if low == high else (100 * (value - low) / (high - low) if higher else 100 * (high - value) / (high - low))
                score += relative * weight / total_weight
            row['score'] = round(score, 1)
    rows.sort(key=lambda row: (-row['score'], -row['pass_rate']))
    return {'rows': rows, 'recommended': rows[0] if rows else None}


class Registry:
    def __init__(self, directory=None):
        self.database_backed = directory is None
        self.directory = Path(directory) if directory is not None else None
        self.lock = threading.RLock()
        self.jobs = {}
        self.cancels = {}
        if self.database_backed:
            model_metadata_store.import_test_runs(database.DATA_DIR / 'model-test-runs')
            self._refresh_from_disk()
            return
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        paths = sorted(self.directory.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in paths[30:]:
            old.unlink(missing_ok=True)
        for path in paths[:30]:
            try:
                job = json.loads(path.read_text())
                repairs_changed = self._refresh_repair_counts(job)
                interrupted = False
                if job['status'] in ACTIVE:
                    try:
                        pid = job.get('owner_pid')
                        if not isinstance(pid, int) or pid <= 0:
                            raise ProcessLookupError()
                        os.kill(pid, 0)
                    except (ProcessLookupError, PermissionError):
                        job.update(status='interrupted', finished_at=time.time(), error='Hearth restarted; start a new run to continue.')
                        interrupted = True
                        self.save(job)
                self.jobs[job['id']] = job
                if repairs_changed and not interrupted:
                    self.save(job)
            except (ValueError, KeyError, OSError):
                continue

    @staticmethod
    def _refresh_repair_counts(job):
        """Correct saved repair summaries that counted blocked workflow steps."""
        changed = False
        for repair in job.get('repairs', []):
            attempts = [result for result in job.get('results', [])
                        if result.get('backend') == repair.get('backend')
                        and result.get('model') == repair.get('model')]
            if not any(result.get('attempt') == 2 for result in attempts):
                continue
            failed_by_attempt = {
                attempt: {(result['name'], bool(result.get('stream', False)))
                          for result in attempts
                          if result.get('attempt') == attempt
                          and not result.get('passed') and not result.get('skipped')
                          and not result.get('diagnostic')}
                for attempt in (1, 2)
            }
            committed = repair.get('outcome') == 'Applied and verified'
            fixed = len(failed_by_attempt[1] - failed_by_attempt[2]) if committed else 0
            remaining = len(failed_by_attempt[2])
            if repair.get('fixed') != fixed or repair.get('remaining') != remaining:
                repair['fixed'], repair['remaining'] = fixed, remaining
                changed = True
        return changed

    def save(self, job):
        if self.database_backed:
            model_metadata_store.save_test_run(job)
            return
        path = self.directory / (job['id'] + '.json')
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(job, indent=2))
        temporary.replace(path)

    def _refresh_from_disk(self):
        """Merge recent saved jobs so the UI sees runs owned by another Hearth process."""
        if self.database_backed:
            present = {}
            for job in model_metadata_store.list_test_runs(limit=30):
                repairs_changed = self._refresh_repair_counts(job)
                if job.get('status') in ACTIVE:
                    try:
                        pid = job.get('owner_pid')
                        if not isinstance(pid, int) or pid <= 0:
                            raise ProcessLookupError()
                        os.kill(pid, 0)
                    except (ProcessLookupError, PermissionError):
                        job.update(status='interrupted', finished_at=time.time(),
                                   error='Hearth process ended; start a new run to continue.')
                        repairs_changed = True
                present[job['id']] = job
                if repairs_changed:
                    self.save(job)
            self.jobs = present
            return
        paths = sorted(self.directory.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:30]
        present = set()
        for path in paths:
            try:
                job = json.loads(path.read_text())
                job_id = job['id']
                present.add(job_id)
                if job.get('status') in ACTIVE:
                    try:
                        pid = job.get('owner_pid')
                        if not isinstance(pid, int) or pid <= 0:
                            raise ProcessLookupError()
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        job.update(status='interrupted', finished_at=time.time(),
                                   error='Hearth process ended; start a new run to continue.')
                        self.save(job)
                self.jobs[job_id] = job
            except (ValueError, KeyError, OSError):
                continue
        for job_id in list(self.jobs):
            if job_id not in present:
                self.jobs.pop(job_id, None)

    def list(self):
        with self.lock:
            self._refresh_from_disk()
            return copy.deepcopy(sorted(self.jobs.values(), key=lambda j: j['started_at'], reverse=True)[:30])

    def get(self, job_id):
        with self.lock:
            self._refresh_from_disk()
            return copy.deepcopy(self.jobs.get(job_id))

    def cancel(self, job_id):
        with self.lock:
            self._refresh_from_disk()
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
            self._refresh_from_disk()
            job = self.jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            if job['status'] in ACTIVE:
                raise ValueError('Cancel the active run and wait for it to finish before deleting its report.')
            self.jobs.pop(job_id)
            self.cancels.pop(job_id, None)
            if self.database_backed:
                model_metadata_store.delete_test_run(job_id)
            else:
                (self.directory / (job_id + '.json')).unlink(missing_ok=True)
            return True

    def start(self, targets, repair, load, connection, context, *, suite='all', background=True):
        with self.lock:
            self._refresh_from_disk()
            if any(j['status'] in ACTIVE for j in self.jobs.values()):
                raise ValueError('A model test run is already active. Wait or cancel it first.')
            job = {'id': uuid.uuid4().hex, 'status': 'loading', 'owner_pid': os.getpid(), 'started_at': time.time(),
                   'finished_at': None, 'auto_repair': repair, 'suite': suite, 'targets': copy.deepcopy(targets),
                   'results': [], 'repairs': [], 'current_model': None, 'current_check': None, 'error': None}
            self.jobs[job['id']] = job
            cancel = self.cancels[job['id']] = threading.Event()
            self.save(job)
            # Retain only bounded history; preserve the new active job.
            for old in sorted(self.jobs.values(), key=lambda j: j['started_at'], reverse=True)[30:]:
                self.jobs.pop(old['id'], None)
                self.cancels.pop(old['id'], None)
                if not self.database_backed:
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
                        results = run_suite(served_model, url, suite=suite, context=window, headers=headers,
                            cancel=cancel, on_result=record, on_case=lambda case: update(current_check=case),
                            guarded=backend == 'fastflowlm', backend=backend,
                            perf_url=kobold_client.base_url() if backend == 'kobold' else None,
                            large_input_context=4096 if backend == 'q38rocm' else None,
                            skip_context_overflow=backend == 'q38rocm',
                            profile=lambda body: profiles.apply(body, backend))
                        # A failed workflow step marks its dependents as skipped. Those
                        # blocked steps are visible in the report, but are not checks
                        # that a compatibility repair can fix or should count against.
                        failures = {(r['name'], r.get('stream', False)) for r in results
                                    if not r['passed'] and not r.get('skipped')
                                    and not r.get('diagnostic')}
                        if repair and suite in ('all', 'complete', 'core', 'opencode', 'extra') and failures:
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
                                retried = run_suite(served_model, url, suite=suite, context=window, headers=headers,
                                    cancel=cancel, on_result=lambda r: record(r, 2), on_case=lambda case: update(current_check=case),
                                    guarded=backend == 'fastflowlm', backend=backend,
                                    perf_url=kobold_client.base_url() if backend == 'kobold' else None,
                                    large_input_context=4096 if backend == 'q38rocm' else None,
                                    skip_context_overflow=backend == 'q38rocm',
                                    profile=lambda body: profiles.apply(
                                        profiles.apply_recipe(body, profiles.RECIPE), backend))
                                # Keep a recipe only when at least one original assertion now passes.
                                remaining = {(r['name'], r.get('stream', False)) for r in retried
                                             if not r['passed'] and not r.get('skipped')
                                             and not r.get('diagnostic')}
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
