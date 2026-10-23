"""Model attribution survives upstream errors and disconnected clients."""
from contextlib import ExitStack
import unittest
from unittest.mock import patch

import requests
import lemonade_guard_shim as shim


class Upstream:
    status_code = 200
    ok = True
    headers = {'content-type': 'text/event-stream'}

    def iter_lines(self, **kwargs):
        yield b'data: {"choices":[{"delta":{"content":"hello"}}]}'
        if self.fail:
            raise requests.ConnectionError('connection lost')
        yield b'data: [DONE]'

    def close(self):
        pass

    def __init__(self, fail=False):
        self.fail = fail


class FailureMetricsTests(unittest.TestCase):
    def setUp(self):
        stack = self.enterContext(ExitStack())
        for name in ('start', 'update', 'finish'):
            stack.enter_context(patch.object(shim.live_report, name))
        stack.enter_context(patch.object(shim.model_test_profiles, 'apply', side_effect=lambda b, *_: b))
        stack.enter_context(patch.object(shim, '_prepare_agent_request', side_effect=lambda b: b))
        stack.enter_context(patch.object(shim, '_is_npu_model', return_value=False))
        self.unload = stack.enter_context(patch.object(shim, '_unload_before_implicit_load'))
        self.post = stack.enter_context(patch.object(shim, '_post_chat_with_utf8_recovery'))
        self.metric = stack.enter_context(patch.object(shim.live_report, 'record_metric'))
        self.client = shim.app.test_client()

    def send(self):
        return self.client.post('/v1/chat/completions', json={'model': 'test-model', 'stream': True},
                                headers={'X-Hearth-Client': 'opencode'})

    def assert_metric(self, status):
        self.metric.assert_called_once()
        fields = self.metric.call_args.kwargs
        self.assertEqual((fields['model'], fields['status'], fields['ask_category']),
                         ('test-model', status, 'code'))

    def test_upstream_connection_and_timeout_errors(self):
        for error, status in ((requests.ConnectionError(), 502), (requests.Timeout(), 504)):
            with self.subTest(status=status):
                self.metric.reset_mock()
                self.post.side_effect = error
                response = self.send()
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.headers['X-Hearth-Metrics-Recorded'], '1')
                self.assert_metric(status)

    def test_unload_failure_is_attributed(self):
        self.unload.side_effect = RuntimeError('busy')
        response = self.send()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers['X-Hearth-Metrics-Recorded'], '1')
        self.assert_metric(503)
        self.post.assert_not_called()

    def test_disconnect_records_model_once(self):
        self.post.return_value = (Upstream(), 0)
        response = self.send()
        response.close()
        self.assert_metric(499)

    def test_broken_stream_records_model_once(self):
        self.post.return_value = (Upstream(fail=True), 0)
        response = self.send()
        with self.assertRaises(requests.ConnectionError):
            list(response.response)
        self.assert_metric(502)

    def test_success_records_once(self):
        self.post.return_value = (Upstream(), 0)
        response = self.send()
        list(response.response)
        self.assert_metric(200)
