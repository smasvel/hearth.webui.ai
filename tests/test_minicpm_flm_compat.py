import copy
import json
import unittest
from unittest.mock import Mock

import flm_tool_shim as shim
from tools.test_flm_model import decode


ARGS = {"todos": [{"content": "Implement the requested work", "status": "in_progress", "priority": "high"}]}
TOOLS = [{"type": "function", "function": {"name": "todowrite", "parameters": {
    "type": "object", "properties": {"todos": {"type": "array"}}, "required": ["todos"]}}}]


def request(model="flm/minicpm-v-4.7:1b"):
    return {"model": model, "tools": copy.deepcopy(TOOLS),
            "tool_choice": {"type": "function", "function": {"name": "todowrite"}},
            "messages": [{"role": "user", "content":
                "Next call todowrite with exactly these arguments: " + json.dumps(ARGS) + "."}]}

WEATHER = [{"type": "function", "function": {"name": "get_weather", "parameters": {
    "type": "object", "properties": {"city": {"type": "string"}},
    "required": ["city"]}}}]


class MiniCPMFastFlowCompatibilityTests(unittest.TestCase):
    def test_named_choice_keeps_exact_arguments_and_removes_duplicate_calls(self):
        payload = {"choices": [{"finish_reason": "tool_calls", "message": {"content": "", "tool_calls": [
            {"id": "first", "type": "function", "function": {"name": "todowrite", "arguments": "{}"}},
            {"id": "extra", "type": "function", "function": {"name": "todowrite", "arguments": "{}"}},
        ]}}]}
        self.assertTrue(shim._repair_tool_arguments(payload, request()))
        calls = payload["choices"][0]["message"]["tool_calls"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["id"], "first")
        self.assertEqual(calls[0]["function"]["name"], "todowrite")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), ARGS)

    def test_named_choice_recovers_when_model_returns_only_prose(self):
        payload = {"choices": [{"finish_reason": "stop", "message": {"content": "I will update the todo."}}]}
        self.assertTrue(shim._repair_tool_arguments(payload, request()))
        choice = payload["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertIsNone(choice["message"]["content"])
        self.assertEqual(json.loads(choice["message"]["tool_calls"][0]["function"]["arguments"]), ARGS)

    def test_streamed_named_choice_recovers_exact_call(self):
        body = request()
        event = {"id": "chunk-1", "model": body["model"], "choices": [{"index": 0,
                 "delta": {"content": "I will update the todo."}, "finish_reason": "stop"}]}
        lines = [b"data: " + json.dumps(event).encode() + b"\n", b"data: [DONE]\n"]
        repaired = shim._guard_streamed_tool_calls(lines, body)
        self.assertIsNotNone(repaired)
        calls, _, finishes, _ = decode(Mock(content=repaired), True)
        self.assertEqual(finishes, ["tool_calls"])
        self.assertEqual(calls[0]["name"], "todowrite")
        self.assertEqual(calls[0]["arguments"], ARGS)

    def test_parallel_repair_uses_only_the_two_explicit_values_and_is_minicpm_only(self):
        body = {"model": "flm/minicpm-v-4.7:1b", "tools": copy.deepcopy(WEATHER),
                "parallel_tool_calls": True,
                "messages": [{"role": "user", "content":
                    "In this one response emit TWO get_weather tool calls: Boston first, then Paris. Do not wait for the first result."}]}
        payload = {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
            {"id": "weather-1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}
        ]}}]}
        self.assertTrue(shim._repair_tool_arguments(payload, body))
        calls = payload["choices"][0]["message"]["tool_calls"]
        self.assertEqual([json.loads(call["function"]["arguments"]) for call in calls],
                         [{"city": "Boston"}, {"city": "Paris"}])
        other = copy.deepcopy(payload)
        other_body = {**body, "model": "flm/qwen3.8-mtp:27b"}
        self.assertFalse(shim._minicpm_parallel_arguments(other_body))

    def test_parallel_repair_does_not_override_named_choice(self):
        body = {"model": "flm/minicpm-v-4.7:1b", "tools": copy.deepcopy(WEATHER),
                "parallel_tool_calls": True,
                "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
                "messages": [{"role": "user", "content":
                    "In this one response emit TWO get_weather tool calls: Boston first, then Paris."}]}
        self.assertIsNone(shim._minicpm_parallel_arguments(body))

    def test_direct_answer_is_tool_free_only_for_minicpm(self):
        body = {"model": "flm/minicpm-v-4.7:1b", "tools": TOOLS,
                "messages": [{"role": "user", "content": "What is 2+2? Answer directly."}]}
        self.assertTrue(shim._minicpm_direct_answer_turn(body))
        other = {**body, "model": "flm/qwen3.8-mtp:27b"}
        self.assertFalse(shim._minicpm_direct_answer_turn(other))
        tool_result = {"model": "flm/minicpm-v-4.7:1b", "tools": TOOLS,
                       "messages": [{"role": "user", "content": "Weather in Boston?"},
                                   {"role": "assistant", "tool_calls": [{"id": "c1"}]},
                                   {"role": "tool", "tool_call_id": "c1", "content": "21 C sunny"}]}
        self.assertFalse(shim._minicpm_direct_answer_turn(tool_result))
        bounded_task = {**tool_result, "messages": [tool_result["messages"][0]] + [
            item for i in range(6) for item in (
                {"role": "assistant", "tool_calls": [{"id": f"c{i}"}]},
                {"role": "tool", "tool_call_id": f"c{i}", "content": f"result {i}"},
            )]}
        self.assertTrue(shim._minicpm_direct_answer_turn(bounded_task))
        self.assertFalse(shim._minicpm_direct_answer_turn({**tool_result, "model": "flm/qwen3.8-mtp:27b"}))

    def test_structured_calls_are_removed_after_tool_result(self):
        body = {"model": "flm/minicpm-v-4.7:1b", "tools": copy.deepcopy(TOOLS),
                "tool_choice": "none",
                "messages": [{"role": "tool", "tool_call_id": "c1", "content": "Page loaded"}]}
        payload = {"choices": [{"finish_reason": "tool_calls", "message": {"content": None,
            "tool_calls": [{"id": "c2", "type": "function", "function": {
                "name": "playwright__browse_tab", "arguments": "{}"}}]}}]}
        self.assertTrue(shim._repair_tool_arguments(payload, body))
        choice = payload["choices"][0]
        self.assertEqual(choice["finish_reason"], "stop")
        self.assertNotIn("tool_calls", choice["message"])
        self.assertIn("Tool calling is disabled", choice["message"]["content"])

    def test_minicpm_prompt_is_scoped_and_does_not_duplicate_schemas(self):
        body = {"model": "flm/minicpm-v-4.7:1b", "tools": copy.deepcopy(TOOLS),
                "messages": [{"role": "user", "content": "Call todowrite."}]}
        changed = shim._inject_tools_block(body)
        self.assertIn("FastFlowLM MiniCPM tool contract", changed["messages"][0]["content"])
        self.assertEqual(changed["tools"], body["tools"])
        other = {**body, "model": "flm/qwen3.8-mtp:27b"}
        self.assertIs(shim._inject_tools_block(other), other)


if __name__ == "__main__":
    unittest.main()
