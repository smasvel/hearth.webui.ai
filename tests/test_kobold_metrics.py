import json
import unittest
from unittest.mock import MagicMock, Mock, patch

from hearth.backends import kobold_client
from tools import test_flm_model as model_runner
from tools.test_flm_model import _metric_tokens_per_second


class KoboldMetricsTests(unittest.TestCase):
    def test_model_test_metrics_prefer_backend_decode_speed(self):
        self.assertEqual(_metric_tokens_per_second(
            {"completion_tokens": 12, "decoding_speed_tps": 24.68}, 4.0), 24.7)

    def test_model_test_metrics_fall_back_to_end_to_end_rate(self):
        self.assertEqual(_metric_tokens_per_second({"completion_tokens": 12}, 4.0), 3.0)

    def test_perf_fills_counts_and_decode_speed_for_one_completed_request(self):
        before = {"total_gens": 5}
        after = {
            "total_gens": 6,
            "last_input_count": 31,
            "last_token_count": 12,
            "last_eval_speed": 24.68,
        }
        self.assertEqual(kobold_client.merge_performance_usage({}, before, after), {
            "prompt_tokens": 31,
            "completion_tokens": 12,
            "total_tokens": 43,
            "decoding_speed_tps": 24.7,
        })

    def test_perf_counters_are_ignored_when_request_attribution_is_ambiguous(self):
        before = {"total_gens": 5}
        after = {"total_gens": 7, "last_input_count": 31,
                 "last_token_count": 12, "last_eval_speed": 24.68}
        self.assertEqual(kobold_client.merge_performance_usage({}, before, after), {})

    def test_perf_never_overwrites_usage_reported_by_the_stream(self):
        before = {"total_gens": 5}
        after = {"total_gens": 6, "last_input_count": 31,
                 "last_token_count": 12, "last_eval_speed": 24.68}
        exact = {"prompt_tokens": 31, "completion_tokens": 12, "total_tokens": 43}
        result = kobold_client.merge_performance_usage(exact, before, after)
        self.assertEqual((result["prompt_tokens"], result["completion_tokens"], result["total_tokens"]),
                         (31, 12, 43))
        self.assertEqual(result["decoding_speed_tps"], 24.7)

    def test_perf_speed_is_rejected_when_its_counts_disagree_with_the_stream(self):
        before = {"total_gens": 5}
        after = {"total_gens": 6, "last_input_count": 31,
                 "last_token_count": 99, "last_eval_speed": 24.68}
        exact = {"prompt_tokens": 30, "completion_tokens": 11, "total_tokens": 41}
        self.assertEqual(kobold_client.merge_performance_usage(exact, before, after), exact)

    def test_chat_stream_adds_correlated_perf_to_terminal_usage_event(self):
        response = Mock(status_code=200)
        response.iter_lines.return_value = [
            b'data: {"choices":[{"delta":{"reasoning_content":"Kobold thought"}}]}',
            b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":null}]}',
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            b'data: [DONE]',
        ]
        post_response = MagicMock()
        post_response.__enter__.return_value = response
        snapshots = [
            {"total_gens": 8},
            {"total_gens": 9, "last_input_count": 5, "last_token_count": 2,
             "last_eval_speed": 10.0},
        ]
        with patch.object(kobold_client, "base_url", return_value="http://127.0.0.1:5001"), \
                patch.object(kobold_client, "_auth_headers", return_value={}), \
                patch.object(kobold_client.model_test_profiles, "apply", side_effect=lambda body, *_: body), \
                patch.object(kobold_client, "performance_snapshot", side_effect=snapshots), \
                patch.object(kobold_client.requests, "post", return_value=post_response):
            lines = list(kobold_client.chat_stream([], "koboldcpp/test"))
        events = [json.loads(line[5:].strip()) for line in lines
                  if line.startswith("data:") and line != "data: [DONE]"]
        self.assertEqual(events[-1]["usage"], {
            "prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7,
            "decoding_speed_tps": 10.0,
        })
        self.assertEqual(events[0]["choices"][0]["delta"]["reasoning_content"], "Kobold thought")
        self.assertEqual(lines[-1], "data: [DONE]")

    def test_context_overflow_test_records_its_http_metric(self):
        response = Mock(status_code=400)
        with patch.object(model_runner.requests, "post", return_value=response), \
                patch.object(model_runner.kobold_client, "performance_snapshot", return_value=None), \
                patch.object(model_runner.live_report, "start", return_value=42), \
                patch.object(model_runner.live_report, "finish") as finish, \
                patch.object(model_runner.live_report, "record_metric") as record_metric:
            results = model_runner.run_suite(
                "koboldcpp/test", "http://127.0.0.1:5001/v1/chat/completions",
                suite="extra", only={("context-overflow", False)}, backend="kobold",
            )
        self.assertTrue(results[0]["passed"])
        finish.assert_called_once()
        self.assertEqual(record_metric.call_args.kwargs["status"], 400)
        self.assertEqual(record_metric.call_args.kwargs["request_type"], "model-test")


if __name__ == "__main__":
    unittest.main()
