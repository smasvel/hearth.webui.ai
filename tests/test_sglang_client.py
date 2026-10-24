import unittest
from unittest.mock import Mock, patch

import sglang_client as sglang


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


class SGLangClientTests(unittest.TestCase):
    @patch("sglang_client.settings_store.get", return_value=None)
    @patch("sglang_client.requests.get")
    def test_lists_namespaced_externally_managed_models(self, get, _setting):
        response = Mock(status_code=200)
        response.json.return_value = {"data": [{"id": "zai-org/GLM-5.2"}]}
        get.return_value = response

        models = sglang.list_models()

        self.assertEqual(models[0]["id"], "sglang/zai-org/GLM-5.2")
        self.assertEqual(models[0]["state"], "loaded")
        self.assertTrue(models[0]["externally_managed"])
        self.assertFalse(models[0]["deletable"])

    @patch("sglang_client.settings_store.get", return_value=None)
    @patch("sglang_client.requests.post")
    def test_chat_strips_namespace_and_forwards_tools(self, post, _setting):
        post.return_value = _StreamingResponse([b'data: {"choices":[]}', b"data: [DONE]"])
        tools = [{"type": "function", "function": {"name": "search"}}]

        lines = list(sglang.chat_stream(
            [{"role": "user", "content": "hello"}],
            "sglang/zai-org/GLM-5.2",
            tools=tools,
        ))

        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "zai-org/GLM-5.2")
        self.assertEqual(payload["tools"], tools)
        self.assertEqual(payload["tool_choice"], "auto")
        self.assertEqual(lines[-1], "data: [DONE]")
        self.assertEqual(post.return_value.chunk_size, 1)

    def test_ownership_requires_namespace(self):
        self.assertTrue(sglang.owns_model("sglang/zai-org/GLM-5.2"))
        self.assertFalse(sglang.owns_model("zai-org/GLM-5.2"))


if __name__ == "__main__":
    unittest.main()
