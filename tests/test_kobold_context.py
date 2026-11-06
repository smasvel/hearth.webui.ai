"""KoboldCPP uses each GGUF's native context limit on later loads."""
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import kobold_client as kobold


class KoboldContextTests(unittest.TestCase):
    def test_reads_native_gguf_context(self):
        key = b"qwen35.context_length"
        header = b"GGUF" + b"\x00" * 20 + struct.pack("<Q", len(key)) + key
        header += struct.pack("<I", 4) + struct.pack("<I", 262144)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.gguf"
            path.write_bytes(header)
            self.assertEqual(kobold._gguf_max_context(str(path)), 262144)
            path.write_bytes(b"invalid" + header)
            self.assertIsNone(kobold._gguf_max_context(str(path)))

    def test_next_load_uses_saved_context(self):
        model_id = "koboldcpp/example"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.gguf"
            path.write_bytes(b"GGUF")
            with patch.object(kobold, "_load_registry", return_value={model_id: {"path": str(path)}}), \
                 patch.object(kobold.model_metadata_store, "get_model_configured_context", return_value=262144), \
                 patch.object(kobold, "_write_service_env") as write, \
                 patch.object(kobold, "_systemctl"), \
                 patch.object(kobold, "current_served_model", return_value=model_id):
                kobold.load_model(model_id)
        self.assertEqual(write.call_args.kwargs["context"], 262144)


if __name__ == "__main__":
    unittest.main()
