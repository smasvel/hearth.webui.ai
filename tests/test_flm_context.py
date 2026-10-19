import copy
import json
import unittest
from unittest.mock import patch

import flm_context as context
import flm_tool_shim as shim
import help_data
import flm_serve


class ContextTests(unittest.TestCase):
    def setUp(self):
        paused = patch.object(shim.fastflowlm, 'requests_paused', return_value=False)
        paused.start()
        self.addCleanup(paused.stop)

    def test_launcher_uses_minicpm_maximum_without_raising_qwen_kernel_limit(self):
        self.assertEqual(flm_serve.command('minicpm-v-4.7:1b')[-2], '262144')
        self.assertEqual(flm_serve.command('flm/minicpm-v-4.7:1b')[-2], '262144')
        self.assertEqual(flm_serve.command('qwen3.8-mtp:27b')[-2], '8192')
        self.assertEqual(flm_serve.command('gemma4-it:e4b')[-2], '131072')
        self.assertEqual(flm_serve.command('gemma4e-flash:e4b')[-2], '1024')
        self.assertEqual(flm_serve.command('llama3.2:1b')[-2], '131072')
        self.assertEqual(flm_serve.command('qwen3-it:4b')[-2], '32768')
        self.assertEqual(flm_serve.command('flm/qwen3-it:4b')[-2], '32768')
        self.assertEqual(flm_serve.command('unknown-model')[-2], '8192')

    def test_schema_annotations_removed_without_changing_constraints_or_property_names(self):
        body = {'messages': [{'role': 'user', 'content': 'Do not change my instructions.'}],
                'tools': [{'function': {'name': 'edit', 'description': 'Never overwrite unrelated work.',
                    'parameters': {'type': 'object', 'title': 'Verbose schema', 'properties': {
                        'title': {'type': 'string', 'description': 'New title', 'examples': ['Example']},
                        'payload': {'const': {'title': 'Keep', 'examples': [1]}}
                    }, 'required': ['title'], 'additionalProperties': False}}}]}
        original = copy.deepcopy(body)
        prepared = context.compact_tools(body)
        schema = prepared['tools'][0]['function']['parameters']
        self.assertNotIn('title', schema)
        self.assertIn('title', schema['properties'])
        self.assertEqual(schema['properties']['payload']['const'], {'title': 'Keep', 'examples': [1]})
        self.assertEqual(schema['required'], ['title'])
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(prepared['messages'], original['messages'])
        self.assertEqual(prepared['tools'][0]['function']['description'], original['tools'][0]['function']['description'])
        self.assertEqual(body, original)

    @patch.object(context, 'context_limit', return_value=8192)
    @patch.object(context, 'prompt_tokens', return_value=7000)
    def test_reply_budget_leaves_room_for_input_and_padding(self, *_):
        prepared, error = context.prepare({'max_tokens': 4096})
        self.assertIsNone(error)
        self.assertEqual(prepared['max_tokens'], 936)

    @patch.object(context, 'context_limit', return_value=8192)
    @patch.object(context, 'prompt_tokens', return_value=8000)
    def test_oversized_prompt_has_actionable_error(self, *_):
        _, error = context.prepare({})
        self.assertEqual(error['error']['code'], 'context_length_exceeded')
        self.assertIn('flm-small', error['error']['message'])
        self.assertIn('8000', error['error']['message'])

    @patch.object(context, 'context_limit', return_value=8192)
    @patch.object(context, 'prompt_tokens', return_value=None)
    def test_unknown_tokenizer_does_not_guess_and_reject(self, *_):
        prepared, error = context.prepare({'max_completion_tokens': 4096})
        self.assertIsNone(error)
        self.assertEqual(prepared['max_completion_tokens'], 2048)
        self.assertNotIn('max_tokens', prepared)

    @patch.object(context, 'context_limit', return_value=8192)
    @patch.object(context, 'prompt_tokens', return_value=1000)
    def test_qwen38_larger_reply_cap_and_default_preserve_explicit_smaller_requests(self, *_):
        for model in ('qwen3.8-mtp:27b', 'flm/qwen3.8-mtp:27b'):
            for key in ('max_tokens', 'max_completion_tokens'):
                prepared, error = context.prepare({'model': model, key: 8192})
                self.assertIsNone(error)
                self.assertEqual(prepared[key], 4096)
                self.assertNotIn('max_completion_tokens' if key == 'max_tokens' else 'max_tokens', prepared)
            prepared, error = context.prepare({'model': model})
            self.assertIsNone(error)
            self.assertEqual(prepared['max_tokens'], 4096)
            prepared, _ = context.prepare({'model': model, 'max_tokens': 128})
            self.assertEqual(prepared['max_tokens'], 128)

    @patch.object(context, 'context_limit', return_value=8192)
    @patch.object(context, 'prompt_tokens', return_value=6000)
    def test_qwen38_output_shrinks_for_long_input_without_discarding_messages(self, *_):
        body = {'model': 'qwen3.8-mtp:27b', 'max_tokens': 8192,
                'messages': [{'role': 'user', 'content': 'Keep this entire input.'}]}
        original = copy.deepcopy(body)
        prepared, error = context.prepare(body)
        self.assertIsNone(error)
        self.assertEqual(prepared['max_tokens'], 1936)
        self.assertEqual(prepared['messages'], body['messages'])
        self.assertEqual(body, original)
        self.assertEqual(context.output_limit('qwen3.8-mtp:27b', 1024), 512)
        self.assertEqual(context.output_limit('minicpm-v-4.7:1b', 262144), 2048)

    @patch.object(context, 'context_limit', return_value=8192)
    @patch.object(context, 'prompt_tokens', return_value=7800)
    def test_small_reply_can_use_more_of_input_window(self, *_):
        prepared, error = context.prepare({'model': 'qwen3.8-mtp:27b', 'max_tokens': 128})
        self.assertIsNone(error)
        self.assertEqual(prepared['max_tokens'], 128)
        _, error = context.prepare({'model': 'qwen3.8-mtp:27b', 'max_tokens': 4096})
        self.assertEqual(error['error']['code'], 'context_length_exceeded')

    def test_media_requests_skip_text_only_budget(self):
        self.assertIsNone(context.prompt_tokens({'model': 'qwen3.8-mtp:27b', 'messages': [
            {'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'test'}}]}]}))

    @patch.object(shim.flm_context, 'prepare', return_value=({}, context.overflow_error(8192, 9000)))
    @patch.object(shim.npu_request_lock, 'acquire')
    def test_oversized_prompt_is_rejected_before_claiming_npu(self, acquire, _prepare):
        response = shim.app.test_client().post('/v1/chat/completions', json={'messages': []})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json['error']['code'], 'context_length_exceeded')
        acquire.assert_not_called()

    @patch.object(context, 'context_limit', return_value=8192)
    @patch.object(help_data.settings_store, 'get', return_value=None)
    def test_generated_config_has_small_agent_and_safe_compaction(self, *_):
        cfg = json.loads(help_data.build_opencode_config('100.1.2.3', 'test-key', help_data._DEFAULT_PORTS,
            {'fastflowlm': ['flm/qwen3.8-mtp:27b']}, {}, ctx_by_model={('fastflowlm', 'flm/qwen3.8-mtp:27b'): 65536}))
        self.assertEqual(cfg['default_agent'], 'flm-small')
        self.assertEqual(cfg['compaction']['keep']['tokens'], 1024)
        self.assertEqual(cfg['compaction']['buffer'], 1024)
        limit = cfg['provider']['fastflowlm']['models']['flm/qwen3.8-mtp:27b']['limit']
        self.assertEqual(limit, {'context': 8192, 'output': 4096})
        agent = cfg['agent']['flm-small']
        self.assertEqual(agent['permission']['*'], 'deny')
        self.assertEqual(agent['permission']['edit'], 'allow')
        self.assertEqual(agent['permission']['doom_loop'], 'ask')
        self.assertEqual(agent['permission']['question'], 'allow')

    @patch.object(context, 'context_limit', side_effect=lambda model=None: 262144 if model is None or 'minicpm' in model else 1024 if 'flash' in model else 8192)
    @patch.object(help_data.settings_store, 'get', side_effect=lambda key: '32768' if key == 'opencode_compaction_buffer' else None)
    def test_large_running_model_does_not_set_buffer_above_qwen_window(self, *_):
        cfg = json.loads(help_data.build_opencode_config('100.1.2.3', 'test', help_data._DEFAULT_PORTS,
            {'fastflowlm': ['flm/minicpm-v-4.7:1b', 'flm/qwen3.8-mtp:27b', 'flm/gemma4e-flash:e4b']},
            {'fastflowlm': ['flm/minicpm-v-4.7:1b']}))
        self.assertEqual(cfg['compaction']['buffer'], 2048)
        self.assertEqual(cfg['compaction']['keep']['tokens'], 1024)
