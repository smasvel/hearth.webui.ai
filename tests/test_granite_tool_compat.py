import contextlib
import copy
import json
import unittest
from unittest.mock import Mock, patch

import granite_tool_compat as compat
import lemonade_guard_shim as shim
from tools.opencode_scenarios import TOOLS, question
from tools.test_flm_model import decode


ARGS = question('Which features?', 'Features', [('API', 'HTTP'), ('CLI', 'Terminal')], True)
BODY = {'model': 'granite-4.0-h-tiny-GGUF', 'tools': TOOLS,
        'messages': [{'role': 'user', 'content': 'Ask which features to build.'}]}


def payload(arguments=ARGS, name='question', finish='stop'):
    markup = '<tool_call>\n' + json.dumps({'name': name, 'arguments': json.dumps(arguments)}) + '\n</tool_call>'
    return {'id': 'upstream', 'choices': [{'index': 0, 'message': {'content': markup},
            'finish_reason': finish}], 'usage': {'completion_tokens': 140}}


class GraniteToolTests(unittest.TestCase):
    def test_transport_uses_template_tools_and_preserves_client_preferences(self):
        original = copy.deepcopy(BODY)
        request = {**BODY, 'temperature': .2, 'max_tokens': 900, 'stream': True,
                   'chat_template_kwargs': {'enable_thinking': False}}
        prepared = compat.transport(request)
        self.assertNotIn('tools', prepared)
        self.assertEqual(prepared['chat_template_kwargs']['available_tools'], TOOLS)
        for key in ('temperature', 'max_tokens', 'stream'):
            self.assertEqual(prepared[key], request[key])
        self.assertEqual(prepared['messages'][1:], request['messages'])
        self.assertFalse(prepared['chat_template_kwargs']['enable_thinking'])
        self.assertEqual(BODY, original)
        for body in ({**BODY, 'model': 'Qwen3'}, {**BODY, 'tool_choice': 'none'},
                     {**BODY, 'tools': []}, {**BODY, 'n': 2}):
            self.assertIs(compat.transport(body), body)

    def test_named_and_required_choices_are_preserved_as_instructions(self):
        for choice, text in [('required', 'at least one'),
                             ({'type': 'function', 'function': {'name': 'question'}}, 'function question')]:
            prepared = compat.transport({**BODY, 'tool_choice': choice})
            self.assertIn(text, prepared['messages'][0]['content'])
            self.assertEqual(prepared['messages'][0]['role'], 'system')

    def test_decodes_real_arguments_without_filling_missing_fields(self):
        for args in (ARGS, {'questions': []}):
            response = payload(args)
            self.assertTrue(compat.repair_completion(response, BODY))
            call = response['choices'][0]['message']['tool_calls'][0]
            self.assertEqual(json.loads(call['function']['arguments']), args)
            self.assertEqual(response['usage']['completion_tokens'], 140)
            self.assertEqual(response['id'], 'upstream')
            self.assertFalse(compat.repair_completion(response, BODY))

    def test_does_not_convert_unknown_truncated_or_disallowed_calls(self):
        for response, body in [(payload(name='unknown'), BODY), (payload(finish='length'), BODY),
                               (payload(), {**BODY, 'tool_choice': 'none'})]:
            original = copy.deepcopy(response)
            self.assertFalse(compat.repair_completion(response, body))
            self.assertEqual(response, original)
        for args in ('not JSON', '[]', '{"missing":', 12, ['unexpected']):
            response = payload(args)
            original = copy.deepcopy(response)
            self.assertFalse(compat.repair_completion(response, BODY))
            self.assertEqual(response, original)

    def test_multiple_calls_preserve_order_values_and_unique_ids(self):
        args = {'filePath': '/workspace/a.py', 'offset': 21, 'limit': 20}
        response = payload(args, 'read')
        message = response['choices'][0]['message']
        message['content'] += '\n' + payload({'filePath': '/workspace/b.py'}, 'read')['choices'][0]['message']['content']
        self.assertTrue(compat.repair_completion(response, BODY))
        calls = message['tool_calls']
        self.assertEqual(json.loads(calls[0]['function']['arguments']), args)
        self.assertEqual(json.loads(calls[1]['function']['arguments']), {'filePath': '/workspace/b.py'})
        self.assertNotEqual(calls[0]['id'], calls[1]['id'])

    def test_redundant_terminal_closer_preserves_literal_argument_bytes(self):
        args = {'path': r'C:\\example\\quoted"file', 'content': 'literal \\n and "quotes"\n'}
        response = payload(args, 'write')
        message = response['choices'][0]['message']
        message['content'] = message['content'].replace('\n</tool_call>', '}\n</tool_call>')
        self.assertTrue(compat.repair_completion(response, BODY))
        self.assertEqual(json.loads(message['tool_calls'][0]['function']['arguments']), args)

    def test_proxy_recovers_native_markup_in_both_modes_and_retains_usage(self):
        with contextlib.ExitStack() as stack:
            for name in ('_unload_before_implicit_load', 'live_report', '_record_chat_metric'):
                stack.enter_context(patch.object(shim, name))
            stack.enter_context(patch.object(shim, '_is_npu_model', return_value=False))
            stack.enter_context(patch.object(shim.model_test_profiles, 'apply', side_effect=lambda b, _: b))
            post = stack.enter_context(patch.object(shim.requests, 'post'))
            for stream in (False, True):
                upstream = Mock(status_code=200, ok=True)
                response = payload()
                upstream.json.return_value = response
                upstream.content = json.dumps(response).encode()
                upstream.headers = {'content-type': 'text/event-stream' if stream else 'application/json'}
                raw = response['choices'][0]['message']['content']
                events = [{'id': 'upstream', 'choices': [{'index': 0, 'delta': {'content': chunk},
                    'finish_reason': 'stop' if i else None}]} for i, chunk in enumerate([raw[:30], raw[30:]])]
                events.append({'choices': [], 'usage': response['usage']})
                upstream.iter_lines.return_value = [b'data: ' + json.dumps(e).encode() for e in events] + [b'data: [DONE]']
                post.return_value = upstream
                result = shim.app.test_client().post('/v1/chat/completions', json={**BODY, 'stream': stream})
                self.assertNotIn('tools', post.call_args.kwargs['json'])
                decoded = Mock(content=result.data) if stream else Mock()
                if not stream:
                    decoded.json.return_value = result.json
                calls, text, finishes, usage = decode(decoded, stream)
                self.assertEqual(calls[0]['arguments'], ARGS)
                self.assertEqual(text, '')
                self.assertEqual(finishes, ['tool_calls'])
                self.assertEqual(usage['completion_tokens'], 140)
                if stream:
                    self.assertEqual(compat.repair_sse(upstream.iter_lines.return_value[:-1], BODY),
                                     upstream.iter_lines.return_value[:-1])
                    truncated = [b'data: ' + json.dumps({**events[0], 'choices': [
                        {'delta': {'content': raw}, 'finish_reason': 'length'}]}).encode(), b'data: [DONE]']
                    self.assertEqual(compat.repair_sse(truncated, BODY), truncated)
