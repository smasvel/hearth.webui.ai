"""Exercise the workflow harness, not a mocked model's coding ability."""
import copy
import json
import threading
import unittest
import requests
from unittest.mock import Mock, patch

from tools.opencode_scenarios import TOOLS, scenarios
from tools.test_flm_model import Cancelled, decode, run_suite, reports_passed_tests


def response_for(step, stream, serial):
    calls = [{'index': i, 'id': f'actual_{serial}_{i}', 'type': 'function',
              'function': {'name': name, 'arguments': json.dumps(args)}}
             for i, (name, args) in enumerate(step['expected'])]
    content = step.get('contains', f"{step['passed_tests']} tests passed" if 'passed_tests' in step else '')
    finish = 'tool_calls' if calls else 'stop'
    message = {'role': 'assistant', 'content': content}
    if calls:
        message['tool_calls'] = calls
    response = Mock()
    if stream:
        response.text = 'data: ' + json.dumps({'choices': [{'delta': message, 'finish_reason': finish}]}) + '\n\ndata: [DONE]\n\n'
    else:
        response.json.return_value = {'choices': [{'message': message, 'finish_reason': finish}]}
    return response


class WorkflowTests(unittest.TestCase):
    def test_verified_test_count_accepts_wording_but_rejects_wrong_or_negated_counts(self):
        for text in ('4 passed', '4 tests passed', '4 tests passing (exit code 0)', 'Passed 4 tests.'):
            self.assertTrue(reports_passed_tests(text, 4), text)
        for text in ('14 passed', '3 tests passed', 'Not 4 passed', 'Never passed 4 tests',
                     'Tests have not run', '4 tests failed', '4 passed; actually 3 passed', '4 passed, 1 failed'):
            self.assertFalse(reports_passed_tests(text, 4), text)

    def test_named_choice_accepts_vllm_stop_only_with_validated_call(self):
        response = Mock()
        response.json.return_value = {'choices': [{'message': {'tool_calls': [
            {'id': 'real', 'function': {'name': 'get_weather', 'arguments': '{"city":"Boston"}'}}]},
            'finish_reason': 'stop'}]}
        named = "tool-choice-{'type': 'function', 'function': {'name': 'get_weather'}}"
        with patch('tools.test_flm_model.requests.post', return_value=response):
            results = run_suite('fixture', 'http://local', suite='core',
                only={(named, False), ('tool-choice-auto', False)})
        self.assertFalse(results[0]['passed'])
        self.assertTrue(results[1]['passed'])

    def test_backend_timeout_does_not_repeat_five_minute_requests(self):
        with patch('tools.test_flm_model.requests.post', side_effect=requests.ReadTimeout('stalled')) as post:
            results = run_suite('fixture', 'http://local', suite='all')
        self.assertEqual(post.call_count, 1)
        self.assertFalse(results[0]['passed'])
        self.assertTrue(all(not r['passed'] and r.get('skipped') for r in results[1:]))

    def test_sse_decodes_utf8_without_splitting_unicode_json_content(self):
        response = Mock()
        text = '✓\u0085\u2028done'
        payload = {'choices': [{'delta': {'content': text}, 'finish_reason': 'stop'}]}
        response.content = ('data: ' + json.dumps(payload, ensure_ascii=False) + '\n\ndata: [DONE]\n\n').encode('utf-8')
        response.text = response.content.decode('latin1')
        calls, content, finishes, _ = decode(response, True)
        self.assertEqual(content, text)
        self.assertEqual(calls, [])
        self.assertEqual(finishes, ['stop'])

    def test_large_code_allows_slow_cpu_prefill(self):
        response = Mock()
        response.json.return_value = {'choices': [{'message': {'content': 'HEARTH_CONTEXT_OK_5829'}, 'finish_reason': 'stop'}]}
        with patch('tools.test_flm_model.requests.post', return_value=response) as post:
            results = run_suite('fixture', 'http://local', suite='core', only={('large-code-input', False)})
        self.assertTrue(results[0]['passed'])
        self.assertEqual(post.call_args.kwargs['timeout'], (10, 900))

    def test_all_workflows_replay_actual_ids_and_results_in_both_modes(self):
        requests_seen, fixtures = [], []
        for stream in (False, True):
            for name, steps in scenarios():
                for step in steps:
                    fixtures.append((stream, name, step))
        def post(url, **kwargs):
            body = copy.deepcopy(kwargs['json'])
            stream, workflow, step = fixtures[len(requests_seen)]
            self.assertEqual(body['stream'], stream)
            requests_seen.append(body)
            return response_for(step, stream, len(requests_seen))
        observed = []
        with patch('tools.test_flm_model.requests.post', side_effect=post):
            results = run_suite('fixture', 'http://local', suite='opencode', on_result=observed.append)
        self.assertEqual(results, observed)
        self.assertEqual(len(results), len(fixtures))
        self.assertTrue(all(r['passed'] for r in results))
        for index, (body, (_, workflow, step)) in enumerate(zip(requests_seen, fixtures)):
            self.assertEqual(body['tools'], TOOLS)
            self.assertNotIn('blocked_by', body)
            self.assertNotIn('workflow', body)
            self.assertNotIn('step', body)
            messages = body['messages']
            ids = {call['id'] for m in messages for call in m.get('tool_calls', [])}
            for message in messages:
                if message['role'] == 'tool':
                    self.assertIn(message['tool_call_id'], ids)
                    self.assertTrue(message['tool_call_id'].startswith('actual_'))
            if step['name'] == 'resume' and workflow == 'multiple-choice':
                outputs = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
                self.assertEqual(outputs[-1]['answers'], [['API', 'CLI']])
            if workflow == 'build' and step['name'] == 'finish':
                self.assertTrue(any('4 passed' in m.get('content', '') for m in messages if m['role'] == 'tool'))
                self.assertGreater(len(messages), 35)
            if workflow == 'parallel-read' and step['name'] == 'resume':
                self.assertEqual(len([m for m in messages if m['role'] == 'tool']), 2)

    def test_failure_blocks_dependent_steps_without_network_or_fake_results(self):
        response = Mock()
        response.raise_for_status.side_effect = RuntimeError('backend failed')
        with patch('tools.test_flm_model.requests.post', return_value=response) as post:
            results = run_suite('fixture', 'http://local', suite='opencode',
                only={('opencode-single-choice-ask', False)})
        self.assertEqual(post.call_count, 1)
        self.assertEqual(len(results), 2)
        self.assertFalse(results[0]['passed'])
        self.assertTrue(results[1]['skipped'])
        self.assertEqual(results[1]['blocked_by'], results[0]['name'])
        self.assertFalse(results[1]['passed'])

    def test_cancellation_after_answer_prevents_followup_request(self):
        cancel = threading.Event()
        step = dict(scenarios())['single-choice'][0]
        response = response_for(step, False, 1)
        with patch('tools.test_flm_model.requests.post', return_value=response) as post:
            with self.assertRaises(Cancelled):
                run_suite('fixture', 'http://local', suite='opencode', cancel=cancel,
                    only={('opencode-single-choice-ask', False)}, on_result=lambda r: cancel.set())
        self.assertEqual(post.call_count, 1)

    def test_filter_for_other_cases_does_not_start_workflows(self):
        with patch('tools.test_flm_model.requests.post') as post:
            self.assertEqual(run_suite('fixture', 'http://local', suite='opencode', only={('plain-chat', False)}), [])
            post.assert_not_called()
