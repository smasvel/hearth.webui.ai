import copy
import unittest

import lemonade_guard_shim as shim


class AgentRequestTests(unittest.TestCase):
    def test_tool_request_sets_template_thinking_flag_without_mutating_input(self):
        body = {'tools': [{'type': 'function'}], 'messages': [{'role': 'user', 'content': 'Inspect project'}]}
        original = copy.deepcopy(body)
        result = shim._prepare_agent_request(body)
        self.assertEqual(body, original)
        self.assertFalse(result['chat_template_kwargs']['enable_thinking'])

    def test_explicit_template_preferences_are_preserved(self):
        body = {'tools': [{}], 'chat_template_kwargs': {'enable_thinking': True, 'other': 1}}
        result = shim._prepare_agent_request(body)
        self.assertEqual(result['chat_template_kwargs'], body['chat_template_kwargs'])
        self.assertIsNot(result['chat_template_kwargs'], body['chat_template_kwargs'])
        # The top-level field must match, or templates that read it (GLM-4.7-Flash) disable
        # thinking despite the explicit chat_template_kwargs request.
        self.assertIs(result['enable_thinking'], True)

    def test_top_level_thinking_preference_is_honored_and_mirrored(self):
        body = {'tools': [{}], 'enable_thinking': True}
        result = shim._prepare_agent_request(body)
        self.assertIs(result['enable_thinking'], True)
        self.assertIs(result['chat_template_kwargs']['enable_thinking'], True)

    def test_thinking_defaults_off_when_the_caller_expresses_no_preference(self):
        result = shim._prepare_agent_request({'tools': [{}]})
        self.assertIs(result['enable_thinking'], False)
        self.assertIs(result['chat_template_kwargs']['enable_thinking'], False)

    def test_plain_chat_is_untouched(self):
        body = {'messages': [{'role': 'user', 'content': 'Hi'}]}
        self.assertIs(shim._prepare_agent_request(body), body)

    def test_disabled_tools_do_not_inject_agent_loop_instructions(self):
        body = {'tools': [{'type': 'function'}], 'tool_choice': 'none',
                'messages': [{'role': 'user', 'content': 'Answer directly.'}]}
        self.assertIs(shim._prepare_agent_request(body), body)

    def test_qwen_penalty_does_not_change_other_models_or_explicit_preferences(self):
        base = {'tools': [{}]}
        self.assertNotIn('frequency_penalty', shim._prepare_agent_request({**base, 'model': 'NVIDIA-Nemotron-3.5-Lightning-30B-A3B-GGUF'}))
        self.assertEqual(shim._prepare_agent_request({**base, 'model': 'Qwen3-GGUF'})['frequency_penalty'], 0.4)
        self.assertEqual(shim._prepare_agent_request({**base, 'model': 'Qwen3-GGUF', 'frequency_penalty': 0})['frequency_penalty'], 0)
