import database
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import metrics_store


class ModelReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = database.temporary_schema()
        self.db.__enter__()
        metrics_store.init_db()

    def tearDown(self):
        self.db.__exit__(None, None, None)
        self.tmp.cleanup()

    def record(self, **fields):
        metrics_store.record(source="opencode", backend="lemonade", model="model-a",
                             request_type="chat_completion", ask_category="code", **fields)

    def test_report_counts_coverage_errors_and_reviewed_accuracy(self):
        self.record(status=200, completion_tokens=100, tokens_per_sec=10, duration_ms=1000)
        self.record(status=400, duration_ms=200)
        self.record(status=500, completion_tokens=0, tokens_per_sec=0)
        self.record(status=None)
        metrics_store.record(source="lan", model="model-a", request_type="models", status=200)
        metrics_store.record(source="opencode", backend="lemonade", model="old",
                             request_type="completion", ts=time.time()-7200, status=200)
        row = metrics_store.summary(1)["by_model"][0]
        self.assertEqual(row["requests"], 4)
        self.assertEqual(row["output_tokens"], 100)
        self.assertEqual(row["token_samples"], 2)
        self.assertEqual(row["avg_tps"], 5)
        self.assertEqual(row["error_rate"], 66.7)
        self.assertEqual(row["unknown_status"], 1)
        self.assertIsNone(row["coding_accuracy"])
        self.assertTrue(metrics_store.review_coding(1, True))
        self.assertTrue(metrics_store.review_coding(2, False))
        row = metrics_store.model_report(1)[0]
        self.assertEqual((row["reviewed"], row["coding_accuracy"]), (2, 50))
        self.assertTrue(metrics_store.review_coding(2, None))
        self.assertEqual(metrics_store.model_report(1)[0]["coding_accuracy"], 100)
        self.assertFalse(metrics_store.review_coding(5, True))
        self.assertEqual(metrics_store.recent(1)["rows"][-1]["coding_passed"], 1)

    def test_unknown_tokens_are_not_reported_as_zero(self):
        self.record(status=None)
        row = metrics_store.model_report(1)[0]
        self.assertIsNone(row["output_tokens"])
        self.assertIsNone(row["error_rate"])
