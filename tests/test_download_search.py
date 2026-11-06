"""Regression coverage for the Downloads page's backend search contract."""
import unittest
from unittest.mock import patch

import app


class DownloadSearchTests(unittest.TestCase):
    def test_empty_combined_search_uses_only_compatible_backends(self):
        def result(backend, query, limit, include_details=True):
            self.assertEqual(query, "")
            return [{"backend": backend, "id": f"{backend}/model-{n}", "downloads": limit - n}
                    for n in range(limit)]

        with app.app.test_request_context("/api/models/search?backend=all&q="):
            with patch.object(app, "_search_models_for_backend", side_effect=result) as search, \
                 patch.object(app.lemonade, "search_huggingface", return_value=[
                     {"id": f"gguf/model-{n}", "source": "huggingface", "downloads": 500 - n}
                     for n in range(500)
                 ]) as gguf_search:
                response = app.api_search_models.__wrapped__()
        data = response.get_json()
        self.assertEqual({call.args[0] for call in search.call_args_list},
                         {"lemonade", "vllm"})
        self.assertEqual({call.args[2] for call in search.call_args_list}, {500})
        self.assertEqual({call.args[3] for call in search.call_args_list}, {False})
        gguf_search.assert_called_once()
        self.assertEqual(len(data["results"]), 100)
        self.assertEqual(data["total"], 2000)

    def test_later_page_fetches_a_larger_backend_prefix(self):
        def result(backend, query, limit, include_details=True):
            return [{"backend": backend, "id": f"{backend}/model-{n}", "downloads": limit - n}
                    for n in range(limit)]

        with app.app.test_request_context(
                "/api/models/search?backend=all&q=large&max_results=60&offset=120"):
            with patch.object(app, "_search_models_for_backend", side_effect=result) as search, \
                 patch.object(app.lemonade, "search_huggingface", return_value=[
                     {"id": f"gguf/model-{n}", "source": "huggingface", "downloads": 500 - n}
                     for n in range(500)
                 ]):
                response = app.api_search_models.__wrapped__()
        data = response.get_json()
        self.assertEqual(len(data["results"]), 60)
        self.assertEqual({call.args[2] for call in search.call_args_list}, {500})
        self.assertEqual({call.args[1] for call in search.call_args_list}, {"large"})

    def test_broad_search_enriches_only_the_returned_page(self):
        def result(backend, query, limit, include_details=True):
            return [{"backend": backend, "id": f"{backend}/model-{n}", "downloads": limit - n}
                    for n in range(limit)]

        with app.app.test_request_context("/api/models/search?backend=all&q=large&max_results=3"):
            with patch.object(app, "_search_models_for_backend", side_effect=result), \
                 patch.object(app, "_enrich_search_page") as fill_sizes:
                response = app.api_search_models.__wrapped__()
        self.assertEqual(len(response.get_json()["results"]), 3)
        fill_sizes.assert_called_once()
        self.assertEqual(len(fill_sizes.call_args.args[0]), 3)

    def test_gguf_search_failure_is_reported_for_both_compatible_backends(self):
        with app.app.test_request_context("/api/models/search?backend=all&q="):
            with patch.object(app, "_search_models_for_backend", return_value=[]), \
                 patch.object(app.lemonade, "search_huggingface", side_effect=RuntimeError("temporary HF failure")):
                response = app.api_search_models.__wrapped__()
        self.assertEqual(response.get_json()["backend_errors"], ["lmstudio", "kobold"])

    def test_gguf_backends_request_only_gguf_from_hugging_face(self):
        model = {"id": "publisher/model-GGUF", "source": "huggingface"}
        with patch.object(app.lemonade, "search_huggingface", return_value=[model]) as search, \
             patch.object(app.lemonade, "fill_result_sizes"):
            app._search_models_for_backend("lmstudio", "model")
            app._search_models_for_backend("kobold", "model")
        self.assertEqual(search.call_count, 2)
        for call in search.call_args_list:
            self.assertEqual(call.kwargs["strict_format"], "gguf")

    def test_huggingface_results_are_shared_between_repeated_backend_searches(self):
        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return [{"id": "publisher/cache-test-GGUF", "downloads": 10}]

        with patch.object(app.lemonade.requests, "get", return_value=Response()) as get:
            first = app.lemonade.search_huggingface(
                "unique cache test", limit=1, strict_format="gguf", include_descriptions=False
            )
            second = app.lemonade.search_huggingface(
                "unique cache test", limit=1, strict_format="gguf", include_descriptions=False
            )
        self.assertEqual(first[0]["id"], second[0]["id"])
        get.assert_called_once()

    def test_gguf_search_falls_back_when_huggingface_api_is_rate_limited(self):
        fallback = [{"id": "publisher/fallback-GGUF", "downloads": 3}]
        with patch.object(app.lemonade.requests, "get", side_effect=app.lemonade.requests.HTTPError("429")), \
             patch.object(app.lemonade, "_search_hf_gguf_html", return_value=fallback) as html_search:
            results = app.lemonade.search_huggingface(
                "unique fallback test", limit=10, strict_format="gguf", include_descriptions=False
            )
        self.assertEqual(results[0]["id"], fallback[0]["id"])
        html_search.assert_called_once_with("unique fallback test", 10)

    def test_lemonade_search_combines_native_catalog_and_gguf(self):
        catalog = {"id": "publisher/native-GGUF", "source": "local"}
        hf_model = {"id": "publisher/other-GGUF", "source": "huggingface"}
        with patch.object(app.lemonade, "search_catalog", return_value=[catalog]) as native, \
             patch.object(app.lemonade, "enrich_with_hf_metadata", side_effect=lambda items: items), \
             patch.object(app.lemonade, "search_huggingface", return_value=[hf_model]) as hf, \
             patch.object(app.lemonade, "fill_result_sizes"):
            results = app._search_models_for_backend("lemonade", "model")
        self.assertEqual({r["id"] for r in results}, {catalog["id"], hf_model["id"]})
        native.assert_called_once()
        self.assertEqual(hf.call_args.kwargs["strict_format"], "gguf")


if __name__ == "__main__":
    unittest.main()
