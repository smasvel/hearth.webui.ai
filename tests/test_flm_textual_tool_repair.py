import json
import unittest

import flm_tool_shim


TOOLS_BODY = {
    "tools": [{
        "type": "function",
        "function": {"name": "playwright__navigate", "parameters": {"type": "object"}},
    }]
}


class TextualToolRepairTests(unittest.TestCase):
    def test_repairs_native_parameter_markup(self):
        text = (
            "Checking now.\n<tool_call><function=playwright__navigate>"
            "<parameter=url>https://example.com</parameter></function></tool_call>"
        )
        clean, calls = flm_tool_shim._repair_textual_tool_calls(text, TOOLS_BODY)
        self.assertEqual(clean, "Checking now.")
        self.assertEqual(calls[0]["function"]["name"], "playwright__navigate")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"url": "https://example.com"})

    def test_repairs_direct_argument_shorthand(self):
        text = "<tool_call><function=playwright__navigate><url=greatclips.com></function></tool_call>"
        clean, calls = flm_tool_shim._repair_textual_tool_calls(text, TOOLS_BODY)
        self.assertEqual(clean, "")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"url": "greatclips.com"})

    def test_repairs_json_payload(self):
        text = '<tool_call>{"function":"playwright__navigate","url":"https://example.com"}</tool_call>'
        _, calls = flm_tool_shim._repair_textual_tool_calls(text, TOOLS_BODY)
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"url": "https://example.com"})

    def test_repairs_json_name_and_nested_arguments(self):
        text = '<tool_call>{"name":"playwright__navigate","arguments":{"url":"https://example.com"}}</tool_call>'
        _, calls = flm_tool_shim._repair_textual_tool_calls(text, TOOLS_BODY)
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"url": "https://example.com"})

    def test_mixed_markup_preserves_call_order_and_prose(self):
        text = ('```json\n{"name":"playwright__navigate","arguments":{"url":"first"}}\n```'
                ' then <tool_call>{"function":"playwright__navigate","url":"second"}</tool_call>')
        clean, calls = flm_tool_shim._repair_textual_tool_calls(text, TOOLS_BODY)
        self.assertEqual(clean, 'then')
        self.assertEqual([json.loads(c['function']['arguments'])['url'] for c in calls], ['first', 'second'])

    def test_llama_xml_single_parameter_requires_unambiguous_schema(self):
        text = '<tool_call><function>get_weather</function><parameter>Boston</parameter></tool_call>'
        body = {'tools': [{'function': {'name': 'get_weather', 'parameters': {
            'properties': {'city': {'type': 'string'}}}}}]}
        clean, calls = flm_tool_shim._repair_textual_tool_calls(text, body)
        self.assertEqual(clean, '')
        self.assertEqual(json.loads(calls[0]['function']['arguments']), {'city': 'Boston'})
        body['tools'][0]['function']['parameters']['properties']['country'] = {'type': 'string'}
        self.assertEqual(flm_tool_shim._repair_textual_tool_calls(text, body), (text, []))

    def test_gemma_native_arguments_preserve_strings_and_nested_types(self):
        text = ('<|tool_call>call:playwright__navigate{url:<|"|>https://example.com/{x}"<|"|>,'
                'config:{enabled:true,count:3,tags:[<|"|>a,b<|"|>,null]}}<tool_call|>')
        clean, calls = flm_tool_shim._repair_textual_tool_calls(text, TOOLS_BODY)
        self.assertEqual(clean, '')
        self.assertEqual(json.loads(calls[0]['function']['arguments']), {
            'url': 'https://example.com/{x}"', 'config': {'enabled': True, 'count': 3, 'tags': ['a,b', None]}})

    def test_gemma_rejects_truncated_unknown_and_disabled_calls(self):
        for text, body in [
            ('<|tool_call>call:playwright__navigate{url:<|"|>bad}<tool_call|>', TOOLS_BODY),
            ('<|tool_call>call:unknown{}<tool_call|>', TOOLS_BODY),
            ('<|tool_call>call:playwright__navigate{}<tool_call|>', {**TOOLS_BODY, 'tool_choice': 'none'})]:
            self.assertEqual(flm_tool_shim._repair_textual_tool_calls(text, body), (text, []))

    def test_rejects_unoffered_tool(self):
        text = "<tool_call><function=dangerous><parameter=x>1</parameter></function></tool_call>"
        clean, calls = flm_tool_shim._repair_textual_tool_calls(text, TOOLS_BODY)
        self.assertEqual(clean, text)
        self.assertEqual(calls, [])

    def test_none_never_recovers_executable_calls(self):
        text = '<tool_call>{"name":"playwright__navigate","arguments":{}}</tool_call>'
        self.assertEqual(flm_tool_shim._repair_textual_tool_calls(
            text, {**TOOLS_BODY, "tool_choice": "none"}), (text, []))

    def test_preserves_unrecognized_blocks_beside_recovered_call(self):
        unknown = '<tool_call>{"name":"unknown","arguments":{}}</tool_call>'
        known = '<tool_call>{"name":"playwright__navigate","arguments":{}}</tool_call>'
        clean, calls = flm_tool_shim._repair_textual_tool_calls(unknown + known, TOOLS_BODY)
        self.assertEqual(clean, unknown)
        self.assertEqual(len(calls), 1)

    def test_xml_parameters_keep_schema_types(self):
        values = {"count": ("integer", "3", 3), "active": ("boolean", "true", True),
                  "items": ("array", '[1,2]', [1, 2]),
                  "config": ("object", '{"x":1}', {"x": 1}),
                  "id": ("string", "123", "123"),
                  "union": (["string", "null"], "123", "123")}
        body = {"tools": [{"type": "function", "function": {
            "name": "typed", "parameters": {"properties": {
                k: {"type": v[0]} for k, v in values.items()}}}}]}
        text = '<tool_call><function=typed>' + ''.join(
            f'<parameter={k}>{v[1]}</parameter>' for k, v in values.items()) + '</function></tool_call>'
        _, calls = flm_tool_shim._repair_textual_tool_calls(text, body)
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]),
                         {k: v[2] for k, v in values.items()})

    def test_repairs_stream_and_removes_raw_markup(self):
        obj = {"id": "x", "model": "m", "choices": [{"index": 0, "delta": {
            "content": "<tool_call><function=playwright__navigate><url=greatclips.com></function></tool_call>"
        }, "finish_reason": None}]}
        raw = [b"data: " + json.dumps(obj).encode() + b"\n", b"data: [DONE]\n"]
        repaired = flm_tool_shim._repair_streamed_textual_tool_calls(raw, TOOLS_BODY)
        self.assertIsNotNone(repaired)
        self.assertNotIn(b"<tool_call>", repaired)
        self.assertIn(b'"finish_reason":"tool_calls"', repaired)
        self.assertIn(b'"playwright__navigate"', repaired)
        self.assertTrue(all(event.startswith(b"data: ") for event in repaired.strip().split(b"\n\n")))


if __name__ == "__main__":
    unittest.main()
