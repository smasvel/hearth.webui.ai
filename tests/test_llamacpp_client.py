"""Direct llama-server inventory and chat contract."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from hearth.backends import llamacpp_client


class LlamaCppClientTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"LLAMACPP_BASE_URL": "http://127.0.0.1:8093/v1"})
        env.start()
        self.addCleanup(env.stop)
        settings = patch.object(llamacpp_client.settings_store, "get", return_value=None)
        settings.start()
        self.addCleanup(settings.stop)

    def test_catalog_preserves_server_context_and_lists_router_models(self):
        response = Mock()
        response.json.return_value = {"data": [
            {"id": "local-test", "meta": {"n_ctx": 8192, "n_ctx_train": 32768}},
            {"id": "cold-test", "status": {"value": "unloaded", "args": ["--ctx-size", "4096"]},
             "can_remove": True},
        ]}
        with patch.object(llamacpp_client.requests, "get", return_value=response) as get:
            models = llamacpp_client.list_models()
        self.assertEqual(get.call_args.args[0], "http://127.0.0.1:8093/v1/models")
        self.assertEqual([model["id"] for model in models], ["llamacpp/local-test", "llamacpp/cold-test"])
        self.assertEqual(models[0]["loaded_context_length"], 8192)
        self.assertEqual(models[0]["max_context_length"], 32768)
        self.assertTrue(models[0]["externally_managed"])
        self.assertEqual(models[1]["state"], "not-loaded")
        self.assertEqual(models[1]["configured_context_length"], 4096)
        self.assertTrue(models[1]["deletable"])

    def test_router_download_reports_bytes_and_finishes_when_cached(self):
        with (
            patch.object(llamacpp_client, "_router_request") as request,
            patch.object(llamacpp_client, "_router_model", side_effect=[
                {"status": {"value": "downloading", "progress": {"file": {"done": 30, "total": 50}}}},
                {"status": {"value": "unloaded"}},
            ]),
            patch.object(llamacpp_client.time, "sleep"),
        ):
            progress = list(llamacpp_client.download_model("test/model:Q4_K_M"))
        request.assert_called_once_with("POST", "/models", json={"model": "test/model:Q4_K_M"}, timeout=30)
        self.assertEqual(progress[0]["bytes_downloaded"], 30)
        self.assertEqual(progress[0]["bytes_total"], 50)
        self.assertEqual(progress[0]["percent"], 60)

    def test_active_local_download_reports_cache_bytes_without_router_progress(self):
        model = "test/model:Q4_K_M"
        with TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache"
            cache.mkdir()
            (cache / "weights.downloadInProgress").write_bytes(b"weights")
            (cache / "projector").write_bytes(b"projector")
            (root / "outside.downloadInProgress").write_bytes(b"outside")
            (cache / "unfinished-downloads.json").write_text(json.dumps({"downloads": [
                {"model": model, "partials": [
                    {"path": "/data/cache/weights.downloadInProgress"},
                    {"path": "/data/cache/projector.downloadInProgress"},
                    {"path": "/data/cache/../outside.downloadInProgress"},
                ]},
            ]}))
            with (
                patch.dict(os.environ, {
                    "LLAMACPP_BASE_URL": "",
                    "HEARTH_LLAMACPP_MANAGED_URL": "http://127.0.0.1:8093",
                    "HEARTH_LLAMACPP_CONTAINER": "fixture-router",
                    "HEARTH_LLAMACPP_PRESETS": str(root / "presets.ini"),
                }),
                patch.object(llamacpp_client.settings_store, "get", return_value="http://127.0.0.1:8093"),
                patch.object(llamacpp_client, "_router_request", return_value={"data": [
                    {"id": model, "status": {"value": "downloading"}},
                ]}),
            ):
                progress = llamacpp_client.active_downloads()[0]["progress"]
        self.assertEqual(progress["bytes_downloaded"], 16)
        self.assertIsNone(progress["percent"])

    def test_unconfigured_endpoint_does_not_probe_other_services(self):
        with patch.dict(os.environ, {"LLAMACPP_BASE_URL": ""}), \
             patch.object(llamacpp_client.requests, "get") as get:
            self.assertEqual(llamacpp_client.list_models(), [])
        get.assert_not_called()

    def test_chat_stream_sends_server_alias_and_reports_upstream_errors(self):
        response = Mock(status_code=200)
        response.iter_lines.return_value = [b"data: fixture", b"", b"data: [DONE]"]
        post_response = Mock()
        post_response.__enter__ = Mock(return_value=response)
        post_response.__exit__ = Mock(return_value=False)
        messages = [{"role": "user", "content": "Say hello"}]
        with patch.object(llamacpp_client.requests, "post", return_value=post_response) as post:
            self.assertEqual(list(llamacpp_client.chat_stream(messages, "llamacpp/local-test", max_tokens=64)),
                             ["data: fixture", "data: [DONE]"])
            self.assertEqual(post.call_args.args[0], "http://127.0.0.1:8093/v1/chat/completions")
            self.assertEqual(post.call_args.kwargs["json"]["model"], "local-test")
            response.status_code = 400
            response.json.return_value = {"error": {"message": "Context is full"}}
            with self.assertRaisesRegex(llamacpp_client.LlamaCppError, "Context is full"):
                list(llamacpp_client.chat_stream(messages, "llamacpp/local-test"))
