"""Chat backends must not let Requests buffer small SSE token events."""
import unittest
from unittest.mock import MagicMock, Mock, patch

import lemonade_client
import lmstudio_client


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


if __name__ == "__main__":
    unittest.main()
