import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import model_resource_warnings as warnings


class ResourceWarningTests(unittest.TestCase):
    def test_warnings_match_backend_and_model_without_marking_other_variants(self):
        models = [{'backend': 'fastflowlm', 'id': 'FLM/QWEN3.8-MTP:27B'},
                  {'backend': 'lmstudio', 'id': 'flm/qwen3.8-mtp:27b'},
                  {'backend': 'fastflowlm', 'id': 'flm/gemma4e-flash:e4b'},
                  {'backend': 'fastflowlm', 'id': 'flm/gemma4-it:e4b'}]
        original = copy.deepcopy(models)
        result = warnings.annotate(models, {'fastflowlm', 'lmstudio'})
        self.assertIn('32.8 GiB', result[0]['resource_warning']['summary'])
        self.assertNotIn('resource_warning', result[1])
        self.assertNotIn('resource_warning', result[2])
        self.assertIn('19.7 GiB', result[3]['resource_warning']['summary'])
        self.assertEqual(models, original)

    def test_local_observations_do_not_follow_models_to_remote_servers(self):
        model = {'backend': 'lemonade', 'id': 'NVIDIA-Nemotron-3.5-Lightning-30B-A3B-GGUF'}
        self.assertNotIn('resource_warning', warnings.annotate([model], set())[0])
        result = warnings.annotate([model], {'lemonade'})[0]['resource_warning']
        self.assertIn('12 threads', result['summary'])
        self.assertIn('not recorded', result['summary'])
        self.assertTrue(all(e['source'] and e['observed_at'] and e['excerpt'] for e in result['evidence']))

    def test_cpu_service_measurements_include_startup_and_keep_backends_separate(self):
        models = [{'backend': 'vllm', 'id': 'Qwen/Qwen3-0.6B'},
                  {'backend': 'kobold', 'id': 'koboldcpp/Qwen3-0.6B-Q8_0'},
                  {'backend': 'lmstudio', 'id': 'qwen3-0.6b'}]
        result = warnings.annotate(models, {'vllm', 'kobold', 'lmstudio'})
        self.assertIn('7.7 CPU cores', result[0]['resource_warning']['summary'])
        self.assertIn('startup', result[0]['resource_warning']['summary'])
        self.assertIn('5.6 CPU cores', result[1]['resource_warning']['summary'])
        self.assertNotIn('resource_warning', result[2])

    def test_missing_catalog_does_not_break_the_models_inventory(self):
        models = [{'backend': 'lemonade', 'id': 'fixture'}]
        with patch.object(warnings, 'CATALOG', Path('/does-not-exist/hearth-resource-warnings.json')):
            self.assertEqual(warnings.annotate(models, {'lemonade'}), models)
