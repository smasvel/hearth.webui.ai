import copy
import contextlib
import json
import unittest
from unittest.mock import Mock, patch

import fastflowlm_client as flm
import flm_tool_shim as shim
from flm_tool_shim import _split_live_reasoning


class ReasoningStreamingTests(unittest.TestCase):
    def setUp(self):
        paused = patch.object(shim.fastflowlm, 'requests_paused', return_value=False)
        paused.start()
        self.addCleanup(paused.stop)

    def test_hearth_receives_first_content_before_upstream_finishes(self):
        advanced = []
        def lines(**kwargs):
            yield b'data: ' + json.dumps({'choices': [{'delta': {'content': 'First'}}]}).encode()
            advanced.append(True)
            yield b'data: [DONE]'
        upstream = Mock(status_code=200, headers={'content-type': 'text/event-stream'})
        upstream.iter_lines.side_effect = lines
        with contextlib.ExitStack() as stack:
            for name in ('_free_lemonade_npu', '_ensure_fastflow_ready', '_record_chat_metric'):
                stack.enter_context(patch.object(shim, name))
            stack.enter_context(patch.object(shim, 'live_report'))
            stack.enter_context(patch.object(shim, 'npu_request_lock'))
            stack.enter_context(patch.object(shim.requests, 'post', return_value=upstream))
            response = shim.app.test_client().post('/v1/chat/completions', json={
                'model': 'qwen3.8-mtp:27b', 'messages': [], 'stream': True,
                'tools': [{'type': 'function'}],
            }, headers={'X-Hearth-Client': 'webui-chat'}, buffered=False)
            try:
                first = next(iter(response.response))
                self.assertIn(b'First', first)
                self.assertEqual(advanced, [])
            finally:
                response.close()

    def test_split_streams_reasoning_once_and_retains_tools_finish_and_usage(self):
        event = {'usage': {'total_tokens': 9}, 'choices': [{
            'index': 0, 'finish_reason': 'stop', 'delta': {
                'reasoning_content': 'Checking', 'content': 'Answer',
                'tool_calls': [{'index': 0}],
            },
        }]}
        original = copy.deepcopy(event)
        live, buffered = _split_live_reasoning(event)
        self.assertEqual(live['choices'][0]['delta'], {'reasoning_content': 'Checking'})
        self.assertIsNone(live['choices'][0]['finish_reason'])
        self.assertNotIn('usage', live)
        self.assertEqual(buffered['choices'][0]['delta'], {
            'content': 'Answer', 'tool_calls': [{'index': 0}],
        })
        self.assertEqual(buffered['usage'], event['usage'])
        self.assertEqual(buffered['choices'][0]['finish_reason'], 'stop')
        self.assertEqual(event, original)

    def test_regular_event_unchanged(self):
        event = {'choices': [{'delta': {'content': 'Hello'}}]}
        live, buffered = _split_live_reasoning(event)
        self.assertIsNone(live)
        self.assertIs(buffered, event)

    @patch.object(flm, '_write_active')
    @patch.object(flm.requests, 'post')
    def test_hearth_enables_thinking_and_reads_incrementally(self, post, _write):
        response = Mock(status_code=200)
        response.iter_lines.return_value = [
            b'data: {"choices":[{"delta":{"reasoning_content":"Checking"}}]}',
        ]
        post.return_value.__enter__.return_value = response
        lines = list(flm.chat_stream([], 'flm/test', tools=[{'type': 'function'}]))
        self.assertIn('reasoning_content', lines[0])
        self.assertTrue(post.call_args.kwargs['json']['enable_thinking'])
        response.iter_lines.assert_called_once_with(chunk_size=1)
