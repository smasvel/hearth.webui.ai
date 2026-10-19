import json
import unittest

import generation_guard


def chunk(text):
    return "data: " + json.dumps({"choices": [{"delta": {"content": text}}]})


class GenerationGuardTests(unittest.TestCase):
    def test_detects_slash_collapse(self):
        self.assertTrue(generation_guard.looks_like_repetition_collapse("answer " + "/" * 30))

    def test_allows_normal_code(self):
        self.assertFalse(generation_guard.looks_like_repetition_collapse("https://example.com/a/b"))

    def test_replaces_collapsed_stream_without_leaking_symbols(self):
        output = list(generation_guard.guard_sse_stream([chunk("/" * 40), "data: [DONE]"]))
        self.assertNotIn("/" * 24, "".join(output))
        self.assertIn("repetition loop", "".join(output))

    def test_preserves_normal_stream(self):
        original = [chunk("hello"), "data: [DONE]"]
        self.assertEqual(list(generation_guard.guard_sse_stream(original)), original)

    def test_byte_proxy_stream_is_guarded(self):
        chunks = [(chunk("/" * 40) + "\n").encode(), b"data: [DONE]\n"]
        output = b"".join(generation_guard.guard_sse_bytes(chunks))
        self.assertNotIn(b"/" * 24, output)
        self.assertIn(b"repetition loop", output)

    def test_nonstream_completion_is_guarded(self):
        payload = {
            "choices": [{"message": {"content": "/" * 40}, "finish_reason": "length"}]
        }
        self.assertTrue(generation_guard.guard_completion_payload(payload))
        self.assertNotIn("/" * 24, payload["choices"][0]["message"]["content"])
        self.assertEqual("stop", payload["choices"][0]["finish_reason"])


if __name__ == "__main__":
    unittest.main()
