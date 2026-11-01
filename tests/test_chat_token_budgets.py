import unittest

import app


class ChatTokenBudgetTests(unittest.TestCase):
    def test_vllm_2048_window_caps_default_output(self):
        output, prompt = app._chat_token_budgets(2048, 32768)
        self.assertEqual(output, 768)
        self.assertEqual(prompt, 1024)
        self.assertLessEqual(output + prompt + 256, 2048)

    def test_large_window_keeps_room_for_prompt(self):
        output, prompt = app._chat_token_budgets(32768, 32768)
        self.assertEqual(output, 31488)
        self.assertEqual(prompt, 1024)
        self.assertLessEqual(output + prompt + 256, 32768)

    def test_small_explicit_output_stays_unchanged(self):
        output, prompt = app._chat_token_budgets(8192, 512)
        self.assertEqual(output, 512)
        self.assertEqual(prompt, 7424)

    def test_tiny_window_still_returns_valid_budgets(self):
        output, prompt = app._chat_token_budgets(512, 32768)
        self.assertEqual(output, 1)
        self.assertEqual(prompt, 255)
        self.assertLessEqual(output + prompt + 256, 512)


if __name__ == '__main__':
    unittest.main()
