import unittest

import lmstudio_tool_shim


class ExplicitToolRequestTests(unittest.TestCase):
    def test_detects_named_tool_instruction(self):
        body = {"messages": [{"role": "user", "content": "Use glob to find seed.txt"}]}
        self.assertTrue(lmstudio_tool_shim._explicitly_requires_tool(body))

    def test_detects_required_tool_choice(self):
        self.assertTrue(lmstudio_tool_shim._explicitly_requires_tool({"tool_choice": "required"}))

    def test_does_not_force_tools_for_normal_chat(self):
        body = {"messages": [{"role": "user", "content": "Explain how file search works"}]}
        self.assertFalse(lmstudio_tool_shim._explicitly_requires_tool(body))
