import unittest
from lemonade_guard_shim import _normalize_tool_choice


class DisabledToolsTests(unittest.TestCase):
    def test_disabled_tools_are_not_sent_to_native_parser(self):
        body = {'model': 'Muse-Glimmer-30B-GGUF', 'tools': [{'type': 'function'}],
                'tool_choice': 'none', 'parallel_tool_calls': True,
                'messages': [{'role': 'user', 'content': 'Answer directly'}]}
        for stream in (False, True):
            result = _normalize_tool_choice({**body, 'stream': stream})
            self.assertNotIn('tools', result)
            self.assertNotIn('tool_choice', result)
            self.assertNotIn('parallel_tool_calls', result)
            self.assertEqual(result['messages'], body['messages'])
            self.assertEqual(result['stream'], stream)
        self.assertIn('tools', body)
