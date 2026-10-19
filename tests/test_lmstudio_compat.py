import copy
import unittest
from unittest.mock import patch

import lmstudio_shim as shim
from wire_lmstudio_proxy import upstream_paths


class CompatibilityTests(unittest.TestCase):
    def body(self):
        return {'model': 'test', 'messages': [{'role': 'user', 'content': 'Call echo'}],
                'max_tokens': 256, 'tools': [
                    {'type': 'function', 'function': {'name': 'echo', 'parameters': {'type': 'object'}}},
                    {'type': 'function', 'function': {'name': 'other'}}],
                'tool_choice': {'type': 'function', 'function': {'name': 'echo'}}}

    def test_named_choice_filters_only_selected_function_and_preserves_budget(self):
        body = self.body(); original = copy.deepcopy(body)
        prepared = shim.normalize_tool_choice(body)
        self.assertEqual(prepared['tool_choice'], 'required')
        self.assertEqual(prepared['tools'], body['tools'][:1])
        self.assertEqual(prepared['max_tokens'], 256)
        self.assertEqual(prepared['messages'], body['messages'])
        self.assertEqual(body, original)

    def test_string_choices_are_not_changed(self):
        for choice in ('auto', 'none', 'required'):
            body = {'tool_choice': choice}
            self.assertIs(shim.normalize_tool_choice(body), body)

    def test_malformed_named_choice_is_rejected(self):
        with self.assertRaises(ValueError):
            shim.normalize_tool_choice({'tool_choice': {'type': 'function', 'function': 'echo'}})

    def test_unknown_function_rejected_before_model_request(self):
        body = self.body(); body['tool_choice']['function']['name'] = 'not_offered'
        with patch.object(shim.requests, 'post') as post:
            response = shim.app.test_client().post('/v1/chat/completions', json=body)
            self.assertEqual(response.status_code, 400)
            post.assert_not_called()

    def test_routing_only_changes_local_lmstudio_upstreams(self):
        config = {'a': [{'upstreams': [{'dial': 'localhost:1234'}, {'dial': 'localhost:52626'},
                                      {'dial': 'external.example:1234'}]}]}
        self.assertEqual(list(upstream_paths(config)), ['/a/0/upstreams/0/dial'])
