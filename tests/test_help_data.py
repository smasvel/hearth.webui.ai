import json
import unittest
from unittest.mock import Mock, patch

import help_data


class TailscaleHelpTests(unittest.TestCase):
    @patch.object(help_data.model_metadata_store, "get_model_configured_context", return_value=131072)
    def test_loaded_context_wins_over_saved_maximum(self, saved):
        model = {"backend": "lemonade", "id": "Muse", "state": "loaded",
                 "loaded_context_length": 12495, "configured_context_length": 131072}
        self.assertEqual(help_data._configured_or_loaded_context(model), 12495)
        saved.assert_not_called()
        model["state"] = "not-loaded"
        self.assertEqual(help_data._configured_or_loaded_context(model), 131072)

    @patch.object(help_data.settings_store, "get", side_effect=lambda key:
                  "true" if key == "opencode_compaction_auto" else None)
    def test_auto_compaction_can_be_enabled_explicitly(self, _get):
        cfg = json.loads(help_data.build_opencode_config(
            "203.0.113.10", "test-key", help_data._DEFAULT_PORTS,
            {"lemonade": ["Muse"]}, {}))
        self.assertTrue(cfg["compaction"]["auto"])

    def test_help_context_uses_supported_maximum_not_loading_size(self):
        cfg = json.loads(help_data.build_opencode_config(
            "203.0.113.10", "test-key", help_data._DEFAULT_PORTS,
            {"lemonade": ["large", "small"]}, {},
            ctx_by_model={("lemonade", "large"): 8192, ("lemonade", "small"): 8192},
            max_ctx_by_model={("lemonade", "large"): 131072,
                              ("lemonade", "small"): 32768}))
        entries = cfg["provider"]["lemonade"]["models"]
        self.assertEqual(entries["large"]["limit"]["context"], 65536)
        self.assertEqual(entries["small"]["limit"]["context"], 8192)

    @patch.object(help_data.settings_store, "get", return_value=None)
    def test_small_lemonade_window_bounds_compaction_without_flm(self, _get):
        for model in ('Muse-Glimmer-30B-GGUF', 'GLM-4.7-Flash-GGUF'):
            with self.subTest(model=model):
                cfg = json.loads(help_data.build_opencode_config(
                    '203.0.113.10', 'test-key', help_data._DEFAULT_PORTS,
                    {'lemonade': [model]}, {}, network_label='Tailscale',
                    ctx_by_model={('lemonade', model): 4096}))
                self.assertEqual(cfg['provider']['lemonade']['models'][model]['limit'],
                                 {'context': 4096, 'output': 2048})
                self.assertEqual(cfg['compaction'],
                                 {'auto': False, 'keep': {'tokens': 1024}, 'buffer': 1024})

    @patch.object(help_data.settings_store, "get", side_effect=lambda key:
                  '32768' if key == 'opencode_compaction_buffer' else None)
    def test_mixed_backends_bound_global_buffer_to_smallest_window(self, _get):
        cfg = json.loads(help_data.build_opencode_config(
            '203.0.113.10', 'test-key', help_data._DEFAULT_PORTS,
            {'lemonade': ['Muse'], 'lmstudio': ['small']}, {},
            ctx_by_model={('lemonade', 'Muse'): 65536, ('lmstudio', 'small'): 4096}))
        self.assertEqual(cfg['compaction'],
                         {'auto': False, 'keep': {'tokens': 1024}, 'buffer': 1024})

    @patch.object(help_data.flm_context, "context_limit", side_effect=lambda model=None: 262144 if model and "minicpm" in model else 8192)
    def test_continue_uses_model_specific_flm_limits(self, _limit):
        result = help_data.build_continue_yaml("203.0.113.10", "test-key", help_data._DEFAULT_PORTS,
            {"fastflowlm": ["flm/minicpm-v-4.7:1b", "flm/qwen3.8-mtp:27b"]}, [], network_label="Tailscale")
        mini, qwen = result.split("  - name: ")[1:3]
        self.assertIn("contextLength: 262144", mini)
        self.assertIn("contextLength: 8192", qwen)

    def test_continue_uses_saved_context_over_backend_default(self):
        result = help_data.build_continue_yaml(
            "203.0.113.10", "test-key", help_data._DEFAULT_PORTS,
            {"lemonade": ["Muse-Glimmer-30B-GGUF"]}, [], network_label="Tailscale",
            ctx_by_model={("lemonade", "Muse-Glimmer-30B-GGUF"): 131072})
        self.assertIn("contextLength: 131072", result)
        self.assertNotIn("contextLength: 65536", result)

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


class HelpDataCacheTests(unittest.TestCase):
    def setUp(self):
        help_data._help_cache.clear()
        self.addCallback = self.addCleanup(help_data._help_cache.clear)

    @patch.object(help_data, "_build_uncached")
    def test_fresh_cache_returned_without_rebuild(self, build_mock):
        build_mock.return_value = {"lan_ip": "192.0.2.1"}
        first = help_data.build()
        second = help_data.build()
        self.assertEqual(build_mock.call_count, 1)
        self.assertIs(first, second)

    @patch.object(help_data, "_build_uncached")
    @patch.object(help_data.threading, "Thread")
    def test_stale_data_returned_while_background_refresh_runs(self, thread_mock, build_mock):
        cached = {"lan_ip": "192.0.2.1"}
        help_data._help_cache["data"] = cached
        help_data._help_cache["ts"] = __import__("time").monotonic() - help_data.HELP_DATA_TTL - 1
        result = help_data.build()
        self.assertIs(result, cached)  # served instantly, never blocking on a rebuild
        self.assertTrue(help_data._help_cache.get("refreshing"))
        self.assertTrue(thread_mock.return_value.start.called)

    @patch.object(help_data, "_build_uncached")
    def test_rebuild_failure_leaves_previous_snapshot_served(self, build_mock):
        cached = {"lan_ip": "192.0.2.1"}
        help_data._help_cache["data"] = cached
        help_data._help_cache["ts"] = __import__("time").monotonic() - help_data.HELP_DATA_TTL - 1
        build_mock.side_effect = RuntimeError("tailscake down")
        help_data._refresh(5443)  # _build_uncached raises
        self.assertFalse(help_data._help_cache.get("refreshing"))
        self.assertEqual(help_data._help_cache.get("data"), cached)


class OpenCodeConsoleConfigTests(unittest.TestCase):
    @patch.object(help_data.settings_store, "get", return_value=None)
    @patch.object(help_data.zen, "api_key", return_value="")
    def test_console_providers_absent_without_a_key(self, _key, _setting):
        cfg = json.loads(help_data.build_opencode_config(
            "203.0.113.10", "test-key", help_data._DEFAULT_PORTS,
            {"lemonade": ["Muse"]}, {},
            ctx_by_model={("lemonade", "Muse"): 65536}))
        self.assertNotIn("opencode", cfg["provider"])

    @patch.object(help_data.settings_store, "get", return_value=None)
    @patch.object(help_data.zen, "list_models", return_value=[
        {"id": "opencode/glm-5.3", "name": "GLM 5.3",
         "configured_context_length": 200000, "output_tokens_limit": 32768}])
    @patch.object(help_data.zen, "api_key", return_value="test-key")
    def test_console_provider_embeds_key_windows_and_output(self, _key, _models, _setting):
        cfg = json.loads(help_data.build_opencode_config(
            "203.0.113.10", "test-key", help_data._DEFAULT_PORTS,
            {"lemonade": []}, {}))
        provider = cfg["provider"]["opencode"]
        self.assertEqual(provider["options"]["baseURL"],
                         "https://opencode.ai/inference/openai/v1")
        self.assertEqual(provider["options"]["apiKey"], "test-key")
        self.assertEqual(provider["models"]["glm-5.3"]["limit"],
                         {"context": 200000, "output": 32768})

    @patch.object(help_data.settings_store, "get", return_value=None)
    @patch.object(help_data.zen, "list_models",
                  return_value=[{"id": "opencode/x", "configured_context_length": None}])
    @patch.object(help_data.zen, "api_key", return_value="test-key")
    def test_console_unknown_context_falls_back_to_safe_floor(self, _key, _models, _setting):
        cfg = json.loads(help_data.build_opencode_config(
            "203.0.113.10", "test-key", help_data._DEFAULT_PORTS, {}, {}))
        entry = cfg["provider"]["opencode"]["models"]["x"]
        self.assertEqual(entry["limit"]["context"], 65536)
        self.assertLessEqual(entry["limit"]["output"], 65536 // 4)


if __name__ == "__main__":
    unittest.main()
