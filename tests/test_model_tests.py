import contextlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import app
import model_tests
import model_test_profiles as profiles
from tools.test_flm_model import Cancelled, decode, run_suite


TARGET = {'backend': 'fastflowlm', 'id': 'flm/qwen3-it:4b'}


def result(name, passed):
    return {'name': name, 'stream': False, 'passed': passed, 'seconds': .1}


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.registry = model_tests.Registry(self.directory)
        self.values = {}
        self.stack.enter_context(patch.object(profiles, 'get', side_effect=lambda b, m: self.values.get((b, m))))
        self.stack.enter_context(patch.object(profiles, 'set_profile', side_effect=lambda b, m, v: self.values.__setitem__((b, m), v)))
        self.loads = []

    def run_job(self, outcomes, targets=None, repair=True):
        calls = []
        def suite(model, url, **kwargs):
            calls.append(kwargs)
            values = outcomes.pop(0)
            if isinstance(values, Exception):
                raise values
            for value in values:
                kwargs['on_result'](value)
            return values
        with patch.object(model_tests, 'run_suite', side_effect=suite):
            job = self.registry.start(targets or [TARGET], repair, self.loads.append,
                lambda t: ('http://local/v1/chat/completions', t['id'], {}), lambda t: 8192,
                background=False)
        return job, calls

    def test_queue_serial_reports_survive_restart(self):
        second = {'backend': 'lmstudio', 'id': 'other'}
        job, _ = self.run_job([[result('chat', True)], [result('chat', True)]], [TARGET, second])
        self.assertEqual(self.loads, [TARGET, second])
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(len(job['results']), 2)
        restored = model_tests.Registry(self.directory).get(job['id'])
        self.assertEqual(restored, job)

    def test_registry_refreshes_live_jobs_written_by_another_process(self):
        job = {'id': 'b' * 32, 'status': 'running', 'owner_pid': os.getpid(),
               'started_at': time.time(), 'finished_at': None, 'results': []}
        Path(self.directory, job['id'] + '.json').write_text(json.dumps(job))
        other_process_view = model_tests.Registry(self.directory)
        self.assertEqual(other_process_view.get(job['id'])['status'], 'running')
        job.update(status='completed', finished_at=time.time())
        Path(self.directory, job['id'] + '.json').write_text(json.dumps(job))
        self.assertEqual(other_process_view.get(job['id'])['status'], 'completed')

    def test_verified_repair_reruns_passes_too_and_persists(self):
        job, calls = self.run_job([[result('chat', True), result('tools', False)],
                                  [result('chat', True), result('tools', True)]])
        self.assertEqual(self.values[(TARGET['backend'], TARGET['id'])], profiles.RECIPE)
        self.assertNotIn('only', calls[1])
        self.assertEqual(job['repairs'][0]['fixed'], 1)
        self.assertEqual(job['repairs'][0]['remaining'], 0)
        self.assertEqual([r['attempt'] for r in job['results']], [1, 1, 2, 2])

    def test_candidate_is_not_published_before_verification(self):
        count = []
        def suite(model, url, **kwargs):
            count.append(1)
            self.assertIsNone(self.values.get((TARGET['backend'], TARGET['id'])))
            if len(count) == 2:
                packet = kwargs['profile']({'model':model, 'tools':[{'function':{'name':'read'}}], 'messages':[]})
                self.assertIn('Tool contract', packet['messages'][0]['content'])
            item = result('tools', len(count) == 2)
            kwargs['on_result'](item)
            return [item]
        with patch.object(model_tests, 'run_suite', side_effect=suite):
            job = self.registry.start([TARGET], True, Mock(),
                lambda t: ('http://local', t['id'], {}), lambda t: 8192, background=False)
        self.assertEqual(job['repairs'][0]['fixed'], 1)
        self.assertEqual(self.values[(TARGET['backend'], TARGET['id'])], profiles.RECIPE)

    def test_repair_regression_rolls_back_and_marks_attempts(self):
        job, _ = self.run_job([[result('chat', True), result('tools', False)],
                               [result('chat', False), result('tools', True)]])
        self.assertIsNone(self.values[(TARGET['backend'], TARGET['id'])])
        self.assertTrue(all(r['rolled_back'] for r in job['results'] if r['attempt'] == 2))
        self.assertEqual(job['repairs'][0]['fixed'], 0)

    def test_no_improvement_rolls_back(self):
        job, _ = self.run_job([[result('tools', False)], [result('tools', False)]])
        self.assertIsNone(self.values[(TARGET['backend'], TARGET['id'])])
        self.assertIn('rolled back', job['repairs'][0]['outcome'])

    def test_blocked_workflow_steps_do_not_count_as_repair_failures(self):
        blocked = {**result('dependent', False), 'skipped': True, 'blocked_by': 'root'}
        job, calls = self.run_job(
            [[result('root', False), blocked], [result('root', True), blocked.copy()]],
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(job['repairs'][0]['fixed'], 1)
        self.assertEqual(job['repairs'][0]['remaining'], 0)
        self.assertEqual(self.values[(TARGET['backend'], TARGET['id'])], profiles.RECIPE)

    def test_all_blocked_results_do_not_trigger_repair(self):
        blocked = {**result('dependent', False), 'skipped': True, 'blocked_by': 'root'}
        job, calls = self.run_job([[blocked]])
        self.assertEqual(len(calls), 1)
        self.assertEqual(job['repairs'], [])
        self.assertFalse(self.values)

    def test_unavailable_backend_is_not_retried_with_a_prompt_recipe(self):
        failure = {**result('plain-chat', False), 'backend_unavailable': True}
        job, calls = self.run_job([[failure]])
        self.assertEqual(len(calls), 1)
        self.assertEqual(job['status'], 'completed')
        self.assertIn('restore the runtime', job['repairs'][0]['outcome'])
        self.assertFalse(self.values)

    def test_cancel_during_repair_restores_previous_profile(self):
        job, _ = self.run_job([[result('tools', False)], Cancelled()])
        self.assertEqual(job['status'], 'cancelled')
        self.assertIsNone(self.values[(TARGET['backend'], TARGET['id'])])

    def test_restart_marks_unfinished_job_interrupted(self):
        job = {'id': 'a' * 32, 'status': 'running', 'started_at': 1}
        Path(self.directory, job['id'] + '.json').write_text(json.dumps(job))
        restored = model_tests.Registry(self.directory).get(job['id'])
        self.assertEqual(restored['status'], 'interrupted')
        self.assertIn('restarted', restored['error'])

    def test_restart_corrects_saved_repair_counts_excluding_blocked_checks(self):
        job_id = 'b' * 32
        saved = {'id': job_id, 'status': 'completed', 'started_at': 1, 'targets': [TARGET],
            'results': [
                {**result('root', False), 'backend': TARGET['backend'], 'model': TARGET['id'], 'attempt': 1},
                {**result('dependent', False), 'backend': TARGET['backend'], 'model': TARGET['id'], 'attempt': 1, 'skipped': True},
                {**result('root', False), 'backend': TARGET['backend'], 'model': TARGET['id'], 'attempt': 2},
                {**result('dependent', False), 'backend': TARGET['backend'], 'model': TARGET['id'], 'attempt': 2, 'skipped': True},
            ],
            'repairs': [{'backend': TARGET['backend'], 'model': TARGET['id'],
                'outcome': 'No improvement; rolled back', 'fixed': 0, 'remaining': 2}],
        }
        path = Path(self.directory, job_id + '.json')
        path.write_text(json.dumps(saved))
        restored = model_tests.Registry(self.directory).get(job_id)
        self.assertEqual(restored['repairs'][0]['remaining'], 1)
        self.assertEqual(restored['repairs'][0]['fixed'], 0)
        self.assertEqual(json.loads(path.read_text())['repairs'][0]['remaining'], 1)

    def test_delete_saved_run_removes_memory_and_report(self):
        job, _ = self.run_job([[result('chat', True)]], repair=False)
        self.assertTrue(self.registry.delete(job['id']))
        self.assertIsNone(self.registry.get(job['id']))
        self.assertFalse(Path(self.directory, job['id'] + '.json').exists())

    def test_active_run_cannot_be_deleted(self):
        gate = threading.Event()
        with patch.object(model_tests, 'run_suite', side_effect=lambda *a, **kw: (gate.wait(2), [] )[1]):
            job = self.registry.start([TARGET], False, Mock(), lambda t: ('http://local', t['id'], {}),
                lambda t: 8192, background=True)
            deadline = time.monotonic() + 2
            while self.registry.get(job['id'])['status'] not in model_tests.ACTIVE and time.monotonic() < deadline:
                time.sleep(.01)
            with self.assertRaises(ValueError):
                self.registry.delete(job['id'])
            gate.set()
            deadline = time.monotonic() + 3
            while self.registry.get(job['id'])['status'] in model_tests.ACTIVE and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(self.registry.get(job['id'])['status'], 'completed')

    def test_inspection_does_not_interrupt_a_live_owner(self):
        job = {'id': 'b' * 32, 'status': 'running', 'started_at': 1, 'owner_pid': os.getpid()}
        path = Path(self.directory, job['id'] + '.json')
        path.write_text(json.dumps(job))
        restored = model_tests.Registry(self.directory).get(job['id'])
        self.assertEqual(restored['status'], 'running')
        self.assertEqual(json.loads(path.read_text()), job)

    def test_setup_error_belongs_to_correct_target(self):
        def load(target):
            if target['id'] == 'bad':
                raise RuntimeError('load failed')
        second = {'backend': 'lmstudio', 'id': 'bad'}
        def suite(model, url, **kwargs):
            item = result('chat', True); kwargs['on_result'](item); return [item]
        with patch.object(model_tests, 'run_suite', side_effect=suite):
            job = self.registry.start([TARGET, second], False, load,
                lambda t: ('http://local', t['id'], {}), lambda t: 8192, background=False)
        self.assertEqual(job['results'][-1]['model'], 'bad')
        self.assertEqual(job['results'][-1]['name'], 'model-setup')

    def test_concurrent_run_is_rejected_and_cancel_survives_load(self):
        gate, entered = threading.Event(), threading.Event()
        self.addCleanup(gate.set)
        def load(target):
            entered.set(); gate.wait(3)
        with patch.object(model_tests, 'run_suite', side_effect=Cancelled()):
            job = self.registry.start([TARGET], False, load, lambda t: ('http://local', t['id'], {}), lambda t: 8192)
            self.assertTrue(entered.wait(1))
            with self.assertRaises(ValueError):
                self.registry.start([TARGET], False, load, Mock(), Mock())
            self.registry.cancel(job['id']); gate.set()
            deadline = time.monotonic() + 2
            while self.registry.get(job['id'])['status'] != 'cancelled' and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(self.registry.get(job['id'])['status'], 'cancelled')


class ProfileTests(unittest.TestCase):
    @patch.object(profiles, 'get', return_value=profiles.RECIPE)
    def test_recipe_is_idempotent_and_does_not_mutate_history(self, _get):
        body = {'model': 'x', 'tools': [{'function': {'name': 'read'}}],
                'messages': [{'role': 'system', 'content': 'Preserve project instructions.'}]}
        updated = profiles.apply(body, 'fastflowlm')
        self.assertEqual(body['messages'][0]['content'], 'Preserve project instructions.')
        self.assertTrue(updated['messages'][0]['content'].startswith(body['messages'][0]['content']))
        self.assertEqual(profiles.apply(updated, 'fastflowlm'), updated)
        self.assertEqual(updated['temperature'], 0)
        self.assertIs(profiles.apply({**body, 'tool_choice': 'none'}, 'fastflowlm').get('messages'), body['messages'])

    def test_unknown_recipe_cannot_write_settings(self):
        with patch.object(profiles.settings_store, 'set') as save:
            with self.assertRaises(ValueError):
                profiles.set_profile('fastflowlm', 'x', 'execute-code')
            save.assert_not_called()

    def test_model_prefixes_share_the_same_profile(self):
        self.assertEqual(profiles.key('fastflowlm', 'flm/x'), profiles.key('fastflowlm', 'x'))
        self.assertEqual(profiles.key('sglang', 'sglang/x'), profiles.key('sglang', 'x'))


class ModelTestAPITests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.registry = model_tests.Registry(directory)
        self.stack.enter_context(patch.object(app, '_agent_tasks', Mock(
            busy=Mock(return_value=False), list=Mock(return_value=[]))))
        self.stack.enter_context(patch.object(app, '_model_test_registry', self.registry))
        self.stack.enter_context(patch.object(app, '_test_model_inventory', return_value=[TARGET, {'backend':'lmstudio','id':'embedding','type':'embeddings'}]))
        self.stack.enter_context(patch.object(app.users_store, 'get_user_by_id', side_effect=lambda uid: {'id':uid,'username':uid,'role':uid}))
        self.client = app.app.test_client()
        self.headers = {'X-CSRF-Token': 'test-token'}

    def login(self, role='admin'):
        with self.client.session_transaction() as session:
            session.update(logged_in=True, user_id=role, username=role, role=role,
                           login_csrf_token='test-token', last_activity=time.time())

    def test_admin_and_csrf_are_required(self):
        self.assertEqual(self.client.get('/api/model-tests').status_code, 401)
        self.login('user')
        self.assertEqual(self.client.get('/api/model-tests').status_code, 403)
        self.login()
        response = self.client.post('/api/model-tests', json={'models':[TARGET]})
        self.assertEqual(response.status_code, 400)
        self.assertIn('CSRF', response.json['error'])

    def test_invalid_targets_cannot_start_a_worker(self):
        self.login()
        with patch.object(self.registry, 'start') as start:
            for models in ([], [TARGET, TARGET], [{'backend':'lmstudio','id':'embedding'}],
                           [{'backend':'fastflowlm','id':'http://evil/'}], [{'backend': []}]):
                response = self.client.post('/api/model-tests', json={'models':models}, headers=self.headers)
                self.assertEqual(response.status_code, 400, response.json)
            start.assert_not_called()

    def test_start_uses_server_connections_and_does_not_accept_client_url(self):
        self.login()
        with patch.object(self.registry, 'start', return_value={'id':'test','status':'running'}) as start:
            response = self.client.post('/api/model-tests', json={'models':[TARGET], 'url':'http://evil'}, headers=self.headers)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(start.call_args.args[:2], ([TARGET], False))
            self.assertIs(start.call_args.args[3], app._test_connection)
            response = self.client.post('/api/model-tests', json={'models':[TARGET], 'auto_repair':True}, headers=self.headers)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(start.call_args.args[:2], ([TARGET], True))

    def test_report_download_and_missing_run(self):
        self.login()
        job, _ = self.fixture_job()
        response = self.client.get('/api/model-tests/' + job['id'] + '?download=1')
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment;', response.headers['Content-Disposition'])
        self.assertEqual(self.client.get('/api/model-tests/no-such-run').status_code, 404)
        summary = self.client.get('/api/model-tests').json['jobs'][0]
        self.assertNotIn('results', summary)

    def test_delete_saved_report_and_reject_active_or_missing(self):
        self.login()
        job, _ = self.fixture_job()
        response = self.client.delete('/api/model-tests/' + job['id'], headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/api/model-tests/' + job['id']).status_code, 404)
        self.assertEqual(self.client.delete('/api/model-tests/no-such-run', headers=self.headers).status_code, 404)
        with patch.object(self.registry, 'delete', side_effect=ValueError('Active run')):
            self.assertEqual(self.client.delete('/api/model-tests/' + job['id'], headers=self.headers).status_code, 409)

    def fixture_job(self):
        with patch.object(model_tests, 'run_suite', return_value=[]):
            job = self.registry.start([TARGET], False, Mock(), lambda t: ('http://local', t['id'], {}), lambda t: 8192, background=False)
        return job, None

    def test_model_management_is_blocked_during_validation(self):
        self.login()
        with patch.object(self.registry, 'list', return_value=[{'status':'running', 'targets':[TARGET]}]):
            for endpoint in ('load','unload','delete'):
                response = self.client.post('/api/models/' + endpoint,
                                            json={'model':TARGET['id'], 'identifier':TARGET['id']}, headers=self.headers)
                self.assertEqual(response.status_code, 409)

    def test_reset_profile_is_reversible_and_blocked_during_run(self):
        self.login()
        with patch.object(app.model_test_profiles, 'set_profile') as reset:
            response = self.client.post('/api/model-tests/reset-profile', json={'backend':TARGET['backend'],'model':TARGET['id']}, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            reset.assert_called_once_with(TARGET['backend'], TARGET['id'], None)
            with patch.object(self.registry, 'list', return_value=[{'status':'running'}]):
                self.assertEqual(self.client.post('/api/model-tests/reset-profile', json={'backend':TARGET['backend'],'model':TARGET['id']}, headers=self.headers).status_code, 409)


class ModelHandoffTests(unittest.TestCase):
    def test_batch_handoff_reuses_npu_lock_without_manual_pause(self):
        with contextlib.ExitStack() as stack:
            directory = stack.enter_context(tempfile.TemporaryDirectory())
            root = Path(directory)
            stack.enter_context(patch.object(app.npu_request_lock, '_LOCK_PATH', root / 'npu.lock'))
            stack.enter_context(patch.object(app.fastflowlm, '_PAUSED_FILE', root / 'paused'))
            stack.enter_context(patch.object(app.fastflowlm, '_systemctl'))
            stack.enter_context(patch.object(app.fastflowlm, '_write_active'))
            for client in (app.lms, app.lemonade, app.vllm, app.kobold):
                stack.enter_context(patch.object(client, 'list_models', return_value=[]))
            stack.enter_context(patch.object(app.fastflowlm, 'list_models', return_value=[
                {'id':'flm/previous','state':'loaded','type':'llm'}]))
            held = app.npu_request_lock.acquire(timeout=0)
            try:
                unloaded = app._unload_reasoning_models_before_load('flm/next', npu_lock_held=True)
            finally:
                app.npu_request_lock.release(held)
            self.assertEqual(unloaded, ['flm/previous'])
            self.assertFalse(app.fastflowlm.requests_paused())

    def test_busy_automatic_handoff_never_sets_manual_pause(self):
        with contextlib.ExitStack() as stack:
            directory = stack.enter_context(tempfile.TemporaryDirectory())
            stack.enter_context(patch.object(app.fastflowlm, '_PAUSED_FILE', Path(directory) / 'paused'))
            for client in (app.lms, app.lemonade, app.vllm, app.kobold):
                stack.enter_context(patch.object(client, 'list_models', return_value=[]))
            stack.enter_context(patch.object(app.fastflowlm, 'list_models', return_value=[
                {'id':'flm/previous','state':'loaded','type':'llm'}]))
            stack.enter_context(patch.object(app.npu_request_lock, 'acquire', side_effect=TimeoutError('NPU busy')))
            with self.assertRaises(RuntimeError):
                app._unload_reasoning_models_before_load('flm/next')
            self.assertFalse(app.fastflowlm.requests_paused())


class SuiteTests(unittest.TestCase):
    def test_tool_check_budget_and_truncation_diagnostics(self):
        response = Mock()
        response.json.return_value = {'choices': [{'message': {'tool_calls': [
            {'id': 'edit-1', 'function': {'name': 'edit', 'arguments': '{"path":"src/example.py"'}}
        ]}, 'finish_reason': 'length'}], 'usage': {'completion_tokens': 2048}}
        with patch('tools.test_flm_model.requests.post', return_value=response) as post:
            results = run_suite('x', 'http://local', suite='core',
                                only={('file-edit-contract', False)})
        self.assertEqual(post.call_args.kwargs['json']['max_tokens'], 2048)
        self.assertFalse(results[0]['passed'])
        self.assertIn("finish reasons=['length']", results[0]['error'])
        self.assertIn('completion tokens=2048', results[0]['error'])
        self.assertIn('src/example.py', results[0]['error'])

    def test_cancelled_suite_makes_no_network_request(self):
        event = threading.Event(); event.set()
        with patch('tools.test_flm_model.requests.post') as post:
            with self.assertRaises(Cancelled):
                run_suite('x', 'http://local', cancel=event)
            post.assert_not_called()

    def test_callback_receives_failures_and_auth_headers(self):
        response = Mock(); response.raise_for_status.side_effect = RuntimeError('backend unavailable')
        observed, cases = [], []
        with patch('tools.test_flm_model.requests.post', return_value=response) as post:
            results = run_suite('x', 'http://local', suite='core', only={('plain-chat',False)},
                                on_result=observed.append, on_case=cases.append, headers={'Authorization':'Bearer test-only'})
        self.assertEqual(results, observed)
        self.assertEqual(cases[0]['name'], 'plain-chat')
        self.assertGreater(cases[0]['started_at'], 0)
        self.assertFalse(results[0]['passed'])
        self.assertEqual(post.call_args.kwargs['headers'], {'Authorization':'Bearer test-only'})

    def test_decode_joins_tool_argument_fragments(self):
        response = Mock(text='data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"read","arguments":"{\\"path\\":"}}]}}]}\n\ndata: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"README.md\\"}"}}]},"finish_reason":"tool_calls"}]}\n\ndata: [DONE]\n\n')
        calls, _, finishes, _ = decode(response, True)
        self.assertEqual(calls[0]['arguments'], {'path':'README.md'})
        self.assertEqual(finishes, ['tool_calls'])


class ModelHandoffVerificationTests(unittest.TestCase):
    def test_tests_start_only_after_exclusive_requested_model_is_loaded(self):
        target = {'backend': 'lemonade', 'id': 'target'}
        good = {'backend': 'lemonade', 'id': 'target', 'state': 'loaded', 'type': 'llm'}
        helper = {'backend': 'lemonade', 'id': 'embedding', 'state': 'loaded', 'type': 'embeddings'}
        wrong = {'backend': 'lmstudio', 'id': 'old', 'state': 'loaded', 'type': 'llm'}
        with patch.object(app, 'api_load_model', Mock(__wrapped__=lambda: app.jsonify({'ok': True}))), \
             patch.object(app, '_test_model_inventory', return_value=[good, helper]):
            app._test_load_model(target)
        for inventory in ([], [wrong], [good, wrong]):
            with self.subTest(inventory=inventory), \
                 patch.object(app, 'api_load_model', Mock(__wrapped__=lambda: app.jsonify({'ok': True}))), \
                 patch.object(app, '_test_model_inventory', return_value=inventory):
                with self.assertRaisesRegex(RuntimeError, 'handoff verification failed'):
                    app._test_load_model(target)
                self.assertFalse(app._model_test_context.active)
