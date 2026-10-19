import unittest
from unittest.mock import Mock, patch

import lemonade_templates
import lemonade_guard_shim


class DevstralTemplateTests(unittest.TestCase):
    @patch.object(lemonade_templates.Path, 'glob')
    def test_amd_memory_detection_handles_missing_and_large_pools(self, glob):
        path = Mock()
        glob.return_value = [path]
        path.read_text.return_value = str(23471587328)
        self.assertTrue(lemonade_templates._small_amd_memory_budget())
        path.read_text.return_value = str(64 * 1024**3)
        self.assertFalse(lemonade_templates._small_amd_memory_budget())
        path.read_text.side_effect = OSError('unavailable')
        self.assertFalse(lemonade_templates._small_amd_memory_budget())
        glob.return_value = []
        self.assertFalse(lemonade_templates._small_amd_memory_budget())

    @patch.object(lemonade_templates, '_small_amd_memory_budget', return_value=True)
    def test_nemotron_uses_cpu_without_losing_context_or_arguments(self, budget):
        current = {'ctx_size': 65536, 'llamacpp_backend': 'vulkan', 'llamacpp_args': '--parallel 1'}
        model = 'NVIDIA-Nemotron-3.5-Lightning-30B-A3B-GGUF'
        options = lemonade_templates.load_options(model, current)
        self.assertEqual(options, {**current, 'llamacpp_backend': 'cpu'})
        self.assertEqual(current['llamacpp_backend'], 'vulkan')
        self.assertEqual(lemonade_templates.load_options(model, options), options)
        self.assertEqual(lemonade_templates.load_options('other-model', current), {})

    @patch.object(lemonade_templates, '_small_amd_memory_budget', return_value=False)
    def test_nemotron_keeps_gpu_on_other_hardware(self, budget):
        self.assertEqual(lemonade_templates.load_options('NVIDIA-Nemotron-3.5-Lightning-30B-A3B-GGUF'), {})

    def test_scoped_override_preserves_context_and_is_idempotent(self):
        current = {"ctx_size": 63569, "llamacpp_args": "--parallel 1"}
        options = lemonade_templates.load_options("Devstral-Small-2507-GGUF", current)
        self.assertEqual(options["ctx_size"], 63569)
        self.assertIn("[TOOL_CALLS]", options["llamacpp_args"])
        self.assertEqual(lemonade_templates.load_options("Devstral-Small-2507-GGUF", options), options)
        self.assertEqual(current["llamacpp_args"], "--parallel 1")
        self.assertEqual(lemonade_templates.load_options("other-model", current), {})

    @patch.object(lemonade_guard_shim.requests, "post")
    @patch.object(lemonade_guard_shim.requests, "get")
    def test_loaded_model_is_repaired_once(self, get, post):
        model = "Devstral-Small-2507-GGUF"
        options = {"ctx_size": 63569, "llamacpp_args": "--parallel 1"}
        response = Mock()
        response.json.return_value = {"all_models_loaded": [
            {"model_name": model, "loaded": True, "recipe_options": options}
        ]}
        get.return_value = response
        lemonade_guard_shim._unload_before_implicit_load(model)
        post.assert_called_once_with(
            f"{lemonade_guard_shim.LEMONADE_BASE}/v1/load",
            json={"model_name": model, **lemonade_templates.load_options(model, options)},
            timeout=300,
        )
        post.reset_mock()
        options.update(lemonade_templates.load_options(model, options))
        lemonade_guard_shim._unload_before_implicit_load(model)
        post.assert_not_called()
