import unittest

import app


class ChatFinalizeReplyTests(unittest.TestCase):
    def test_reasoning_only_stream_is_promoted_to_the_reply(self):
        text, reasoning = app._chat_finalize_reply("", "Reasoning stream here.")
        self.assertEqual(text, "Reasoning stream here.")
        self.assertEqual(reasoning, "")

    def test_content_plus_reasoning_keeps_separate_fields(self):
        text, reasoning = app._chat_finalize_reply("Answer body.", "Thinking.")
        self.assertEqual(text, "Answer body.")
        self.assertEqual(reasoning, "Thinking.")

    def test_empty_stream_stays_empty(self):
        text, reasoning = app._chat_finalize_reply("", "")
        self.assertEqual(text, "")
        self.assertEqual(reasoning, "")


if __name__ == '__main__':
    unittest.main()
