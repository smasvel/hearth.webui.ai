import unittest
from unittest.mock import patch

from hearth.backends import vllm_client as vllm


class VLLMReloadDefaultTests(unittest.TestCase):
    def _scenario(self, served_id, served_len, service_env_value):
        with patch.object(vllm, "_current_served_model", return_value=served_id), \
             patch.object(vllm, "_served_max_model_len", return_value=served_len), \
             patch.object(vllm, "_service_config_value", return_value=service_env_value):
            return vllm._current_reload_default("TinyLlama/TinyLlama-1.1B-Chat-v1.0")

    def test_reloading_same_model_prefers_live_window(self):
        self.assertEqual(self._scenario(
            "TinyLlama/TinyLlama-1.1B-Chat-v1.0", 2048, 4096
        ), 2048)

    def test_service_env_falls_back_for_a_stopped_model(self):
        self.assertEqual(self._scenario(
            None, None, 2048
        ), 2048)


class VLLMLoadResolutionTests(unittest.TestCase):
    def test_load_resolves_live_window_over_registry(self):
        """TinyLlama cannot serve at Hearth's 4,096 default; the registry paired with the
        container can hold a larger, stale value. A reload of that same served model must
        prefer the live window so vLLM does not immediately crash-loop again."""
        with patch.object(vllm, "_load_registry", return_value={
            "TinyLlama/TinyLlama-1.1B-Chat-v1.0": {"max_model_len": 4096},
        }), patch.object(vllm, "_current_reload_default", return_value=2048), \
           patch.object(vllm, "_register") as registered, \
           patch.object(vllm, "_write_service_config") as write, \
           patch.object(vllm, "_systemctl") as systemctl, \
           patch.object(vllm, "_current_served_model", return_value="TinyLlama/TinyLlama-1.1B-Chat-v1.0"):
            vllm.load_model("TinyLlama/TinyLlama-1.1B-Chat-v1.0")
        self.assertEqual(write.call_args.args, ("TinyLlama/TinyLlama-1.1B-Chat-v1.0", 2048, "bfloat16"))
        self.assertEqual(systemctl.call_args.args[:1], ("restart",))


if __name__ == '__main__':
    unittest.main()
