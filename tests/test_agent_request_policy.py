import unittest

import agent_request_policy


class ExplicitStopPolicyTests(unittest.TestCase):
    def body(self, user, *, older_user=None):
        messages = []
        if older_user:
            messages.append({"role": "user", "content": older_user})
        messages.append({"role": "user", "content": user})
        return {
            "model": "qwen3:8b",
            "tools": [{"type": "function", "function": {"name": "read"}}],
            "messages": messages,
        }

    def test_reply_and_stop_disables_tools_for_dismissed_question(self):
        body = self.body(
            'The user dismissed optional analytics. Reply "Skipping optional analytics" '
            "and do not ask again or install anything."
        )
        prepared = agent_request_policy.prepare(body)
        self.assertEqual(prepared["tool_choice"], "none")
        self.assertEqual(body.get("tool_choice"), None)

    def test_finish_instruction_disables_tools(self):
        body = self.body("Summarize the verified result. State how many tests passed. Do not call more tools.")
        self.assertEqual(agent_request_policy.prepare(body)["tool_choice"], "none")

    def test_text_only_turn_does_not_disable_tools_when_it_also_requests_work(self):
        body = self.body("Reply with the status and then run pytest -q.")
        self.assertNotEqual(agent_request_policy.prepare(body).get("tool_choice"), "none")

    def test_do_not_ask_again_does_not_hide_a_later_explicit_tool_request(self):
        body = self.body(
            "Use the returned user answer. Do not ask again. "
            'Next call todowrite with exactly: {"todos": []}'
        )
        self.assertNotEqual(agent_request_policy.prepare(body).get("tool_choice"), "none")

    def test_policy_only_uses_latest_user_turn(self):
        body = self.body(
            "Please inspect the implementation and run its tests.",
            older_user="Reply with the status and do not call more tools.",
        )
        self.assertNotEqual(agent_request_policy.prepare(body).get("tool_choice"), "none")


if __name__ == "__main__":
    unittest.main()
