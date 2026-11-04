import unittest
from unittest.mock import patch

import lemonade_client
import lmstudio_client
import kobold_client


class _Response:
    status_code = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_lines(self, chunk_size=1):
        return iter([b'data: [DONE]'])

    def raise_for_status(self):
        pass


class ChatMCPRoutingTests(unittest.TestCase):
    def _assert_tool_route(self, client, expected_url, **kwargs):
        tools = [{'type': 'function', 'function': {'name': 'playwright__navigate',
                  'parameters': {'type': 'object', 'properties': {'url': {'type': 'string'}}}}}]
        with patch.object(client.requests, 'post', return_value=_Response()) as post, \
             patch.object(client.settings_store, 'get', return_value=None), \
             patch.object(client.model_test_profiles, 'apply', side_effect=lambda payload, *_: payload), \
             patch.object(client, '_auth_headers', return_value={}):
            list(client.chat_stream([], 'test-model', tools=tools,
                                    parallel_tool_calls=True, **kwargs))
        self.assertEqual(post.call_args.args[0], expected_url)
        self.assertEqual(post.call_args.kwargs['json']['tools'], tools)
        self.assertTrue(post.call_args.kwargs['json']['parallel_tool_calls'])

    def test_lemonade_tool_chat_uses_guard_shim(self):
        self._assert_tool_route(lemonade_client,
            'http://127.0.0.1:13306/v1/chat/completions', enable_thinking=False)

    def test_lmstudio_tool_chat_uses_compatibility_shim(self):
        self._assert_tool_route(lmstudio_client,
            'http://127.0.0.1:13307/v1/chat/completions')

    def test_kobold_tool_chat_forwards_parallel_call_support(self):
        with patch('kobold_tool_shim.start_server', return_value='http://127.0.0.1:13308') as start:
            self._assert_tool_route(kobold_client,
                'http://127.0.0.1:13308/v1/chat/completions')
        start.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
