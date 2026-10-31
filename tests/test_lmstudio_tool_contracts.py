import contextlib
import json
import threading
import unittest
from unittest.mock import Mock, patch

import lmstudio_shim as shim
import lmstudio_tool_shim
from model_stream_cancel import Registry
import lmstudio_thinking
from tools.opencode_scenarios import QUESTION
from tools.test_flm_model import decode
from tests.test_exact_file_content import request_body, payload, stream_lines, WANTED


class LMStudioContractTests(unittest.TestCase):
    def test_reasoning_reaches_client_before_tool_stream_finishes(self):
        release = threading.Event()
        upstream = Mock(ok=True, status_code=200, headers={'content-type': 'text/event-stream'})
        def lines(*_args, **_kwargs):
            yield b'data: {"choices":[{"index":0,"delta":{"reasoning_content":"working"},"finish_reason":null}]}'
            yield b''
            release.wait(2)
            yield b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}'
            yield b'data: [DONE]'
        upstream.iter_lines.side_effect = lines
        self.post.return_value = upstream
        response = self.client.post('/v1/chat/completions', json={
            'model': 'fixture', 'stream': True, 'tools': [{'type': 'function',
            'function': {'name': 'read', 'parameters': {'type': 'object'}}}]}, buffered=False)
        try:
            self.assertIn(b'"reasoning_content":"working"', next(response.response))
            self.assertFalse(release.is_set())
        finally:
            release.set()
            response.close()

    def test_cancelled_stream_closes_only_matching_upstream(self):
        registry = Registry()
        first, second, late = Mock(), Mock(), Mock()
        registry.begin('first')
        registry.begin('second')
        registry.attach('first', first)
        registry.attach('second', second)
        registry.cancel('first')
        first.close.assert_called_once()
        second.close.assert_not_called()
        registry.cancel('late')
        registry.begin('late')
        registry.attach('late', late)
        late.close.assert_called_once()

    def test_both_lmstudio_gateways_accept_local_cancel(self):
        stream_id = 'a' * 48
        for gateway in (shim.app, lmstudio_tool_shim.app):
            with patch('model_stream_cancel.streams.cancel') as cancel:
                response = gateway.test_client().post('/_hearth/cancel/' + stream_id)
                self.assertEqual(response.status_code, 204)
                cancel.assert_called_once_with(stream_id)

    def test_per_model_thinking_preference_keeps_other_models_unchanged(self):
        body = {'model': 'selected', 'chat_template_kwargs': {'other': True}}
        with patch.object(lmstudio_thinking.settings_store, 'get', return_value='{"selected":false}'):
            updated = lmstudio_thinking.apply(body)
            self.assertEqual(updated['enable_thinking'], False)
            self.assertEqual(updated['chat_template_kwargs'], {'other': True, 'enable_thinking': False})
            self.assertEqual(lmstudio_thinking.apply({'model': 'other'}), {'model': 'other'})
        self.assertNotIn('enable_thinking', body)

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

    def test_named_choice_rebuilds_mangled_or_missing_call(self):
        """LM Studio + mimo-v2.6-distill (observed live 2026-10-30): the legacy tool markup
        the model emits gets mis-parsed server-side — either a tool call with markup inside
        its arguments and the *correct* arguments left as plain content text, or no call at
        all after a length-truncated finish. The client's exact-arguments contract is the
        authority for rebuilding, mirroring lemonade_guard_shim.py."""
        from tools.opencode_scenarios import TOOLS
        contract_body = {
            'tools': [t for t in TOOLS if t['function']['name'] == 'glob'],
            'tool_choice': {'type': 'function', 'function': {'name': 'glob'}},
            'messages': [
                {'role': 'system', 'content': 'Testing an OpenCode coding workflow.'},
                {'role': 'user', 'content':
                 'Next call glob with exactly these arguments: {"pattern": "**/*.py", "path": "/workspace/atlas"}.'},
            ],
        }
        expected = {'pattern': '**/*.py', 'path': '/workspace/atlas'}
        # Mangled arguments (legacy markup inside), arguments leaked as plain content.
        completion = {'choices': [{'message': {'tool_calls': [
            {'id': 'g1', 'function': {'name': 'glob',
             'arguments': '{"pattern":"</parameter>\\n<parameter=path>"}'}}],
            'content': '{"pattern": "**/*.py", "path": "/workspace/atlas"}',
            'finish_reason': 'tool_calls'}}]}
        # Backend dropped the call entirely (finish=length, empty content).
        dropped = {'choices': [{'message': {'content': ''}, 'finish_reason': 'length'}]}
        for broken in (completion, dropped):
            for streamed in (False, True):
                if streamed:
                    # Same content, delivered as deltas (args split, content an event).
                    content = broken['choices'][0]['message'].get('content') or ''
                    garbage_event = {'choices': [{'delta': {'tool_calls': [
                        {'index': 0, 'id': 'g1', 'function': {'name': 'glob',
                                                              'arguments': '{"pattern":"</'}}]}}]}
                    finish = broken['choices'][0].get('finish_reason') or 'tool_calls'
                    second = {'choices': [{'delta': {'tool_calls': [
                        {'index': 0, 'function': {'arguments':
                                                  'parameter>\\n<parameter=path>"}'}}]},
                        'finish_reason': finish}]}
                    events = [
                        {'choices': [{'delta': {'content': content}}]},
                        garbage_event,
                        second,
                    ]
                    lines = [b'data: ' + json.dumps(e).encode() for e in events] + [b'data: [DONE]']
                    upstream = Mock(ok=True, status_code=200)
                    upstream.headers = {'content-type': 'text/event-stream'}
                    upstream.json.return_value = broken
                    upstream.content = json.dumps(broken).encode()
                    upstream.iter_lines.return_value = [l.rstrip(b'\n') for l in lines]
                    self.post.return_value = upstream
                    r = self.client.post('/v1/chat/completions',
                                         json={**contract_body, 'stream': True})
                else:
                    upstream = Mock(ok=True, status_code=200)
                    upstream.headers = {'content-type': 'application/json'}
                    upstream.json.return_value = json.loads(json.dumps(broken))
                    upstream.content = json.dumps(broken).encode()
                    self.post.return_value = upstream
                    r = self.client.post('/v1/chat/completions',
                                         json={**contract_body, 'stream': False})
                self.assertEqual(r.status_code, 200)
                decoded = Mock(content=r.data)
                decoded.json.return_value = r.get_json() if not streamed else None
                calls, _, finishes, _ = decode(decoded, streamed)
                self.assertEqual(calls[0]['arguments'], expected)
                self.assertEqual(calls[0]['name'], 'glob')
                self.assertEqual(finishes[-1], 'tool_calls')

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
