"""FLM requests must appear in Live logs even before the NPU becomes available."""
import unittest
from unittest.mock import patch

from hearth.backends import flm_tool_shim, lemonade_guard_shim
from hearth.core import live_requests


class FLMLiveLogTests(unittest.TestCase):
    def setUp(self):
        for name, value in (('_active', {}), ('_cancel_events', {}), ('_recent', [])):
            self.enterContext(patch.object(live_requests, name, value))
        self.enterContext(patch.object(live_requests, '_persist_started'))
        self.enterContext(patch.object(live_requests, '_persist_finished'))

    def check_failed_handoff(self, shim, lock, backend, model, reported_model):
        self.enterContext(patch.object(shim.live_report, 'start', live_requests.start))
        self.enterContext(patch.object(shim.live_report, 'finish', live_requests.finish))
        waiting = []
        lock.acquire.side_effect = lambda: waiting.extend(live_requests.snapshot())
        response = shim.app.test_client().post('/v1/chat/completions', json={
            'model': model, 'messages': [{'role': 'user', 'content': 'Hello'}],
        }, headers={'X-Hearth-Client': 'opencode'})

        self.assertEqual(response.status_code, 503)
        self.assertEqual(len(waiting), 1, 'Expected one visible row before acquiring the NPU')
        row = waiting[0]
        self.assertEqual((row['backend'], row['model'], row['source'], row['status']),
                         (backend, reported_model, 'opencode', 'streaming'))
        self.assertTrue(row['is_npu'])
        finished, = live_requests.snapshot()
        self.assertEqual((finished['id'], finished['status'], finished['status_code']),
                         (row['id'], 'error', 503))

    def test_standalone_flm_wait_then_startup_failure(self):
        shim = flm_tool_shim
        with (
            patch.object(shim.fastflowlm, 'requests_paused', return_value=False),
            patch.object(shim.flm_context, 'prepare', side_effect=lambda body: (body, None)),
            patch.object(shim, 'npu_request_lock') as lock,
            patch.object(shim, '_free_lemonade_npu'),
            patch.object(shim, '_ensure_fastflow_ready', side_effect=shim.fastflowlm.FastFlowLMError('startup failed')),
        ):
            self.check_failed_handoff(shim, lock, 'fastflowlm', 'flm/test', 'test')

    def test_lemonade_flm_wait_then_unload_failure(self):
        shim = lemonade_guard_shim
        with (
            patch.object(shim, '_is_npu_model', return_value=True),
            patch.object(shim, '_NPU_REQUEST_LOCK') as lock,
            patch.object(shim, 'npu_request_lock'),
            patch.object(shim, '_free_fastflowlm_npu'),
            patch.object(shim, '_unload_before_implicit_load', side_effect=RuntimeError('unload failed')),
            patch.object(shim, '_record_chat_metric'),
        ):
            self.check_failed_handoff(shim, lock, 'lemonade', 'test-FLM', 'test-FLM')
