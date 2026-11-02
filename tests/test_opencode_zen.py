import unittest
from unittest.mock import Mock, patch

import opencode_client as zen


class _StreamingResponse:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_lines(self, chunk_size=None):
        self.chunk_size = chunk_size
        return iter(self._lines)


class OpenCodeClientTests(unittest.TestCase):
    def setUp(self):
        # Each test must exercise a cold model-list cache.
        zen._models_cache.update(loaded_at=0.0, models=[])

    @patch.object(zen.settings_store, "get", return_value=None)
    @patch.object(zen, "api_key", return_value="test-key")
    @patch.object(zen.requests, "get")
    def test_lists_namespaced_models_with_limits(self, get, _key, _setting):
        response = Mock(status_code=200)
        response.json.return_value = {"data": [
            {"id": "glm-5.3", "name": "GLM 5.3", "limit": {"context": 200000, "output": 32768}},
        ]}
        get.return_value = response

        models = zen.list_models()

        self.assertEqual(models[0]["id"], "opencode/glm-5.3")
        self.assertEqual(models[0]["state"], "loaded")
        self.assertEqual(models[0]["configured_context_length"], 200000)
        self.assertEqual(models[0]["output_tokens_limit"], 32768)
        self.assertTrue(models[0]["externally_managed"])
        self.assertFalse(models[0]["deletable"])
        # Structured evaluator models (Jev) are not chat models and are skipped.
        zen._models_cache.update(loaded_at=0.0, models=[])
        response.json.return_value = {"data": [{"id": "jev-1.13"}]}
        self.assertEqual(zen.list_models(), [])

    @patch.object(zen, "api_key", return_value="")
    @patch.object(zen.requests, "get")
    def test_no_key_lists_nothing_and_never_hits_the_network(self, get, _key):
        self.assertEqual(zen.list_models(), [])
        self.assertFalse(get.called)

    @patch.object(zen.settings_store, "get", return_value=None)
    @patch.object(zen, "api_key", return_value="test-key")
    @patch.object(zen.requests, "get")
    def test_list_failure_raises_with_status(self, get, _key, _setting):
        get.return_value = Mock(status_code=401, text="", json=Mock(
            side_effect=ValueError()))
        with self.assertRaises(zen.OpenCodeError) as ctx:
            zen.list_models()
        self.assertIn("opencode.ai", str(ctx.exception))

    @patch.object(zen.settings_store, "get", return_value=None)
    @patch.object(zen, "api_key", return_value="test-key")
    @patch.object(zen.requests, "post")
    def test_chat_strips_namespace_and_forwards_tools(self, post, _key, _setting):
        post.return_value = _StreamingResponse([b'data: {"choices":[]}', b"data: [DONE]"])
        tools = [{"type": "function", "function": {"name": "search"}}]

        lines = list(zen.chat_stream(
            [{"role": "user", "content": "hello"}],
            "opencode/glm-5.3",
            tools=tools,
        ))

        call = post.call_args
        self.assertIn("/chat/completions", call.args[0])
        self.assertIn("/inference/openai/v1/chat/completions", call.args[0])
        payload = call.kwargs["json"]
        self.assertEqual(payload["model"], "glm-5.3")
        self.assertEqual(payload["tools"], tools)
        self.assertEqual(payload["tool_choice"], "auto")
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(lines[-1], "data: [DONE]")
        self.assertEqual(post.return_value.chunk_size, 1)

    def test_ownership_requires_namespace(self):
        self.assertTrue(zen.owns_model("opencode/glm-5.3"))
        self.assertFalse(zen.owns_model("glm-5.3"))

    @patch.object(zen, "api_key", return_value="")
    def test_verify_requires_a_key_before_anything_else(self, _key):
        with self.assertRaises(zen.OpenCodeError) as ctx:
            zen.verify_model("opencode/glm-5.3")
        self.assertIn("API key", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
