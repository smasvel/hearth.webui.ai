import subprocess
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import flm_conversion


class ConversionValidationTests(unittest.TestCase):
    def validate_output(self, values):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.gguf"
            source.write_bytes(b"gguf")
            completed = SimpleNamespace(returncode=0, stdout=json.dumps(values), stderr="")
            with (
                mock.patch.object(flm_conversion, "_source_path", return_value=source),
                mock.patch.object(flm_conversion, "availability", return_value={"available": True}),
                mock.patch.object(subprocess, "run", return_value=completed),
            ):
                return flm_conversion.validate_model(str(source))

    def test_exact_27b_dimensions_are_required(self):
        result = self.validate_output({
            "eligible": True, "profile": "QWEN35_27B", "hidden_size": 5120,
            "intermediate_size": 17408, "layers": 64, "tensors": 851,
            "unmapped": 0, "missing": 0,
        })
        self.assertTrue(result["eligible"])

    def test_misclassified_dimensions_are_rejected(self):
        with self.assertRaises(flm_conversion.ConversionError) as raised:
            self.validate_output({
                "eligible": True, "profile": "QWEN35_27B", "hidden_size": 4096,
                "intermediate_size": 12288, "layers": 32, "tensors": 475,
                "unmapped": 0, "missing": 0,
            })
        self.assertIn("wrong dimensions", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
