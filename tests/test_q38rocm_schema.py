"""Contracts for the installed q38rocm server's OpenAI schema differences."""
import copy
import unittest
from unittest.mock import Mock, patch

import q38rocm_client as q38
from tools.test_flm_model import WEATHER, function, run_suite
from tools import test_flm_model as harness


class Q38SchemaTests(unittest.TestCase):
    def test_inventory_reports_live_context_separately_from_model_maximum(self):
        path = Mock()
        path.is_file.return_value = True
        path.stat.return_value.st_size = 13500737504
        with patch.object(q38, '_MODEL_PATH', path), \
             patch.object(q38, '_server_model', return_value={
                 'id': 'served-id', 'meta': {'n_ctx': 65536}}):
            model = q38.list_models()[0]
        self.assertEqual(model['loaded_context_length'], 65536)
        self.assertEqual(model['configured_context_length'], 65536)
        self.assertEqual(model['max_context_length'], 262144)

    def test_decode_preserves_backend_speed_instead_of_counting_prefill_time(self):
        response = Mock()
        response.json.return_value = {'choices': [{'message': {'content': '4'},
                                                   'finish_reason': 'stop'}],
            'usage': {'completion_tokens': 1},
            'timings': {'predicted_per_second': 2.77, 'prompt_per_second': 49.0}}
        _, _, _, usage = harness.decode(response, False)
        self.assertEqual(usage['completion_tokens'], 1)
        self.assertEqual(usage['decoding_speed_tps'], 2.77)
        self.assertEqual(harness._metric_tokens_per_second(usage, 10), 2.8)

    def test_cli_uses_q38_endpoint_and_does_not_fail_on_deliberate_skip(self):
        with patch('sys.argv', ['test', '--backend', 'q38rocm', '--model', 'served-id']), \
             patch.object(q38, 'base_url', return_value='http://q38-local:8008'), \
             patch.object(q38, '_headers', return_value={'Authorization': 'Bearer fixture'}), \
             patch.object(harness, 'run_suite', return_value=[{
                 'passed': False, 'skipped': True}]) as run:
            self.assertEqual(harness.main(), 0)
        self.assertEqual(run.call_args.args[1], 'http://q38-local:8008/v1/chat/completions')
        self.assertFalse(run.call_args.kwargs['guarded'])
        self.assertEqual(run.call_args.kwargs['backend'], 'q38rocm')
        self.assertEqual(run.call_args.kwargs['headers'], {'Authorization': 'Bearer fixture'})

    def test_named_choice_restricts_schema_without_changing_history(self):
        other = function('ping', {})
        body = {'messages': [{'role': 'user', 'content': 'Use a tool'}],
                'tools': [other, WEATHER], 'max_tokens': 2048,
                'tool_choice': {'type': 'function', 'function': {'name': 'get_weather'}}}
        original = copy.deepcopy(body)
        wire = q38.prepare_request(body)
        self.assertEqual(wire['tools'], [WEATHER])
        self.assertEqual(wire['tool_choice'], 'required')
        self.assertEqual(wire['max_tokens'], 2048)
        self.assertEqual(body, original)
        self.assertEqual(q38.prepare_request(wire), wire)

    def test_unoffered_named_choice_fails_before_sending(self):
        with self.assertRaisesRegex(q38.Q38RocmError, 'offered function'):
            q38.prepare_request({'tools': [WEATHER], 'tool_choice': {
                'type': 'function', 'function': {'name': 'missing'}}})

    def test_thinking_switch_is_mapped_to_template_for_both_states(self):
        for enabled in (True, False):
            body = {'enable_thinking': enabled,
                    'chat_template_kwargs': {'preserve_thinking': True}}
            wire = q38.prepare_request(body)
            self.assertNotIn('enable_thinking', wire)
            self.assertIs(wire['chat_template_kwargs']['enable_thinking'], enabled)
            self.assertTrue(wire['chat_template_kwargs']['preserve_thinking'])
            self.assertEqual(body['chat_template_kwargs'], {'preserve_thinking': True})

    def test_stream_usage_and_explicit_sampling_are_preserved(self):
        wire = q38.prepare_request({'stream': True, 'frequency_penalty': 0.2,
                                    'stream_options': {'other': True}})
        self.assertEqual(wire['stream_options'], {'include_usage': True, 'other': True})
        self.assertEqual(wire['frequency_penalty'], 0.2)

    def test_harness_uses_named_adapter_and_original_budget(self):
        response = Mock()
        response.json.return_value = {'choices': [{'finish_reason': 'tool_calls', 'message': {
            'tool_calls': [{'id': 'call-1', 'function': {
                'name': 'get_weather', 'arguments': '{"city":"Boston"}'}}]}}],
            'usage': {'completion_tokens': 12}}
        choice_name = "tool-choice-{'type': 'function', 'function': {'name': 'get_weather'}}"
        with patch('tools.test_flm_model.requests.post', return_value=response) as post, \
             patch('tools.test_flm_model._quiet', return_value=None):
            results = run_suite('served-id', 'http://backend/v1/chat/completions',
                suite='tools', only={(choice_name, False)}, backend='q38rocm', guarded=False)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]['passed'], results)
        wire = post.call_args.kwargs['json']
        self.assertEqual(wire['tool_choice'], 'required')
        self.assertEqual(wire['tools'], [WEATHER])
        self.assertEqual(wire['max_tokens'], 2048)
        self.assertFalse(wire['chat_template_kwargs']['enable_thinking'])


if __name__ == '__main__':
    unittest.main()
