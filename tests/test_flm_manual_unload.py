import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fastflowlm_client as flm
import flm_tool_shim as shim


class ManualUnloadTests(unittest.TestCase):
    def test_manual_unload_blocks_retries_until_explicit_load(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(shim.npu_request_lock, '_LOCK_PATH', root / 'npu.lock'),
                patch.object(flm, '_STATE_DIR', root),
                patch.object(flm, '_PAUSED_FILE', root / 'paused'),
                patch.object(flm, '_systemctl'),
                patch.object(flm, '_write_active'),
                patch.object(flm, 'ensure_service'),
                patch.object(flm, '_installed', return_value=[{'model': 'test'}]),
            ):
                flm.unload_model('flm/test')
                self.assertTrue(flm.requests_paused())
                with patch.object(shim.npu_request_lock, 'acquire') as acquire:
                    response = shim.app.test_client().post('/v1/chat/completions', json={'model': 'test'})
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.json['error']['code'], 'model_manually_unloaded')
                    acquire.assert_not_called()
                flm.load_model('flm/test')
                self.assertFalse(flm.requests_paused())

    def test_internal_handoff_does_not_pause_requests(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(flm, '_PAUSED_FILE', Path(directory) / 'paused'),
            patch.object(flm, '_systemctl'),
            patch.object(flm, '_write_active'),
        ):
            flm.unload_model(npu_lock_held=True)
            self.assertFalse(flm.requests_paused())
