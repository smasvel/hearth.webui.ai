"""Shared compaction formula and the desktop OpenCode config sync tool."""
import json
import unittest
from unittest.mock import patch

import help_data

BE, MODEL = 'lemonade', 'Muse-Glimmer-30B-GGUF'


class CompactionForWindowTests(unittest.TestCase):
    def test_65k_window_keeps_room_for_the_compaction_request(self):
        budgets = help_data.compaction_for_window(65536)
        self.assertEqual(budgets, {'keep': 40960, 'buffer': 16384})
        self.assertLessEqual(budgets['keep'] + budgets['buffer'] + 4096, 65536)

    def test_65k_buffer_covers_a_single_large_step(self):
        # A production agent step reached 10,830 tokens; the buffer (headroom left
        # when auto-compaction fires) must be larger than such a step.
        self.assertGreater(help_data.compaction_for_window(65536)['buffer'], 11000)

    def test_large_windows_cap_keep_at_65536(self):
        self.assertEqual(help_data.compaction_for_window(131072),
                         {'keep': 65536, 'buffer': 32768})

    def test_output_reserve_cannot_shrink_the_safety_cushion(self):
        # A small output reserve must not shrink the buffer below window // 4:
        # the cushion exists to absorb a single large step, not just the reserve.
        self.assertEqual(help_data.compaction_for_window(65536, 4096),
                         {'keep': 40960, 'buffer': 16384})

    def test_small_windows_scale_to_one_eighth(self):
        for window, expected in ((8192, (1024, 1024)), (32768, (4096, 4096))):
            with self.subTest(window=window):
                budgets = help_data.compaction_for_window(window)
                self.assertEqual((budgets['keep'], budgets['buffer']), expected)


class SyncConfigTests(unittest.TestCase):
    def _run(self, existing, generated):
        import io
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
                         {'auto': False, 'keep': {'tokens': 40960}, 'buffer': 16384})

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
