import importlib
import json
import os
import unittest
from unittest.mock import patch

from hearth.backends import fastflowlm_client as flm


def _state_dir(tmp: str) -> "flm.Path":
    return flm.Path(tmp) / ".config" / "fastflowlm"


class FastFlowLMSharedStateTests(unittest.TestCase):
    """Both Hearth installs write the NPU paused/active state into the same host
    directory. When Docker Hearth creates those files as root they must stay writable
    by the host Hearth install, and vice versa."""

    def _make_state(self, tmp: str):
        import os as os_mod
        if os_mod.geteuid() != 0:
            self.skipTest("needs root to demonstrate the chown path")
        return None


class BrokerFlmPullTests(unittest.TestCase):
    """Docker Hearth has no `flm` binary, so model downloads relay through the broker."""

    def test_download_via_broker_relays_pull_progress(self):
        snapshots = [
            {"running": True, "tag": "qwen3.8-mtp:27b", "percent": 20, "bytes_downloaded": 100, "bytes_total": 500},
            {"running": True, "tag": "qwen3.8-mtp:27b", "percent": 60, "bytes_downloaded": 300, "bytes_total": 500},
            {"running": False, "tag": "qwen3.8-mtp:27b", "percent": 0, "bytes_downloaded": 0, "bytes_total": 0},
        ]

        with patch.object(flm, "_broker_flm", side_effect=lambda args, timeout=900: "") as post, \
             patch.object(flm, "_flm_pull_progress", side_effect=lambda: snapshots.pop(0) if snapshots else {"running": False}), \
             patch.object(flm, "_installed_tags", return_value={"qwen3.8-mtp:27b"}):
            progress = list(flm._download_model_via_broker("qwen3.8-mtp:27b"))

        percents = [item.get("percent") for item in progress]
        self.assertIn(20, percents)
        self.assertIn(60, percents)
        self.assertEqual(percents[-1], 100)

    def test_broker_payload_for_pull_progress_is_round_tripped(self):
        """The broker sends the raw progress dict for `pull-progress` (no
        returncode/stdout wrapper). `_broker_flm` must turn it back into JSON text so
        `_flm_pull_progress()` sees the same data the CLI path produces."""
        progress = {"running": True, "tag": "qwen3:0.6b", "percent": 40, "bytes_downloaded": 40, "bytes_total": 100}
        src = importlib.import_module("hearth.backends.backend_broker_client")

        class FakeResponse:
            status_code = 200
            headers = {}
            def json(self):
                return progress

        # No explicit returncode/stdout fields -> the whole payload round-trips.
        with patch.object(src, "broker_configured", return_value=True), \
             patch.object(src, "broker_src_url", return_value="http://127.0.0.1:8765"), \
             patch.object(src, "broker_token", return_value="t"), \
             patch.object(flm.requests, "post", return_value=FakeResponse()):
            content = flm._broker_flm(["pull-progress"])
        self.assertEqual(json.loads(content), progress)


if __name__ == '__main__':
    unittest.main()
