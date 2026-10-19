import contextlib
import json
import unittest
from unittest.mock import Mock, MagicMock, patch

import flm_tool_shim as shim
from tools.test_flm_model import decode
from tools.opencode_scenarios import TOOLS


def marker_lines(with_call=False):
    delta = {'content': '[Error parsing tool call]'}
    if with_call:
        delta['tool_calls'] = [{'index': 0, 'id': 'already-seen', 'function': {'name': 'ping', 'arguments': '{}'}}]
    event = {'choices': [{'delta': delta, 'finish_reason': 'stop'}]}
    return [b'data: ' + json.dumps(event).encode(), b'', b'data: [DONE]', b'']


class ParserRecoveryTests(unittest.TestCase):
    def test_recovered_textual_call_still_passes_the_unsafe_command_guard(self):
        response = MagicMock()
        response.__enter__.return_value = response
        markup = '<tool_call>' + json.dumps({'name': 'bash', 'arguments': {
            'command': 'cat > /tmp/file.txt <<EOF\ncontent\nEOF'}}) + '</tool_call>'
        response.json.return_value = {'choices': [{'message': {'content': markup}, 'finish_reason': 'stop'}]}
        with patch.object(shim.requests, 'post', return_value=response):
            wire, _ = shim._recover_native_tool_parser({'tools': TOOLS}, {})
        calls, text, finishes, _ = decode(Mock(content=wire), True)
        self.assertEqual(calls, [])
        self.assertIn('Hearth blocked', text)
        self.assertEqual(finishes, ['stop'])

    def test_never_recovers_after_any_call_or_ordinary_error_text(self):
        self.assertFalse(shim._native_tool_parser_failed(marker_lines(with_call=True)))
        self.assertTrue(shim._native_tool_parser_failed(marker_lines()))
        self.assertFalse(shim._native_tool_parser_failed([b'data: {"choices":[{"delta":{"content":"ordinary answer"}}]}']))

    def test_http_stream_recovers_once_without_releasing_model_ownership(self):
        for heals in (True, False):
            with self.subTest(heals=heals), contextlib.ExitStack() as stack:
                for name in ('_free_lemonade_npu', '_ensure_fastflow_ready', '_record_chat_metric',
                             '_log_tool_call_capture', 'live_report'):
                    stack.enter_context(patch.object(shim, name))
                lock = stack.enter_context(patch.object(shim, 'npu_request_lock'))
                stack.enter_context(patch.object(shim.fastflowlm, 'requests_paused', return_value=False))
                stack.enter_context(patch.object(shim.flm_context, 'prepare', side_effect=lambda b: (b, None)))
                upstream = Mock(ok=True, status_code=200)
                upstream.headers = {'content-type': 'text/event-stream'}
                upstream.iter_lines.return_value = marker_lines()
                fallback = MagicMock(ok=True, status_code=200)
                fallback.__enter__.return_value = fallback
                args = {'todos': [{'content': 'Implement PostgreSQL', 'status': 'in_progress', 'priority': 'high'}]}
                message = {'content': '[Error parsing tool call]'}
                if heals:
                    message = {'tool_calls': [{'id': 'recovered-real-id', 'type': 'function',
                        'function': {'name': 'todowrite', 'arguments': json.dumps(args) + ']}'}}]}
                fallback.json.return_value = {'id': 'recovered-response-id', 'choices': [{'message': message,
                    'finish_reason': 'tool_calls' if heals else 'stop'}], 'usage': {'completion_tokens': 21}}
                post = stack.enter_context(patch.object(shim.requests, 'post', side_effect=[upstream, fallback]))
                response = shim.app.test_client().post('/v1/chat/completions', json={
                    'model': 'flm/qwen3-it:4b', 'tools': TOOLS, 'stream': True,
                    'messages': [{'role': 'user', 'content': 'Update the implementation plan'}]})
                calls, text, finishes, usage = decode(Mock(content=response.data), True)
                self.assertEqual(post.call_count, 2)
                self.assertFalse(post.call_args_list[1].kwargs['json']['stream'])
                self.assertEqual(post.call_args_list[1].kwargs['timeout'], (10, 90))
                lock.acquire.assert_called_once()
                lock.release.assert_called_once()
                if heals:
                    self.assertEqual(calls[0]['arguments'], args)
                    self.assertEqual(calls[0]['id'], 'recovered-real-id')
                    self.assertEqual(finishes, ['tool_calls'])
                else:
                    self.assertEqual(calls, [])
                    self.assertIn('Error parsing tool call', text)
                    self.assertEqual(finishes, ['stop'])
