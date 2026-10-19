import contextlib
import json
import unittest
from unittest.mock import Mock, patch

import lmstudio_shim as shim
from tools.opencode_scenarios import QUESTION
from tools.test_flm_model import decode
from tests.test_exact_file_content import request_body, payload, stream_lines, WANTED


class LMStudioContractTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name in ('live_report', '_record_chat_metric'):
            self.stack.enter_context(patch.object(shim, name))
        self.post = self.stack.enter_context(patch.object(shim.requests, 'post'))
        self.client = shim.app.test_client()

    def check_modes(self, body, completion, lines, expected):
        for streamed in (False, True):
            upstream = Mock(ok=True, status_code=200)
            upstream.headers = {'content-type': 'text/event-stream' if streamed else 'application/json'}
            upstream.json.return_value = completion
            upstream.content = json.dumps(completion).encode()
            upstream.iter_lines.return_value = [line.rstrip(b'\n') for line in lines]
            self.post.return_value = upstream
            r = self.client.post('/v1/chat/completions', json={**body, 'stream': streamed})
            self.assertEqual(r.status_code, 200)
            response = Mock(content=r.data)
            response.json.return_value = r.get_json() if not streamed else None
            calls, _, _, _ = decode(response, streamed)
            self.assertEqual(calls[0]['arguments'], expected)
            forwarded = self.post.call_args.kwargs['json']
            self.assertFalse(forwarded['chat_template_kwargs']['enable_thinking'])
            self.assertIn('Do not skip a requested read', forwarded['messages'][0]['content'])

    def test_exact_user_file_content_is_repaired_in_both_modes(self):
        self.check_modes(request_body(), payload(), stream_lines(), WANTED)

    def test_extra_closing_delimiters_are_repaired_before_content_normalization(self):
        completion = payload()
        completion['choices'][0]['message']['tool_calls'][0]['function']['arguments'] += ']}'
        lines = stream_lines()
        final = json.loads(lines[1][6:])
        final['choices'][0]['delta']['tool_calls'][0]['function']['arguments'] += ']}'
        lines[1] = b'data: ' + json.dumps(final).encode() + b'\n'
        self.check_modes(request_body(), completion, lines, WANTED)

    def test_question_defaults_are_repaired_after_fragment_assembly(self):
        args = {'questions': [{'header': 'Database', 'question': 'Which database?',
            'options': [{'label': 'SQLite', 'description': 'Embedded'}]}]}
        fn = {'name': 'question', 'arguments': json.dumps(args)}
        completion = {'choices': [{'message': {'tool_calls': [{'id': 'q1', 'function': fn}]},
                                  'finish_reason': 'tool_calls'}]}
        raw = fn['arguments']
        events = [{'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'q1',
            'function': {'name': 'question', 'arguments': raw[:12]}}]}}]},
            {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': raw[12:]}}]},
                          'finish_reason': 'tool_calls'}]}]
        lines = [b'data: ' + json.dumps(e).encode() + b'\n' for e in events] + [b'data: [DONE]\n']
        expected = {'questions': [{**args['questions'][0], 'multiple': False}]}
        self.check_modes({'tools': [QUESTION], 'messages': [{'role': 'user', 'content': 'Ask which database'}]},
                         completion, lines, expected)

    def test_text_stream_is_not_buffered_until_completion(self):
        seen = []
        def events():
            seen.append('first')
            yield b'data: {"choices":[{"delta":{"content":"Hello"}}]}'
            seen.append('second')
            yield b'data: [DONE]'
        upstream = Mock(ok=True, status_code=200)
        upstream.headers = {'content-type': 'text/event-stream'}
        upstream.iter_lines.return_value = events()
        self.post.return_value = upstream
        response = self.client.post('/v1/chat/completions', json={'stream': True}, buffered=False)
        self.assertEqual(seen, ['first'])
        self.assertIn(b'Hello', response.get_data())
        self.assertEqual(seen, ['first', 'second'])

    def test_textual_calls_are_recovered_in_both_modes_with_usage(self):
        for text in ('```json\n{"name":"echo","arguments":{"value":"hello"}}\n```',
                     '<|tool_call>call:echo{value:<|"|>hello<|"|>}<tool_call|>'):
            body = {'tools': [{'type': 'function', 'function': {'name': 'echo',
                    'parameters': {'type': 'object', 'properties': {'value': {'type': 'string'}}}}}]}
            completion = {'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}],
                          'usage': {'completion_tokens': 12}}
            events = [{'choices': [{'delta': {'content': part}, 'finish_reason': None}]}
                      for part in (text[:10], text[10:])]
            events.append({'choices': [], 'usage': {'completion_tokens': 12}})
            lines = [b'data: ' + json.dumps(e).encode() for e in events] + [b'data: [DONE]']
            for streamed in (False, True):
                upstream = Mock(ok=True, status_code=200)
                upstream.headers = {'content-type': 'text/event-stream' if streamed else 'application/json'}
                upstream.json.return_value = json.loads(json.dumps(completion))
                upstream.content = json.dumps(completion).encode()
                upstream.iter_lines.return_value = lines
                self.post.return_value = upstream
                response = self.client.post('/v1/chat/completions', json={**body, 'stream': streamed})
                decoded = Mock(content=response.data)
                decoded.json.return_value = response.get_json() if not streamed else None
                calls, _, finishes, usage = decode(decoded, streamed)
                self.assertEqual(calls[0]['arguments'], {'value': 'hello'})
                self.assertEqual(finishes[-1], 'tool_calls')
                self.assertEqual(usage['completion_tokens'], 12)
