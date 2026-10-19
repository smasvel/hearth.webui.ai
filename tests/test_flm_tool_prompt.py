import unittest
from flm_tool_shim import _inject_tools_block, _ensure_no_thinking


class ToolPromptTests(unittest.TestCase):
    def test_minicpm_keeps_reasoning_and_respects_explicit_preference(self):
        self.assertTrue(_ensure_no_thinking({'model': 'flm/minicpm-v-4.7:1b'})['enable_thinking'])
        self.assertFalse(_ensure_no_thinking({'model': 'minicpm-v-4.7:1b', 'enable_thinking': False})['enable_thinking'])
        self.assertFalse(_ensure_no_thinking({'model': 'qwen3.6-moe:35b-a3b'})['enable_thinking'])

    def test_tool_choice_none_does_not_inject_tool_instructions(self):
        for model in ('qwen3-it:4b', 'qwen3:8b', 'qwen3.6-moe:35b-a3b'):
            body = {'model': model, 'tool_choice': 'none',
                    'tools': [{'type': 'function', 'function': {'name': 'read'}}],
                    'messages': [{'role': 'user', 'content': 'Read README.md'}]}
            self.assertIs(_inject_tools_block(body), body)

    def test_qwen3_uses_json_tool_format(self):
        body = {'model': 'flm/qwen3:8b', 'tools': [{'type': 'function', 'function': {'name': 'read'}}],
                'messages': [{'role': 'user', 'content': 'Read README.md'}]}
        result = _inject_tools_block(body)
        instructions = result['messages'][0]['content']
        self.assertIn('"name": "function_name"', instructions)
        self.assertNotIn('<function=', instructions)
        self.assertEqual(body['messages'][0]['role'], 'user')

    def test_qwen3_instruct_uses_json_tools_without_xml_function_markup(self):
        result = _inject_tools_block({'model': 'flm/qwen3-it:4b',
            'tools': [{'type': 'function', 'function': {'name': 'edit'}}],
            'messages': [{'role': 'user', 'content': 'Edit the implementation'}]})
        self.assertIn('"arguments":', result['messages'][0]['content'])
        self.assertNotIn('<function=', result['messages'][0]['content'])

    def test_qwen3_no_think_marker_preserves_history_and_explicit_thinking(self):
        body = {'model': 'qwen3:8b', 'messages': [{'role': 'user', 'content': 'Read README.md'}]}
        result = _ensure_no_thinking(body)
        self.assertTrue(result['messages'][0]['content'].endswith('/no_think'))
        self.assertEqual(body['messages'][0]['content'], 'Read README.md')
        self.assertEqual(_ensure_no_thinking(result), result)
        self.assertIs(_ensure_no_thinking({**body, 'enable_thinking': True})['messages'], body['messages'])

    def test_native_qwen38_does_not_duplicate_schemas(self):
        for model in ('qwen3.8-mtp:27b', 'flm/qwen3.8-mtp:27b', 'minicpm-v-4.7:1b', 'flm/minicpm-v-4.7:1b'):
            body = {'model': model, 'tools': [{'type': 'function'}],
                    'messages': [{'role': 'system', 'content': 'Keep this instruction.'},
                                 {'role': 'user', 'content': 'hi'}]}
            self.assertIs(_inject_tools_block(body), body)

    def test_legacy_model_still_gets_tool_instructions(self):
        body = {'model': 'qwen3.6-moe:35b-a3b', 'tools': [{'type': 'function'}],
                'messages': [{'role': 'user', 'content': 'hi'}]}
        result = _inject_tools_block(body)
        self.assertEqual(result['messages'][0]['role'], 'system')
        self.assertIn('# Tools', result['messages'][0]['content'])
        self.assertEqual(result['messages'][1:], body['messages'])
