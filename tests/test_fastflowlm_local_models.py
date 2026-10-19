import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fastflowlm_client as flm
import flm_backend_profile as profile
from test_flm_backend_profile import write_ready_fixture


class LocalModelRegistryTests(unittest.TestCase):
    def test_manifest_discovery_does_not_depend_on_directory_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conversion = root / "arbitrary-local-name"
            conversion.mkdir()
            (conversion / "model.q4nx").write_bytes(b"not-a-real-header")
            (conversion / "conversion.json").write_text(json.dumps({
                "schema": 1,
                "profile": profile.CONVERTER_PROFILE,
                "runtime_tag": "legacy-wrong-tag",
                "runtime_ready": False,
            }))
            with (
                mock.patch.object(flm, "_CONVERSION_DIR", root),
                mock.patch.object(flm, "_XCLBIN_ROOT", root / "xclbins"),
                mock.patch.object(flm, "_installed", return_value=[]),
                mock.patch.object(flm, "_runtime_version", return_value="0.9.45"),
                mock.patch.object(flm, "is_active", return_value=False),
            ):
                local = [model for model in flm.list_models() if model.get("converted")]
            self.assertEqual([model["id"] for model in local], ["flm/local:arbitrary-local-name"])
            self.assertFalse(local[0]["loadable"])

    def test_ready_local_id_resolves_to_qwen35_runtime_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conversion, xclbins = write_ready_fixture(root)
            installed = [{"model": profile.RUNTIME_TAG, "footprint": 22.0}]
            with (
                mock.patch.object(flm, "_CONVERSION_DIR", root),
                mock.patch.object(flm, "_XCLBIN_ROOT", xclbins),
                mock.patch.object(flm, "_installed", return_value=installed),
                mock.patch.object(flm, "_runtime_version", return_value="1.0.2"),
            ):
                self.assertEqual(
                    flm.resolve_model(f"flm/local:{conversion.name}"),
                    profile.RUNTIME_TAG,
                )

    def test_blocked_local_preflight_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conversion = root / "blocked"
            conversion.mkdir()
            (conversion / "conversion.json").write_text(json.dumps({
                "schema": 1,
                "profile": profile.CONVERTER_PROFILE,
                "runtime_ready": False,
            }))
            with (
                mock.patch.object(flm, "_CONVERSION_DIR", root),
                mock.patch.object(flm, "_XCLBIN_ROOT", root / "xclbins"),
                mock.patch.object(flm, "_installed", return_value=[]),
                mock.patch.object(flm, "_runtime_version", return_value="0.9.45"),
            ):
                with self.assertRaises(flm.FastFlowLMError) as raised:
                    flm.check_loadable("flm/local:blocked")
            self.assertIn("Not loadable yet", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
