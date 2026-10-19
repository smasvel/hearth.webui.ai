import contextlib
import copy
import json
import unittest
from unittest.mock import Mock, patch

import exact_file_content as repair
import lemonade_guard_shim as shim
import flm_tool_shim
from tools.opencode_scenarios import TOOLS
from tools.test_flm_model import decode

WANTED = {'filePath': '/project/test.py', 'content': 'def test_ok():\n    assert True\n'}
ACTUAL = {**WANTED, 'content': '\ndef test_ok():\n\n    assert True'}


def request_body():
    return {'model': 'Nemotron', 'tools': TOOLS, 'messages': [{'role': 'user',
        'content': 'Next call write with exactly these arguments: ' + json.dumps(WANTED) + '.'}]}


def payload(args=None):
    return {'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {'tool_calls': [
        {'id': 'real-call', 'type': 'function', 'function': {'name': 'write', 'arguments': json.dumps(args or ACTUAL)}}]}}],
        'usage': {'completion_tokens': 20}}


def stream_lines():
    raw = json.dumps(ACTUAL)
    events = [{'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'id': 'real-call',
        'function': {'name': 'write', 'arguments': raw[:30]}}]}}]},
        {'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0,
        'function': {'arguments': raw[30:]}}]}, 'finish_reason': 'tool_calls'}], 'usage': {'completion_tokens': 20}}]
    return [b'data: ' + json.dumps(e).encode() + b'\n' for e in events] + [b'\n', b'data: [DONE]\n']


class ExactContentTests(unittest.TestCase):
    def test_flm_completion_and_assembled_stream_use_the_same_content_guard(self):
        p = payload()
        self.assertTrue(flm_tool_shim._repair_tool_arguments(p, request_body()))
        self.assertEqual(json.loads(p['choices'][0]['message']['tool_calls'][0]['function']['arguments']), WANTED)
        wire = flm_tool_shim._guard_streamed_tool_calls(stream_lines(), request_body())
        calls, _, _, _ = decode(Mock(content=wire), True)
        self.assertEqual(calls[0]['arguments'], WANTED)

    def test_restore_supplied_bytes_preserving_call_id_and_usage(self):
        p = payload()
        self.assertTrue(repair.repair_completion(p, request_body()))
        call = p['choices'][0]['message']['tool_calls'][0]
        self.assertEqual(json.loads(call['function']['arguments']), WANTED)
        self.assertEqual(call['id'], 'real-call')
        self.assertEqual(p['usage']['completion_tokens'], 20)
        self.assertFalse(repair.repair_completion(p, request_body()))

    def test_never_changes_generated_code_paths_indentation_or_extra_arguments(self):
        for args in [{**ACTUAL, 'filePath': '/other'}, {**ACTUAL, 'content': 'def test_ok():\n    assert False'},
                     {**ACTUAL, 'content': 'def test_ok():\n  assert True'}, {**ACTUAL, 'extra': True}]:
            p = payload(args); original = copy.deepcopy(p)
            self.assertFalse(repair.repair_completion(p, request_body()))
            self.assertEqual(p, original)

    def test_only_unambiguous_latest_user_instruction_and_declared_tool(self):
        for role in ('tool', 'assistant', 'system'):
            b = request_body(); b['messages'][0]['role'] = role
            self.assertFalse(repair.repair_completion(payload(), b))
        for change in ('new-user', 'ambiguous', 'no-schema', 'ordinary-prose'):
            b = request_body()
            if change == 'new-user': b['messages'].append({'role': 'user', 'content': 'Do something else'})
            if change == 'ambiguous': b['messages'][0]['content'] += ' Actually use another path.'
            if change == 'no-schema': b['tools'] = []
            if change == 'ordinary-prose': b['messages'][0]['content'] = 'Please write a test'
            self.assertFalse(repair.repair_completion(payload(), b))

    def test_truncated_or_multiple_calls_are_not_restored(self):
        for reason in ('length', 'stop', None):
            p = payload(); p['choices'][0]['finish_reason'] = reason
            self.assertFalse(repair.repair_completion(p, request_body()))
        p = payload(); p['choices'][0]['message']['tool_calls'] *= 2
        self.assertFalse(repair.repair_completion(p, request_body()))

    def test_fragmented_stream_preserves_metadata_and_terminator(self):
        original = stream_lines()
        result = repair.repair_sse(original, request_body())
        r = Mock(content=b''.join(result))
        calls, _, finishes, usage = decode(r, True)
        self.assertEqual(calls[0]['arguments'], WANTED)
        self.assertEqual(calls[0]['id'], 'real-call')
        self.assertEqual(finishes, ['tool_calls'])
        self.assertEqual(usage, {'completion_tokens': 20})
        self.assertEqual(result[-1], original[-1])
        self.assertEqual(repair.repair_sse(result, request_body()), result)

    def test_real_proxy_applies_repair_in_both_response_modes(self):
        with contextlib.ExitStack() as stack:
            for name in ('_unload_before_implicit_load', 'live_report', '_record_chat_metric'):
                stack.enter_context(patch.object(shim, name))
            stack.enter_context(patch.object(shim, '_is_npu_model', return_value=False))
            post = stack.enter_context(patch.object(shim, '_post_chat_with_utf8_recovery'))
            for streamed in (False, True):
                upstream = Mock(ok=True, status_code=200)
                upstream.headers = {'content-type': 'text/event-stream' if streamed else 'application/json'}
                upstream.json.return_value = payload()
                upstream.content = json.dumps(payload()).encode()
                upstream.iter_lines.return_value = [x.rstrip(b'\n') for x in stream_lines()]
                post.return_value = (upstream, 0)
                b = request_body(); b['stream'] = streamed
                r = shim.app.test_client().post('/v1/chat/completions', json=b)
                self.assertEqual(r.status_code, 200)
                fixture = Mock(content=r.data)
                fixture.json.return_value = r.get_json() if not streamed else None
                calls, _, _, _ = decode(fixture, streamed)
                self.assertEqual(calls[0]['arguments'], WANTED)
