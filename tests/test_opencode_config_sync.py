"""Shared compaction formula and the desktop OpenCode config sync tool."""
import json
import unittest
from unittest.mock import patch

import help_data

BE, MODEL = 'lemonade', 'Muse-Glimmer-30B-GGUF'


class CompactionForWindowTests(unittest.TestCase):
    def test_65k_window_keeps_room_for_the_compaction_request(self):
        budgets = help_data.compaction_for_window(65536)
        self.assertEqual(budgets, {'keep': 49152, 'buffer': 8192})
        self.assertLessEqual(budgets['keep'] + budgets['buffer'] + 4096, 65536)

    def test_large_windows_cap_keep_at_65536(self):
        self.assertEqual(help_data.compaction_for_window(131072),
                         {'keep': 65536, 'buffer': 8192})

    def test_output_reserve_scales_the_buffer(self):
        self.assertEqual(help_data.compaction_for_window(65536, 4096),
                         {'keep': 53248, 'buffer': 4096})

    def test_small_windows_scale_to_one_eighth(self):
        for window, expected in ((8192, (1024, 1024)), (32768, (4096, 4096))):
            with self.subTest(window=window):
                budgets = help_data.compaction_for_window(window)
                self.assertEqual((budgets['keep'], budgets['buffer']), expected)


class SyncConfigTests(unittest.TestCase):
    def _run(self, existing, generated):
        import sys as _sys
        tempfile = __import__('tempfile')
        from pathlib import Path
        _sys.path.insert(0, str(Path(help_data.__file__).parents[0] / 'tools'))
        import sync_opencode_config as sync
        _sys.path.pop(0)
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / 'opencode.jsonc'
            if existing is not None:
                config_path.write_text(json.dumps(existing, indent=2))
            with patch.object(sync, 'CONFIG_PATH', config_path), \
                 patch.object(sync.help_data, 'build',
                              return_value={'opencode_config': json.dumps(generated)}):
                sync.main()
            return config_path.read_text()

    def test_selected_model_and_compaction_are_derived_and_preserved(self):
        existing = {'model': f'{BE}/{MODEL}',
                    'agents': {'custom': {'mode': 'primary'}}}
        generated = {'model': 'lmstudio/other-model',
                     'provider': {BE: {'models': {MODEL: {
                         'limit': {'context': 65536, 'output': 8192}}}}},
                     'compaction': {'auto': True, 'keep': {'tokens': 1024}, 'buffer': 1024}}
        result = json.loads(self._run(existing, generated))
        self.assertEqual(result['model'], f'{BE}/{MODEL}')
        self.assertIn('custom', result.get('agents', {}))
        self.assertEqual(result['compaction'],
                         {'auto': True, 'keep': {'tokens': 49152}, 'buffer': 8192})

    def test_idempotent_when_config_matches_generation(self):
        first = self._run(
            {'model': f'{BE}/{MODEL}'},
            {'model': f'{BE}/{MODEL}',
             'provider': {BE: {'models': {MODEL: {
                 'limit': {'context': 65536, 'output': 8192}}}}}})
        second = self._run(json.loads(first), json.loads(first))
        self.assertEqual(json.loads(second), json.loads(first))


if __name__ == '__main__':
    unittest.main()
