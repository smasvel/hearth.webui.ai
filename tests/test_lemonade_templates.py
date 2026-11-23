import unittest
from unittest.mock import Mock, patch

from hearth.backends import lemonade_templates
from hearth.backends import lemonade_guard_shim


class DevstralTemplateTests(unittest.TestCase):
    @patch.object(lemonade_templates.Path, 'glob')
    def test_amd_memory_detection_handles_missing_and_large_pools(self, glob):
        path = Mock()
        glob.return_value = [path]
        path.read_text.return_value = str(23471587328)
        self.assertEqual(lemonade_templates._amd_shared_gpu_budget_bytes(), 23471587328)
        path.read_text.return_value = str(64 * 1024**3)
        self.assertEqual(lemonade_templates._amd_shared_gpu_budget_bytes(), 64 * 1024**3)
        path.read_text.side_effect = OSError('unavailable')
        self.assertIsNone(lemonade_templates._amd_shared_gpu_budget_bytes())
        glob.return_value = []
        self.assertIsNone(lemonade_templates._amd_shared_gpu_budget_bytes())

    @patch.object(lemonade_templates, '_amd_shared_gpu_budget_bytes', return_value=24 * 1024**3)
    def test_nemotron_uses_cpu_without_losing_context_or_arguments(self, budget):
        current = {'ctx_size': 65536, 'llamacpp_backend': 'vulkan', 'llamacpp_args': '--parallel 1'}
        model = 'NVIDIA-Nemotron-3.5-Lightning-30B-A3B-GGUF'
        options = lemonade_templates.load_options(model, current)
        self.assertEqual(options, {**current, 'llamacpp_backend': 'cpu'})
        self.assertEqual(current['llamacpp_backend'], 'vulkan')
        self.assertEqual(lemonade_templates.load_options(model, options), options)
        self.assertEqual(lemonade_templates.load_options('other-model', current), {})

    @patch.object(lemonade_templates, '_amd_shared_gpu_budget_bytes', return_value=64 * 1024**3)
    def test_nemotron_keeps_gpu_on_other_hardware(self, budget):
        self.assertEqual(lemonade_templates.load_options('NVIDIA-Nemotron-3.5-Lightning-30B-A3B-GGUF'), {})

    @patch.object(lemonade_templates, '_amd_shared_gpu_budget_bytes', return_value=24 * 1024**3)
    def test_ornith_35b_uses_cpu_but_9b_keeps_default(self, budget):
        current = {'ctx_size': 65536, 'llamacpp_backend': 'vulkan', 'llamacpp_args': '--parallel 1'}
        options = lemonade_templates.load_options('Ornith-1.5-35B-A3B-GGUF', current)
        self.assertEqual(options, {**current, 'llamacpp_backend': 'cpu'})
        self.assertEqual(current['llamacpp_backend'], 'vulkan')
        self.assertEqual(lemonade_templates.load_options('Ornith-1.5-9B-GGUF', current), {})

    @patch.object(lemonade_templates, '_amd_shared_gpu_budget_bytes', return_value=64 * 1024**3)
    def test_ornith_keeps_gpu_on_other_hardware(self, budget):
        self.assertEqual(lemonade_templates.load_options('Ornith-1.5-35B-A3B-GGUF'), {})

    @patch.object(lemonade_templates, '_amd_shared_gpu_budget_bytes', return_value=24 * 1024**3)
    def test_new_large_models_use_cpu_by_weight_size(self, budget):
        self.assertEqual(
            lemonade_templates.load_options('Muse-Glimmer-30B-GGUF', model_size_gb=20),
            {'llamacpp_backend': 'cpu'},
        )
        self.assertEqual(
            lemonade_templates.load_options('small-model', model_size_gb=4),
            {},
        )

    @patch.object(lemonade_templates, '_amd_shared_gpu_budget_bytes', return_value=24 * 1024**3)
    def test_qwen_27b_uses_rocm_when_installed_and_reduces_attention_cache(self, budget):
        model = 'PhillyLabs-Qwen3.8-27B-ABLITERATED-UNCENSORED-PHILADELPHIA-CLASS'
        current = {'ctx_size': 65536, 'llamacpp_args': '--parallel 1'}

        options = lemonade_templates.load_options(
            model, current, model_size_gb=15.4, rocm_available=True,
        )

        self.assertEqual(options['llamacpp_backend'], 'rocm')
        self.assertEqual(options['ctx_size'], 65536)
        self.assertIn('--flash-attn on', options['llamacpp_args'])
        self.assertIn('--cache-type-k q8_0', options['llamacpp_args'])
        self.assertIn('--cache-type-v q8_0', options['llamacpp_args'])
        self.assertIn('--parallel 1', options['llamacpp_args'])
        self.assertEqual(current['llamacpp_args'], '--parallel 1')

    @patch.object(lemonade_templates, '_amd_shared_gpu_budget_bytes', return_value=24 * 1024**3)
    def test_qwen_27b_keeps_safe_cpu_fallback_when_rocm_is_unavailable(self, budget):
        options = lemonade_templates.load_options(
            'Qwen3.8-27B-GGUF', model_size_gb=15.4, rocm_available=False,
        )

        self.assertEqual(options['llamacpp_backend'], 'cpu')
        self.assertIn('--flash-attn on', options['llamacpp_args'])
        self.assertIn('--cache-type-v q8_0', options['llamacpp_args'])

    def test_scoped_override_preserves_context_and_is_idempotent(self):
        current = {"ctx_size": 63569, "llamacpp_args": "--parallel 1"}
        options = lemonade_templates.load_options("Devstral-Small-2507-GGUF", current)
        self.assertEqual(options["ctx_size"], 63569)
        self.assertIn("[TOOL_CALLS]", options["llamacpp_args"])
        self.assertEqual(lemonade_templates.load_options("Devstral-Small-2507-GGUF", options), options)
        self.assertEqual(current["llamacpp_args"], "--parallel 1")
        self.assertEqual(lemonade_templates.load_options("other-model", current), {})

    @patch.object(lemonade_guard_shim.model_metadata_store, "get_model_configured_context", return_value=None)
    @patch.object(lemonade_guard_shim.requests, "post")
    @patch.object(lemonade_guard_shim.requests, "get")
    def test_loaded_model_is_repaired_once(self, get, post, saved_context):
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
            timeout=600,
        )
        post.reset_mock()
        options.update(lemonade_templates.load_options(model, options))
        lemonade_guard_shim._unload_before_implicit_load(model)
        post.assert_not_called()

    @patch.object(lemonade_guard_shim.model_metadata_store, "get_model_configured_context", return_value=65536)
    @patch.object(lemonade_guard_shim.requests, "post")
    @patch.object(lemonade_guard_shim.requests, "get")
    def test_opencode_implicit_load_uses_saved_context(self, get, post, saved_context):
        get.return_value.json.return_value = {"all_models_loaded": []}

        lemonade_guard_shim._unload_before_implicit_load("Muse-Glimmer-30B-GGUF")

        post.assert_called_once_with(
            f"{lemonade_guard_shim.LEMONADE_BASE}/v1/load",
            json={"model_name": "Muse-Glimmer-30B-GGUF", "ctx_size": 65536},
            timeout=600,
        )

    @patch.object(lemonade_guard_shim.model_metadata_store, "get_model_configured_context", return_value=65536)
    @patch.object(lemonade_guard_shim.requests, "post")
    @patch.object(lemonade_guard_shim.requests, "get")
    def test_opencode_reloads_loaded_model_at_saved_context(self, get, post, saved_context):
        get.return_value.json.return_value = {"all_models_loaded": [{
            "model_name": "Muse-Glimmer-30B-GGUF",
            "loaded": True,
            "recipe_options": {"ctx_size": 6144},
        }]}

        lemonade_guard_shim._unload_before_implicit_load("Muse-Glimmer-30B-GGUF")

        post.assert_called_once_with(
            f"{lemonade_guard_shim.LEMONADE_BASE}/v1/load",
            json={"model_name": "Muse-Glimmer-30B-GGUF", "ctx_size": 65536},
            timeout=600,
        )
