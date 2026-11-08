import unittest
from unittest.mock import patch

import public_model_catalog as catalog


class PublicCatalogTests(unittest.TestCase):
    def test_lemonade_matches_words_and_removes_file_suffix(self):
        data = {
            "Nemotron-3-GGUF": {"recipe": "llamacpp", "checkpoint": "org/Nemotron-3-GGUF:Q4.gguf", "labels": ["tool-calling"]},
            "Muse-Glimmer-GGUF": {"recipe": "llamacpp", "checkpoints": {"main": "org/Muse-Glimmer-GGUF:Q4.gguf"}},
            "Nemotron-3-ONNX": {"recipe": "ryzenai-llm", "checkpoint": "org/Nemotron-3-ONNX"},
        }
        with patch.object(catalog, "_lemonade_catalog_bucket", return_value=data):
            rows = catalog.lemonade_gguf("nemotron tool", set())
        self.assertEqual([row["id"] for row in rows], ["org/Nemotron-3-GGUF"])
        self.assertEqual(rows[0]["source"], "huggingface")
        with patch.object(catalog, "_lemonade_catalog_bucket", return_value=data):
            self.assertEqual(catalog.lemonade_gguf("Muse Glimmer", set())[0]["id"], "org/Muse-Glimmer-GGUF")

    def test_lmstudio_uses_verified_artifact_and_deduplicates(self):
        with patch.object(catalog, "_lm_families_bucket", return_value=[("LFM3 2B Tool use", "/models/lfm3")]), \
             patch.object(catalog, "_lm_family_repos", return_value=[
                 "https://huggingface.co/org/LFM3-GGUF", "https://huggingface.co/org/LFM3-GGUF"]):
            rows = catalog.lmstudio_gguf("lfm3")
        self.assertEqual([row["id"] for row in rows], ["org/LFM3-GGUF"])


if __name__ == "__main__":
    unittest.main()
