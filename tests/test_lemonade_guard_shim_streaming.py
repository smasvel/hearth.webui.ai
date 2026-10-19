"""Streaming regression tests for lemonade_guard_shim.py.

Real incident (2026-10-18): OpenCode appeared to hang for the entire think phase of
GLM-4.7-Flash (a reasoning model that can think for minutes) because the shim buffered the
*whole* Lemonade response — reasoning included — before yielding anything. It now forwards
reasoning_content the moment it arrives (and streams everything live when the request
declares no tools), matching flm_tool_shim.py's tee().
"""
import json
import unittest
from unittest.mock import patch

import lemonade_guard_shim as shim


def sse(delta: dict, finish=None) -> bytes:
    return b"data: " + json.dumps(
        {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    ).encode()


class SplitLiveReasoningTests(unittest.TestCase):
    def test_reasoning_is_split_from_the_buffered_event(self):
        obj = {"id": "x", "created": 1, "choices": [{"index": 0, "delta": {
            "reasoning_content": "hmm", "content": "answer"}, "finish_reason": None}]}
        live, buffered = shim._split_live_reasoning(obj)
        self.assertEqual(live["choices"][0]["delta"], {"reasoning_content": "hmm"})
        self.assertNotIn("usage", live)
        self.assertEqual(buffered["choices"][0]["delta"], {"content": "answer"})
        self.assertNotIn("reasoning_content", buffered["choices"][0]["delta"])

    def test_non_reasoning_event_is_left_untouched(self):
        obj = {"choices": [{"index": 0, "delta": {"content": "answer"}, "finish_reason": None}]}
        live, buffered = shim._split_live_reasoning(obj)
        self.assertIsNone(live)
        self.assertIs(buffered, obj)


class _FakeUpstream:
    ok = True
    status_code = 200
    headers = {"content-type": "text/event-stream"}

    def __init__(self, lines, events):
        self._lines = lines
        self._events = events

    def iter_lines(self, chunk_size=None):
        for line in self._lines:
            self._events.append(("up", line))
            yield line

    def close(self):
        pass


class StreamedResponseTests(unittest.TestCase):
    """Drive the real view through Flask's test client, recording pull/yield ordering."""

    def _collect(self, body, lines):
        events = []
        upstream = _FakeUpstream(lines, events)
        with patch.object(shim, "model_test_profiles"), \
                patch.object(shim, "_prepare_agent_request", side_effect=lambda b: b), \
                patch.object(shim, "_is_npu_model", return_value=False), \
                patch.object(shim, "_unload_before_implicit_load"), \
                patch.object(shim, "_record_chat_metric"), \
                patch.object(shim.live_report, "start", return_value=1), \
                patch.object(shim.live_report, "update"), \
                patch.object(shim.live_report, "finish"), \
                patch.object(shim, "_post_chat_with_utf8_recovery",
                             return_value=(upstream, 0)):
            shim.model_test_profiles.apply.side_effect = lambda b, *_a: b
            response = shim.app.test_client().post("/v1/chat/completions", json=body)
            for chunk in response.response:
                events.append(("out", chunk if isinstance(chunk, bytes) else chunk.encode()))
        return events

    def test_no_tools_streams_every_line_immediately(self):
        lines = [sse({"reasoning_content": "think"}), sse({"content": "answer"}),
                 sse({}, finish="stop"), b"data: [DONE]"]
        events = self._collect({"model": "GLM-4.7-Flash-GGUF", "stream": True}, lines)
        # Strictly interleaved: each upstream line is yielded before the next is pulled.
        kinds = [kind for kind, _ in events]
        self.assertEqual(kinds, ["up", "out", "up", "out", "up", "out", "up", "out"])
        # The shim appends a newline delimiter to each forwarded line.
        self.assertEqual([raw for kind, raw in events if kind == "out"],
                         [line + b"\n" for line in lines])

    def test_tools_stream_reasoning_live_before_content_is_buffered(self):
        lines = [sse({"reasoning_content": "think"}), sse({"content": "answer"}),
                 sse({}, finish="stop"), b"data: [DONE]"]
        body = {"model": "GLM-4.7-Flash-GGUF", "stream": True,
                "tools": [{"type": "function", "function": {"name": "read", "parameters": {}}}]}
        events = self._collect(body, lines)
        # The reasoning is forwarded before the upstream content line is even pulled.
        pulled_before_live = None
        for index, (kind, raw) in enumerate(events):
            if kind == "out" and b"reasoning_content" in raw:
                pulled_before_live = [r for k, r in events[:index] if k == "up"]
                break
        self.assertIsNotNone(pulled_before_live, "live reasoning event was never yielded")
        self.assertEqual(len(pulled_before_live), 1)  # only the reasoning line had been pulled

        # And the buffered replay must not repeat the reasoning.
        after = [raw for kind, raw in events if kind == "out"]
        self.assertEqual(sum(b"reasoning_content" in raw for raw in after), 1)
        self.assertEqual(b"".join(after).count(b"answer"), 1)


if __name__ == "__main__":
    unittest.main()
