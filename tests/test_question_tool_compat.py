import contextlib
import copy
import json
import unittest
from unittest.mock import Mock, patch

import lemonade_guard_shim as shim
import question_tool_compat as compat
from tools.opencode_scenarios import QUESTION, question
from tools.test_flm_model import decode, run_suite


ARGS = question('Install optional analytics?', 'Analytics', [('Install', 'Add analytics'),
    ('Skip (Recommended)', 'Continue without it')])
del ARGS['questions'][0]['multiple']
BODY = {'model': 'GLM-4.7-Flash-GGUF', 'tools': [QUESTION],
        'messages': [{'role': 'user', 'content': 'Ask about optional analytics.'}]}


def completion():
    return {'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': 'actual-question-id', 'type': 'function', 'function': {'name': 'question',
         'arguments': json.dumps(ARGS)}}]}, 'finish_reason': 'tool_calls'}], 'usage': {'completion_tokens': 80}}


def events():
    raw = json.dumps(ARGS)
    return [{'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0,
        'id': 'actual-question-id', 'function': {'name': 'question', 'arguments': raw[:21]}}]}}]},
        {'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0,
        'function': {'arguments': raw[21:]}}]}, 'finish_reason': 'tool_calls'}], 'usage': {'completion_tokens': 80}}]


class QuestionCompatTests(unittest.TestCase):
    def test_explicit_option_order_restored_in_completion_and_stream(self):
        desired = question('Which interface?', 'Interface', [('CLI', 'Terminal'), ('API', 'HTTP')])
        generated = copy.deepcopy(desired)
        generated['questions'][0]['options'].reverse()
        body = {**BODY, 'messages': [{'role': 'user', 'content': 'Call question with exactly: ' + json.dumps(desired)}]}
        payload = completion()
        payload['choices'][0]['message']['tool_calls'][0]['function']['arguments'] = json.dumps(generated)
        self.assertTrue(compat.repair_completion(payload, body))
        self.assertEqual(json.loads(payload['choices'][0]['message']['tool_calls'][0]['function']['arguments']), desired)
        raw = json.dumps(generated)
        chunks = [raw[:30], raw[30:]]
        lines = [b'data: ' + json.dumps({'choices': [{'index': 0, 'delta': {'tool_calls': [
            {'index': 0, 'id': 'original-id', 'function': {'name': 'question' if i == 0 else '', 'arguments': chunk}}]},
            'finish_reason': 'tool_calls' if i else None}]}).encode() + b'\n' for i, chunk in enumerate(chunks)]
        lines.append(b'data: [DONE]\n')
        calls, _, _, _ = decode(Mock(text=b''.join(compat.repair_sse(lines, body)).decode()), True)
        self.assertEqual(calls[0]['arguments'], desired)
        self.assertEqual(calls[0]['id'], 'original-id')

    def test_explicit_order_does_not_repair_changed_or_missing_choices(self):
        desired = question('Which interface?', 'Interface', [('CLI', 'Terminal'), ('API', 'HTTP')])
        schema = QUESTION['function']['parameters']
        for change in ('missing', 'description', 'selection'):
            generated = copy.deepcopy(desired)
            generated['questions'][0]['options'].reverse()
            if change == 'missing':
                generated['questions'][0]['options'].pop()
            elif change == 'description':
                generated['questions'][0]['options'][0]['description'] = 'Changed'
            else:
                generated['questions'][0]['multiple'] = True
            self.assertEqual(compat.normalize(generated, schema, desired), generated)
        body = {**BODY, 'messages': [{'role': 'user', 'content': json.dumps(desired) + json.dumps(desired)}]}
        self.assertIsNone(compat._requested(body, schema))
        body['messages'].append({'role': 'user', 'content': 'Ask a different question'})
        self.assertIsNone(compat._requested(body, schema))

    def assert_question(self, args):
        q = args['questions'][0]
        self.assertIs(q['multiple'], False)
        self.assertEqual(q['options'], list(reversed(ARGS['questions'][0]['options'])))
        self.assertEqual(q['question'], ARGS['questions'][0]['question'])

    def test_optional_default_and_recommendation_preserve_content_and_input(self):
        original = copy.deepcopy(ARGS)
        schema = QUESTION['function']['parameters']
        fixed = compat.normalize(ARGS, schema)
        self.assert_question(fixed)
        self.assertEqual(ARGS, original)
        fixed['questions'][0]['multiple'] = True
        self.assertIs(compat.normalize(fixed, schema)['questions'][0]['multiple'], True)
        self.assertEqual(compat.normalize(fixed, schema), fixed)

    def test_does_not_add_undeclared_or_required_values(self):
        self.assertEqual(compat.normalize(ARGS, {}), ARGS)
        schema = copy.deepcopy(QUESTION['function']['parameters'])
        schema['properties']['questions']['items']['required'].append('multiple')
        self.assertNotIn('multiple', compat.normalize(ARGS, schema)['questions'][0])
        payload = completion()
        self.assertFalse(compat.repair_completion(payload, {'tools': []}))
        self.assertEqual(payload, completion())

    def test_open_schemas_pass_through_without_guessing_fields(self):
        for schema in (True, {}, {'properties': {'questions': True}},
                       {'properties': {'questions': {'type': 'array', 'items': True}}}):
            self.assertEqual(compat.normalize(ARGS, schema), ARGS)

    def test_fragmented_sse_retains_ids_usage_finishes_and_terminator(self):
        lines = [b'data: ' + json.dumps(event).encode() + b'\n' for event in events()] + [b'\n', b'data: [DONE]\n']
        result = compat.repair_sse(lines, BODY)
        response = Mock(text=b''.join(result).decode())
        calls, _, finishes, usage = decode(response, True)
        self.assert_question(calls[0]['arguments'])
        self.assertEqual(calls[0]['id'], 'actual-question-id')
        self.assertEqual(finishes, ['tool_calls'])
        self.assertEqual(usage['completion_tokens'], 80)
        self.assertEqual(result[-1], lines[-1])
        self.assertEqual(compat.repair_sse(result, BODY), result)

    def test_malformed_or_unrelated_streams_pass_through(self):
        for name, args in [('question', '{'), ('other', json.dumps(ARGS))]:
            event = {'choices': [{'delta': {'tool_calls': [{'function': {'name': name, 'arguments': args}}]}}]}
            lines = [b'data: ' + json.dumps(event).encode(), b'data: [DONE]']
            self.assertEqual(compat.repair_sse(lines, BODY), lines)

    def test_real_lemonade_proxy_repairs_nonstream_and_fragmented_stream(self):
        with contextlib.ExitStack() as stack:
            for name in ('_unload_before_implicit_load', 'live_report', '_record_chat_metric'):
                stack.enter_context(patch.object(shim, name))
            stack.enter_context(patch.object(shim, '_is_npu_model', return_value=False))
            stack.enter_context(patch.object(shim.model_test_profiles, 'apply', side_effect=lambda b, _: b))
            post = stack.enter_context(patch.object(shim, '_post_chat_with_utf8_recovery'))
            for stream in (False, True):
                with self.subTest(stream=stream):
                    upstream = Mock(status_code=200, ok=True)
                    upstream.headers = {'content-type': 'text/event-stream' if stream else 'application/json'}
                    upstream.json.return_value = completion()
                    upstream.content = json.dumps(completion()).encode()
                    upstream.iter_lines.return_value = [b'data: ' + json.dumps(e).encode() for e in events()] + [b'data: [DONE]']
                    post.return_value = (upstream, 0)
                    r = shim.app.test_client().post('/v1/chat/completions', json={**BODY, 'stream': stream})
                    self.assertEqual(r.status_code, 200)
                    decoded = Mock(text=r.data.decode()) if stream else Mock()
                    if not stream:
                        decoded.json.return_value = r.json
                    calls, _, _, _ = decode(decoded, stream)
                    self.assert_question(calls[0]['arguments'])

    def test_large_code_request_budget_and_failure_diagnostics(self):
        response = Mock()
        response.json.return_value = {'choices': [{'message': {'content': 'incomplete'}, 'finish_reason': 'length'}],
                                     'usage': {'completion_tokens': 1024}}
        with patch('tools.test_flm_model.requests.post', return_value=response) as post:
            results = run_suite('GLM-4.7-Flash-GGUF', 'http://fixture', suite='core', only={('large-code-input', False)})
        body = post.call_args.kwargs['json']
        self.assertEqual(body['max_tokens'], 1024)
        self.assertIs(body['chat_template_kwargs']['enable_thinking'], False)
        self.assertFalse(results[0]['passed'])
        self.assertIn('reply budget=1024', results[0]['error'])
        self.assertEqual(results[0]['usage']['completion_tokens'], 1024)
