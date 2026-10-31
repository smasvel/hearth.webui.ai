import json
import unittest
from unittest.mock import Mock, patch

import help_data


class TailscaleHelpTests(unittest.TestCase):
    @patch.object(help_data.settings_store, "get", return_value=None)
    def test_small_lemonade_window_bounds_compaction_without_flm(self, _get):
        for model in ('Muse-Glimmer-30B-GGUF', 'GLM-4.7-Flash-GGUF'):
            with self.subTest(model=model):
                cfg = json.loads(help_data.build_opencode_config(
                    '100.1.2.3', 'test-key', help_data._DEFAULT_PORTS,
                    {'lemonade': [model]}, {}, network_label='Tailscale',
                    ctx_by_model={('lemonade', model): 4096}))
                self.assertEqual(cfg['provider']['lemonade']['models'][model]['limit'],
                                 {'context': 4096, 'output': 2048})
                self.assertEqual(cfg['compaction'],
                                 {'auto': True, 'keep': {'tokens': 512}, 'buffer': 512})

    @patch.object(help_data.settings_store, "get", side_effect=lambda key:
                  '32768' if key == 'opencode_compaction_buffer' else None)
    def test_mixed_backends_bound_global_buffer_to_smallest_window(self, _get):
        cfg = json.loads(help_data.build_opencode_config(
            '100.1.2.3', 'test-key', help_data._DEFAULT_PORTS,
            {'lemonade': ['Muse'], 'lmstudio': ['small']}, {},
            ctx_by_model={('lemonade', 'Muse'): 65536, ('lmstudio', 'small'): 4096}))
        self.assertEqual(cfg['compaction'],
                         {'auto': True, 'keep': {'tokens': 512}, 'buffer': 1024})

    @patch.object(help_data.flm_context, "context_limit", side_effect=lambda model=None: 262144 if model and "minicpm" in model else 8192)
    def test_continue_uses_model_specific_flm_limits(self, _limit):
        result = help_data.build_continue_yaml("100.1.2.3", "test-key", help_data._DEFAULT_PORTS,
            {"fastflowlm": ["flm/minicpm-v-4.7:1b", "flm/qwen3.8-mtp:27b"]}, [], network_label="Tailscale")
        mini, qwen = result.split("  - name: ")[1:3]
        self.assertIn("contextLength: 262144", mini)
        self.assertIn("contextLength: 8192", qwen)

    @patch.object(help_data.subprocess, "run")
    def test_hardcoded_default_port_does_not_select_another_app(self, run):
        domain = "machine.example.ts.net"
        run.return_value = Mock(returncode=0, stdout=json.dumps({
            "TCP": {"443": {"HTTPS": True}, "8443": {"HTTPS": True}, "10000": {"HTTPS": True}},
            "Web": {
                domain + ":443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:5050"}}},
                domain + ":10000": {"Handlers": {"/": {"Proxy": "https+insecure://127.0.0.1:5000"}}},
                domain + ":8443": {"Handlers": {"/": {"Proxy": "https+insecure://127.0.0.1:5443"}}},
            },
        }))
        self.assertEqual(help_data.tailscale_app_url(domain, 5443), "https://" + domain + ":8443")

    @patch.object(help_data.subprocess, "run", return_value=Mock(returncode=1, stdout=""))
    def test_no_dns_link_is_invented_when_serve_is_unavailable(self, _run):
        self.assertIsNone(help_data.tailscale_app_url("machine.example.ts.net", 5443))


if __name__ == "__main__":
    unittest.main()
