import json
import unittest
from unittest.mock import Mock

import flm_tool_shim as shim
import tool_argument_json
from tools.test_flm_model import decode
from tools.opencode_scenarios import TOOLS


class ToolArgumentJSONTests(unittest.TestCase):
    def test_only_extra_closing_delimiters_are_recovered(self):
        expected = {'todos': [{'content': 'Implement PostgreSQL', 'status': 'in_progress', 'priority': 'high'}]}
        self.assertEqual(tool_argument_json.loads(json.dumps(expected) + ']}'), expected)
        for suffix in ['{}', ' true', ' trailing', '<tool_call>', ']' * 65]:
            with self.subTest(suffix=suffix), self.assertRaises(json.JSONDecodeError):
                tool_argument_json.loads(json.dumps(expected) + suffix)
        for raw in ['{"unfinished":', '{"unfinished":"text', '{"a":1']:
            with self.assertRaises(json.JSONDecodeError):
                tool_argument_json.loads(raw)

    def test_strings_and_indentation_are_never_changed(self):
        expected = {'content': 'def code():\n    return "]}"\n', 'filePath': '/project/x.py'}
        self.assertEqual(tool_argument_json.loads(json.dumps(expected) + ']}'), expected)

    def test_flm_complete_and_fragmented_stream_are_repaired(self):
        args = {'todos': [{'content': 'Implement PostgreSQL', 'status': 'in_progress', 'priority': 'high'}]}
        raw = json.dumps(args) + ']}'
        body = {'tools': TOOLS, 'messages': []}
        response = {'choices': [{'index': 0, 'message': {'tool_calls': [{'id': 'original-id', 'type': 'function',
            'function': {'name': 'todowrite', 'arguments': raw}}]}, 'finish_reason': 'tool_calls'}]}
        self.assertTrue(shim._repair_tool_arguments(response, body))
        fn = response['choices'][0]['message']['tool_calls'][0]['function']
        self.assertEqual(json.loads(fn['arguments']), args)
        events = [{'id': 'response-id', 'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0,
            'id': 'original-id', 'function': {'name': 'todowrite', 'arguments': raw[:25]}}]}}]},
            {'id': 'response-id', 'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0,
            'function': {'arguments': raw[25:]}}]}, 'finish_reason': 'tool_calls'}], 'usage': {'completion_tokens': 22}}]
        lines = [b'data: ' + json.dumps(e).encode() + b'\n' for e in events] + [b'data: [DONE]\n']
        repaired = shim._guard_streamed_tool_calls(lines, body)
        calls, _, finishes, usage = decode(Mock(content=repaired), True)
        self.assertEqual(calls[0]['arguments'], args)
        self.assertEqual(calls[0]['id'], 'original-id')
        self.assertEqual(finishes, ['tool_calls'])
        self.assertEqual(usage['completion_tokens'], 22)
