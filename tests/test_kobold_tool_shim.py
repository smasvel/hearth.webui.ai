import json
import unittest
from unittest.mock import Mock, patch

import kobold_tool_shim as shim


class KoboldToolCompatibilityTests(unittest.TestCase):
    @staticmethod
    def parallel_body():
        return {
            "parallel_tool_calls": True,
            "tools": [{"type": "function", "function": {"name": "get_weather", "parameters": {
                "type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}],
            "messages": [{"role": "user", "content":
                "In this one response emit TWO get_weather tool calls: Boston first, then Paris. "
                "Do not wait for the first result."}],
        }

    def test_explicit_parallel_request_recovers_only_the_missing_literal_call(self):
        body = self.parallel_body()
        contract = shim._parallel_contract(body)
        self.assertEqual(contract, ("get_weather", "city", ["Boston", "Paris"]))
        calls, changed = shim._normalize_calls([{
            "id": "kobold-call", "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city":"Boston"}'},
        }], contract)
        self.assertTrue(changed)
        self.assertEqual([(c["function"]["name"], json.loads(c["function"]["arguments"])) for c in calls], [
            ("get_weather", {"city": "Boston"}), ("get_weather", {"city": "Paris"}),
        ])
        self.assertEqual(calls[0]["id"], "kobold-call")
        self.assertTrue(calls[1]["id"].startswith("call_hearth_"))

    def test_parallel_recovery_requires_one_explicit_required_string_field(self):
        body = self.parallel_body()
        body["tools"][0]["function"]["parameters"]["properties"]["units"] = {"type": "string"}
        self.assertIsNone(shim._parallel_contract(body))

    def test_no_more_tools_turn_withholds_schema_but_not_for_forced_choice(self):
        body = {"tools": [{"type": "function"}], "messages": [{"role": "user", "content":
            "The verified result was 4 passed. Do not call more tools."}]}
        self.assertTrue(shim.suppress_tools_for_explicit_text_turn(body))
        body["tool_choice"] = "required"
        self.assertFalse(shim.suppress_tools_for_explicit_text_turn(body))

    def test_explicit_text_turn_removes_any_unexpected_tool_call(self):
        body = {"tools": [{"type": "function"}], "messages": [{"role": "user", "content":
            "The verified result was 4 passed. Do not call more tools."}]}
        payload = {"choices": [{"message": {"content": None, "tool_calls": [{"id": "x"}]},
                                "finish_reason": "tool_calls"}]}
        self.assertTrue(shim._repair_completion(payload, body))
        self.assertEqual(payload["choices"][0]["message"], {"content": ""})
        self.assertEqual(payload["choices"][0]["finish_reason"], "stop")

    def test_proxy_repairs_nonstream_parallel_response(self):
        upstream = Mock(status_code=200, headers={"content-type": "application/json"}, content=b"original")
        upstream.json.return_value = {"choices": [{"message": {"tool_calls": [{
            "id": "kobold-call", "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city":"Boston"}'},
        }]}, "finish_reason": "tool_calls"}]}
        with patch.object(shim, "_upstream_url", return_value="http://kobold"), \
                patch.object(shim, "_headers", return_value={}), \
                patch.object(shim.requests, "post", return_value=upstream):
            response = shim.app.test_client().post("/v1/chat/completions", json=self.parallel_body())
        self.assertEqual(response.status_code, 200)
        calls = response.get_json()["choices"][0]["message"]["tool_calls"]
        self.assertEqual([json.loads(call["function"]["arguments"]) for call in calls],
                         [{"city": "Boston"}, {"city": "Paris"}])

    def test_proxy_withholds_tools_on_explicit_text_only_followup(self):
        payload = {"choices": [{"message": {"content": "4 tests passed"}, "finish_reason": "stop"}]}
        upstream = Mock(status_code=200, headers={"content-type": "application/json"},
                         content=json.dumps(payload).encode())
        upstream.json.return_value = payload
        body = {"tools": [{"type": "function"}], "messages": [{"role": "user",
            "content": "The verified result was 4 tests passed. Do not call more tools."}]}
        with patch.object(shim, "_upstream_url", return_value="http://kobold"), \
                patch.object(shim, "_headers", return_value={}), \
                patch.object(shim.requests, "post", return_value=upstream) as post:
            response = shim.app.test_client().post("/v1/chat/completions", json=body)
        self.assertEqual(response.status_code, 200)
        sent = post.call_args.kwargs["json"]
        self.assertNotIn("tools", sent)
        self.assertNotIn("tool_choice", sent)
        self.assertEqual(response.get_json()["choices"][0]["message"]["content"], "4 tests passed")

    def test_stream_passthrough_keeps_sse_event_boundaries(self):
        upstream = Mock(status_code=200, headers={"content-type": "text/event-stream"})
        upstream.iter_lines.return_value = [b'data: {"choices":[{"delta":{"content":"ok"}}]}',
                                            b'data: [DONE]']
        body = {"stream": True, "messages": [{"role": "user", "content": "Reply ok."}]}
        with patch.object(shim, "_upstream_url", return_value="http://kobold"), \
                patch.object(shim, "_headers", return_value={}), \
                patch.object(shim.requests, "post", return_value=upstream):
            response = shim.app.test_client().post("/v1/chat/completions", json=body)
        self.assertIn(b'\n\n', response.data)
        self.assertIn(b'data: [DONE]', response.data)
        events = [line[6:] for line in response.data.decode().splitlines()
                  if line.startswith("data: ") and line != "data: [DONE]"]
        self.assertEqual(json.loads(events[0])["choices"][0]["delta"]["content"], "ok")

    def test_stream_proxy_requests_final_usage_chunk(self):
        upstream = Mock(status_code=200, headers={"content-type": "text/event-stream"})
        upstream.iter_lines.return_value = [b'data: {"choices":[{"delta":{"content":"ok"}}]}',
                                            b'data: [DONE]']
        body = {"stream": True, "messages": [{"role": "user", "content": "Reply ok."}]}
        with patch.object(shim, "_upstream_url", return_value="http://kobold"), \
                patch.object(shim, "_headers", return_value={}), \
                patch.object(shim.requests, "post", return_value=upstream) as post:
            response = shim.app.test_client().post("/v1/chat/completions", json=body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(post.call_args.kwargs["json"]["stream_options"], {"include_usage": True})


if __name__ == "__main__":
    unittest.main()
