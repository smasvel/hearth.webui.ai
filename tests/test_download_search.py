"""Regression coverage for the Downloads page's backend search contract."""
import unittest
from unittest.mock import patch

import app


class DownloadSearchTests(unittest.TestCase):
    def test_empty_combined_search_uses_only_compatible_backends(self):
        def result(backend, query, limit):
            self.assertEqual(query, "")
            return [{"backend": backend, "id": f"{backend}/model-{n}", "downloads": limit - n}
                    for n in range(limit)]

        with app.app.test_request_context("/api/models/search?backend=all&q="):
            with patch.object(app, "_search_models_for_backend", side_effect=result) as search:
                response = app.api_search_models.__wrapped__()
        data = response.get_json()
        self.assertEqual({call.args[0] for call in search.call_args_list},
                         {"lemonade", "lmstudio", "kobold", "vllm"})
        self.assertEqual({call.args[2] for call in search.call_args_list}, {101})
        self.assertEqual(len(data["results"]), 100)
        self.assertEqual(data["total"], 404)

    def test_later_page_fetches_a_larger_backend_prefix(self):
        def result(backend, query, limit):
            return [{"backend": backend, "id": f"{backend}/model-{n}", "downloads": limit - n}
                    for n in range(limit)]

        with app.app.test_request_context(
                "/api/models/search?backend=all&q=large&max_results=60&offset=120"):
            with patch.object(app, "_search_models_for_backend", side_effect=result) as search:
                response = app.api_search_models.__wrapped__()
        data = response.get_json()
        self.assertEqual(len(data["results"]), 60)
        self.assertEqual({call.args[2] for call in search.call_args_list}, {181})
        self.assertEqual({call.args[1] for call in search.call_args_list}, {"large"})

    def test_gguf_backends_request_only_gguf_from_hugging_face(self):
        model = {"id": "publisher/model-GGUF", "source": "huggingface"}
        with patch.object(app.lemonade, "search_huggingface", return_value=[model]) as search, \
             patch.object(app.lemonade, "fill_result_sizes"):
            app._search_models_for_backend("lmstudio", "model")
            app._search_models_for_backend("kobold", "model")
        self.assertEqual(search.call_count, 2)
        for call in search.call_args_list:
            self.assertEqual(call.kwargs["strict_format"], "gguf")

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
