"""Chat backends must not let Requests buffer small SSE token events."""
import unittest
from unittest.mock import MagicMock, Mock, patch

import lemonade_client
import lmstudio_client
import kobold_client
import q38rocm_client


class ChatStreamChunkingTests(unittest.TestCase):
    def _assert_streams_one_byte_at_a_time(self, client, model):
        response = Mock(status_code=200)
        response.iter_lines.return_value = [b"data: [DONE]"]
        post_response = MagicMock()
        post_response.__enter__.return_value = response

        with patch.object(client.settings_store, "get", return_value=None), \
                patch.object(client.requests, "post", return_value=post_response), \
                patch.object(client.model_test_profiles, "apply", side_effect=lambda body, *_: body):
            self.assertEqual(list(client.chat_stream([], model)), ["data: [DONE]"])

        response.iter_lines.assert_called_once_with(chunk_size=1)

    def test_lm_studio_chat_disables_requests_chunk_buffering(self):
        self._assert_streams_one_byte_at_a_time(lmstudio_client, "test-model")

    def test_lemonade_chat_disables_requests_chunk_buffering(self):
        self._assert_streams_one_byte_at_a_time(lemonade_client, "test-model")

    def test_q38rocm_chat_requests_usage_for_metrics(self):
        response = Mock(status_code=200)
        response.iter_lines.return_value = [b"data: [DONE]"]
        post_response = MagicMock()
        post_response.__enter__.return_value = response
        with patch.object(q38rocm_client, "request_model_id", return_value="Qwen-test"), \
                patch.object(q38rocm_client.settings_store, "get", return_value=None), \
                patch.object(q38rocm_client, "_headers", return_value={}), \
                patch.object(q38rocm_client.requests, "post", return_value=post_response) as post:
            self.assertEqual(list(q38rocm_client.chat_stream([], "q38rocm/test")), ["data: [DONE]"])
        self.assertEqual(post.call_args.kwargs["json"]["stream_options"], {"include_usage": True})
        self.assertNotIn("frequency_penalty", post.call_args.kwargs["json"])
        response.iter_lines.assert_called_once_with(chunk_size=1)

    def test_kobold_chat_requests_stream_usage_for_metrics(self):
        response = Mock(status_code=200)
        response.iter_lines.return_value = [b"data: [DONE]"]
        post_response = MagicMock()
        post_response.__enter__.return_value = response
        with patch.object(kobold_client, "base_url", return_value="http://127.0.0.1:5001"), \
                patch.object(kobold_client, "_auth_headers", return_value={}), \
                patch.object(kobold_client.model_test_profiles, "apply", side_effect=lambda body, *_: body), \
                patch.object(kobold_client, "performance_snapshot", return_value=None), \
                patch("kobold_tool_shim.start_server", return_value="http://127.0.0.1:13308"), \
                patch.object(kobold_client.requests, "post", return_value=post_response) as post:
            self.assertEqual(list(kobold_client.chat_stream([], "koboldcpp/test")), ["data: [DONE]"])
        self.assertEqual(post.call_args.kwargs["json"]["stream_options"], {"include_usage": True})
        response.iter_lines.assert_called_once_with(chunk_size=1)


if __name__ == "__main__":
    unittest.main()
