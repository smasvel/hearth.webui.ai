"""Exercise OpenCode's file-tool contract through the HTTP compatibility proxy.

Upstream model responses are fixtures, so these regressions need no running model
and test protocol compatibility rather than claiming to test model intelligence.
"""
import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import flm_tool_shim as shim


def tool(name, properties):
    return {"type": "function", "function": {"name": name, "parameters": {
        "type": "object", "properties": {key: {"type": "string"} for key in properties},
        "required": list(properties), "additionalProperties": False,
    }}}


def completion(name, arguments):
    return {"id": "fixture-completion", "model": "minicpm-v-4.7:1b",
            "object": "chat.completion", "created": 1,
            "choices": [{"index": 0, "message": {"role": "assistant", "tool_calls": [{
                "id": "fixture-call", "type": "function", "function": {
                    "name": name, "arguments": json.dumps(arguments)}}]},
                "finish_reason": "tool_calls"}], "usage": {"completion_tokens": 12}}


class OpenCodeFileToolTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name in ("_free_lemonade_npu", "_ensure_fastflow_ready", "_record_chat_metric",
                     "_log_tool_call_capture", "live_report", "npu_request_lock"):
            self.stack.enter_context(patch.object(shim, name))
        self.stack.enter_context(patch.object(shim.fastflowlm, "requests_paused", return_value=False))
        self.stack.enter_context(patch.object(shim.flm_context, "prepare", side_effect=lambda body: (body, None)))
        self.post = self.stack.enter_context(patch.object(shim.requests, "post"))
        self.client = shim.app.test_client()

    def request(self, upstream_json, tools, streamed=False, events=None, extra=None):
        upstream = Mock(status_code=200, ok=True)
        upstream.headers = {"content-type": "text/event-stream" if streamed else "application/json"}
        upstream.json.return_value = upstream_json
        upstream.content = json.dumps(upstream_json).encode()
        if streamed:
            lines = []
            for event in events:
                lines.extend([b"data: " + json.dumps(event).encode(), b""])
            lines.extend([b"data: [DONE]", b""])
            upstream.iter_lines.return_value = lines
        self.post.return_value = upstream
        response = self.client.post("/v1/chat/completions", json={
            "model": "flm/minicpm-v-4.7:1b", "stream": streamed, "tools": tools,
            "messages": [{"role": "user", "content": "Read the project and fix the implementation."}],
            **(extra or {}),
        }, headers={"X-Hearth-Client": "opencode"})
        self.assertEqual(response.status_code, 200)
        return response

    def stream_choices(self, response):
        self.assertTrue(response.data.endswith(b"data: [DONE]\n\n"))
        return [json.loads(line[6:])["choices"][0] for line in response.data.splitlines()
                if line.startswith(b"data: ") and line != b"data: [DONE]"]

    def test_read_and_edit_existing_project_without_changing_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "README.md").write_text("Implementation: src/invoice.py. Tax is a fraction.\n")
            source = root / "src/invoice.py"
            source.write_text("def total(price, tax_rate):\n    return price + tax_rate\n")
            checks = root / "check.py"
            checks.write_text("from src.invoice import total\nassert total(100, 0.2) == 120\n")
            original_checks = checks.read_bytes()
            tools = [tool("read", ["path"]), tool("edit", ["path", "oldString", "newString"])]
            for relative_path in ("README.md", "src/invoice.py"):
                response = self.request(completion("read", {"path": relative_path}), tools)
                call = response.json["choices"][0]["message"]["tool_calls"][0]
                args = json.loads(call["function"]["arguments"])
                self.assertEqual(args, {"path": relative_path})
                self.assertTrue((root / args["path"]).read_text())
            response = self.request(completion("edit", {
                "filePath": "src/invoice.py", "old_string": "return price + tax_rate",
                "new_string": "return price * (1 + tax_rate)",
            }), tools)
            choice = response.json["choices"][0]
            self.assertEqual(choice["finish_reason"], "tool_calls")
            call = choice["message"]["tool_calls"][0]
            self.assertEqual(call["function"]["name"], "edit")
            args = json.loads(call["function"]["arguments"])
            self.assertEqual(set(args), {"path", "oldString", "newString"})
            target = root / args["path"]
            original = target.read_text()
            self.assertEqual(original.count(args["oldString"]), 1)
            target.write_text(original.replace(args["oldString"], args["newString"], 1))
            namespace = {}
            exec(compile(target.read_text(), str(target), "exec"), namespace)
            self.assertEqual(namespace["total"](100, 0.2), 120)
            self.assertEqual(namespace["total"](50, 0), 50)
            self.assertEqual(checks.read_bytes(), original_checks)
            self.assertTrue(self.post.call_args.kwargs["json"]["enable_thinking"])

    def test_fragmented_edit_retains_id_arguments_and_usage(self):
        args = {"filePath": "src/a.py", "old_string": "old", "new_string": "new"}
        raw = json.dumps(args)
        template = {"id": "fixture-stream", "model": "minicpm-v-4.7:1b", "created": 1,
                    "object": "chat.completion.chunk"}
        events = []
        for index, fragment in enumerate((raw[:17], raw[17:])):
            fn = {"arguments": fragment}
            call = {"index": 0, "function": fn}
            if index == 0:
                call["id"] = "stable-call-id"
                call["type"] = "function"
                fn["name"] = "edit"
            events.append({**template, "choices": [{"index": 0, "delta": {"tool_calls": [call]},
                                                       "finish_reason": None}]})
        events.append({**template, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                       "usage": {"completion_tokens": 12}})
        response = self.request({}, [tool("edit", ["path", "oldString", "newString"])], True, events)
        choices = self.stream_choices(response)
        self.assertEqual(choices[-1]["finish_reason"], "tool_calls")
        call = choices[-1]["delta"]["tool_calls"][0]
        self.assertEqual(call["id"], "stable-call-id")
        self.assertEqual(json.loads(call["function"]["arguments"]),
                         {"path": "src/a.py", "oldString": "old", "newString": "new"})
        event = json.loads(response.data.splitlines()[0][6:])
        self.assertEqual(event["usage"], {"completion_tokens": 12})

    def test_stream_supplies_missing_call_id_without_changing_arguments(self):
        args = {"path": "README.md"}
        events = [{"id": "fixture-stream", "created": 1, "model": "minicpm-v-4.7:1b",
                   "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0,
                       "function": {"name": "read", "arguments": json.dumps(args)}}]},
                       "finish_reason": "tool_calls"}]}]
        response = self.request({}, [tool("read", ["path"])], True, events)
        choices = self.stream_choices(response)
        call = choices[-1]["delta"]["tool_calls"][0]
        self.assertTrue(call["id"])
        self.assertEqual(call["function"]["name"], "read")
        self.assertEqual(json.loads(call["function"]["arguments"]), args)
        self.assertEqual(choices[-1]["finish_reason"], "tool_calls")

    def test_none_blocks_upstream_calls_in_both_response_formats(self):
        for streamed in (False, True):
            with self.subTest(streamed=streamed):
                upstream = completion('read', {'path': 'README.md'})
                choice = upstream['choices'][0]
                event = {**upstream, 'choices': [{
                    'index': 0, 'delta': choice['message'], 'finish_reason': 'tool_calls'}]}
                response = self.request(upstream, [tool('read', ['path'])], streamed,
                                        [event], extra={'tool_choice': 'none'})
                result = self.stream_choices(response)[-1] if streamed else response.json['choices'][0]
                self.assertEqual(result['finish_reason'], 'stop')
                message = result['delta' if streamed else 'message']
                self.assertNotIn('tool_calls', message)
                self.assertIn('disabled', message['content'])
                self.assertEqual(self.post.call_args.kwargs['json']['tools'], [])

    def test_runtime_typed_strings_are_restored_in_both_formats(self):
        schema = {"type": "object", "properties": {
            "enabled": {"type": "boolean"}, "count": {"type": "integer"},
            "label": {"type": "string"}, "flags": {"type": "array", "items": {"type": "boolean"}},
            "config": {"type": "object", "properties": {"active": {"type": "boolean"}}}}}
        offered = [{"type": "function", "function": {"name": "save_settings", "parameters": schema}}]
        args = {"enabled": "True", "count": "3", "label": "True", "flags": ["False"],
                "config": {"active": "true"}}
        expected = {"enabled": True, "count": 3, "label": "True", "flags": [False],
                    "config": {"active": True}}
        for streamed in (False, True):
            with self.subTest(streamed=streamed):
                upstream = completion("save_settings", args)
                event = {**upstream, "choices": [{"index": 0,
                    "delta": upstream["choices"][0]["message"], "finish_reason": "tool_calls"}]}
                response = self.request(upstream, offered, streamed, [event])
                choice = self.stream_choices(response)[-1] if streamed else response.json["choices"][0]
                actual = json.loads(choice["delta" if streamed else "message"]["tool_calls"][0]["function"]["arguments"])
                self.assertEqual(actual, expected)
                self.assertIs(actual["enabled"], True)

    def test_textual_boolean_and_invalid_values(self):
        body = {"tools": [{"function": {"name": "save_settings", "parameters": {
            "type": "object", "properties": {"enabled": {"type": "boolean"}}}}}]}
        _, calls = shim._repair_textual_tool_calls(
            '<tool_call><function=save_settings><parameter=enabled>True</parameter></function></tool_call>', body)
        self.assertIs(json.loads(calls[0]["function"]["arguments"])["enabled"], True)
        for value in ("yes", "1", "", 1, None):
            self.assertEqual(shim._restore_schema_types(value, {"type": "boolean"}), value)
        self.assertEqual(shim._restore_schema_types("True", {"type": ["boolean", "string"]}), "True")

    def test_nested_opencode_question_choices_survive_fragmented_stream(self):
        from tools.opencode_scenarios import QUESTION
        args = {"questions": [{"question": "Which features?", "header": "Features",
            "options": [{"label": "API", "description": "HTTP endpoints"},
                        {"label": "CLI", "description": "Terminal interface"}], "multiple": "True"}]}
        raw = json.dumps(args)
        for streamed in (False, True):
            with self.subTest(streamed=streamed):
                events = [
                    {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0,
                        "id": "question-call", "type": "function",
                        "function": {"name": "question", "arguments": raw[:35]}}]}}]},
                    {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0,
                        "function": {"arguments": raw[35:]}}]}, "finish_reason": "tool_calls"}]},
                ]
                response = self.request(completion("question", args), [QUESTION], streamed, events)
                choice = self.stream_choices(response)[-1] if streamed else response.json["choices"][0]
                fn = choice["delta" if streamed else "message"]["tool_calls"][0]["function"]
                actual = json.loads(fn["arguments"])
                self.assertEqual(fn["name"], "question")
                self.assertIs(actual["questions"][0]["multiple"], True)
                self.assertEqual(actual["questions"][0]["options"], args["questions"][0]["options"])

    def test_patch_endpoint_preserves_embedded_paths(self):
        args = {"patchText": "*** Begin Patch\n*** Update File: src/a.py\n@@\n-old\n+new\n*** End Patch"}
        response = self.request(completion("apply_patch", args), [tool("apply_patch", ["patchText"])])
        choice = response.json["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        call = choice["message"]["tool_calls"][0]
        self.assertEqual(json.loads(call["function"]["arguments"]), args)

    def test_empty_runtime_call_does_not_crash_streaming_client(self):
        events = [{"id": "fixture-stream", "created": 1, "model": "minicpm-v-4.7:1b",
                   "choices": [{"index": 0, "delta": {"content": "Fixed and verified.", "tool_calls": [
                       {"index": 0, "id": "empty-call", "function": {"name": "", "arguments": "{}"}}]},
                       "finish_reason": "tool_calls"}], "usage": {"completion_tokens": 9}}]
        response = self.request({}, [tool("edit", ["path", "oldString", "newString"])], True, events)
        choices = self.stream_choices(response)
        self.assertEqual(choices[-1]["finish_reason"], "stop")
        self.assertEqual(choices[-1]["delta"], {"content": "Fixed and verified."})
        self.assertNotIn("tool_calls", response.data.decode())


if __name__ == "__main__":
    unittest.main()
