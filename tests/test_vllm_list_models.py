import unittest
from unittest.mock import patch

from hearth.backends import vllm_client as vllm


class VLLMListModelsTests(unittest.TestCase):
    def test_unregistered_served_model_reports_live_max_model_len(self):
        with patch.object(vllm, "_vllm_server_model_info", return_value={
            "id": "TinyLlama/TinyLlama-1.1B-Chat-v1.0", "max_model_len": 2048,
        }), patch.object(vllm, "_load_registry", return_value={}), \
           patch.object(vllm, "_cached_repo_sizes_gb", return_value={}):
            models = vllm.list_models()
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["id"], "TinyLlama/TinyLlama-1.1B-Chat-v1.0")
        self.assertEqual(models[0]["state"], "loaded")
        self.assertEqual(models[0]["configured_context_length"], 2048)
        self.assertEqual(models[0]["loaded_context_length"], 2048)

    def test_registered_model_used_for_out_of_band_starts(self):
        with patch.object(vllm, "_vllm_server_model_info", return_value={
            "id": "TinyLlama/TinyLlama-1.1B-Chat-v1.0", "max_model_len": 2048,
        }), patch.object(vllm, "_load_registry", return_value={
            "TinyLlama/TinyLlama-1.1B-Chat-v1.0": {"max_model_len": 8192},
        }), patch.object(vllm, "_cached_repo_sizes_gb", return_value={}):
            models = vllm.list_models()
        self.assertEqual(models[0]["configured_context_length"], 8192)
        self.assertEqual(models[0]["loaded_context_length"], 8192)


if __name__ == '__main__':
    unittest.main()
