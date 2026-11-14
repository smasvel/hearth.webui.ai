"""Shared compaction formula and the desktop OpenCode config sync tool."""
import json
import unittest
from unittest.mock import patch

from hearth.core import help_data

BE, MODEL = 'lemonade', 'Muse-Glimmer-30B-GGUF'


class CompactionForWindowTests(unittest.TestCase):
    def test_65k_window_derives_keep_and_buffer_as_context_percentages(self):
        budgets = help_data.compaction_for_window(65536)
        self.assertEqual(budgets, {'keep': 59051, 'buffer': 5461})
        prompt_reserve = 65536 // 64
        self.assertLessEqual(budgets['keep'] + budgets['buffer'] + prompt_reserve, 65536)

    def test_large_windows_keep_and_buffer_scale_with_context(self):
        self.assertEqual(help_data.compaction_for_window(131072),
                         {'keep': 118102, 'buffer': 10922})

    def test_small_windows_use_the_same_percentages(self):
        for window, expected in ((8192, (7382, 682)), (32768, (29526, 2730))):
            with self.subTest(window=window):
                budgets = help_data.compaction_for_window(window)
                self.assertEqual((budgets['keep'], budgets['buffer']), expected)

    def test_configured_budgets_default_to_formula_and_bound_overrides(self):
        self.assertEqual(help_data.configured_compaction_for_window(65536),
                         {'keep': 59051, 'buffer': 5461})
        self.assertEqual(help_data.configured_compaction_for_window(
            65536, keep_override=12000, buffer_override=32000),
            {'keep': 12000, 'buffer': 16384})


class SyncConfigTests(unittest.TestCase):
    def _run(self, existing, generated):
        import io
        import sys as _sys
        tempfile = __import__('tempfile')
        from pathlib import Path
        _sys.path.insert(0, str(Path(help_data.__file__).parents[1]))
        from tools import sync_opencode_config as sync
        _sys.path.pop(0)
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / 'opencode.jsonc'
            if existing is not None:
                config_path.write_text(json.dumps(existing, indent=2))
            with patch.object(sync, 'CONFIG_PATH', config_path), \
                 patch.object(sync.help_data, 'build',
                              return_value={'opencode_config': json.dumps(generated)}), \
                 patch.object(sync.help_data, 'compaction_settings',
                              return_value={'auto': True, 'keep_tokens': None, 'buffer': None}):
                old_stdout = _sys.stdout
                _sys.stdout = io.StringIO()
                try:
                    sync.main()
                    output = _sys.stdout.getvalue()
                finally:
                    _sys.stdout = old_stdout
            return output

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
                         {'auto': True, 'keep': {'tokens': 59051}, 'buffer': 5461})

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
