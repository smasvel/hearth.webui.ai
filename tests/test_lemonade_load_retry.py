"""Regression tests for load_model's bounded retry of transient load failures.

A cold load of a large MoE GGUF (or Lemonade mid-evicting a previous model) returns a
transient 500 from /v1/load even when the model is otherwise fine and the next attempt
succeeds (confirmed live: first load 500, immediate retry "success"). load_model must
therefore retry 5xx and connection errors a bounded number of times, while failing fast on
non-5xx responses (an unknown model key / RAM-refusal / bad payload fails identically every
attempt and must not burn the whole backoff budget).
"""
import unittest
from unittest.mock import patch

import requests

import lemonade_client


class LoadModelRetryTests(unittest.TestCase):
    def setUp(self):
        self.mocks = [
            patch.object(lemonade_client, "base_url", return_value="http://localhost:13305"),
            patch.object(lemonade_client, "_auth_headers", return_value={}),
            # Jitter to zero so the backoff math is deterministic and no real sleep happens.
            patch.object(lemonade_client.random, "uniform", return_value=0.0),
            patch.object(lemonade_client.time, "sleep"),
            patch.object(lemonade_client, "is_model_loaded", return_value=True),
        ]
        for m in self.mocks:
            m.start()
        self.addCleanup(lambda: [m.stop() for m in reversed(self.mocks)])

    def _response(self, status_code: int):
        import types
        resp = types.SimpleNamespace(
            status_code=status_code,
            url="http://localhost:13305/v1/load",
        )

        def raise_for_status():
            if status_code >= 400:
                raise requests.HTTPError(f"HTTP {status_code}")

        resp.raise_for_status = raise_for_status
        return resp

    def test_transient_5xx_then_success_is_retried(self):
        with patch.object(lemonade_client.requests, "post",
                          side_effect=[self._response(500), self._response(200)]) as post:
            lemonade_client.load_model("K2-Horizon-MoVA-36B-A4B-GGUF")
            self.assertEqual(post.call_count, 2)

    def test_non_5xx_fails_fast_without_retrying(self):
        with patch.object(lemonade_client.requests, "post",
                          side_effect=[self._response(404)]) as post:
            with self.assertRaises(lemonade_client.LemonadeError):
                lemonade_client.load_model("does-not-exist-GGUF")
            self.assertEqual(post.call_count, 1)

    def test_persistent_5xx_raises_after_bounded_attempts(self):
        with patch.object(lemonade_client.requests, "post",
                          side_effect=[self._response(500)] * lemonade_client._LOAD_RETRIES) as post:
            with self.assertRaises(lemonade_client.LemonadeError):
                lemonade_client.load_model("K2-Horizon-MoVA-36B-A4B-GGUF")
            self.assertEqual(post.call_count, lemonade_client._LOAD_RETRIES)

    def test_connection_error_then_success_is_retried(self):
        with patch.object(lemonade_client.requests, "post",
                          side_effect=[requests.ConnectionError("connection reset"),
                                       self._response(200)]) as post:
            lemonade_client.load_model("K2-Horizon-MoVA-36B-A4B-GGUF")
            self.assertEqual(post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
