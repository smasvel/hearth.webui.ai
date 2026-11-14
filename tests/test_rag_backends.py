"""Backend-aware cross-chat RAG must keep embedding spaces separate."""
import unittest
from hearth.storage import database
from unittest.mock import Mock, patch

import app
from hearth.backends import kobold_client
from hearth.storage import memory_store


class RagBackendRoutingTests(unittest.TestCase):
    def setUp(self):
        self.database_schema = database.temporary_schema()
        self.database_schema.__enter__()
        self.addCleanup(self.database_schema.__exit__, None, None, None)
        memory_store.init_db()

    def test_all_chat_backends_keep_their_expected_embedding_provider(self):
        # Only KoboldCpp uses its optional sidecar model. All other supported local
        # runtimes and hosted OpenCode keep the established Lemonade vector space.
        models = (
            ("lemonade-model", "lemonade"),
            ("lmstudio-model", "lemonade"),
            ("vllm-model", "lemonade"),
            ("koboldcpp/model", "kobold"),
            ("fastflowlm-model", "lemonade"),
            ("q38rocm-model", "lemonade"),
            ("opencode/model", "lemonade"),
        )
        for model, expected_provider in models:
            with self.subTest(model=model), \
                    patch.object(app.kobold, "owns_model", side_effect=lambda value: value.startswith("koboldcpp/")), \
                    patch.object(app, "_embed_rag_texts",
                                 return_value=(expected_provider, [[1.0, 0.0]])) as embed, \
                    patch.object(memory_store, "search", return_value=[{
                        "role": "user", "content": "A saved project detail", "created_at": 0.0,
                    }]) as search:
                note = app._recall_memory("user", "current", "a project detail?", model=model)

            self.assertIn("A saved project detail", note)
            embed.assert_called_once_with(["a project detail?"], expected_provider)
            self.assertEqual(search.call_args.kwargs["embedding_backend"], expected_provider)

    def test_kobold_embedding_failure_falls_back_to_lemonade_recall(self):
        with patch.object(app.kobold, "owns_model", return_value=True), \
                patch.object(app, "_ensure_embedding_model_loaded"), \
                patch.object(app.kobold, "embed", side_effect=kobold_client.KoboldError("not enabled")), \
                patch.object(app.lemonade, "embed", return_value=[[1.0, 0.0]]), \
                patch.object(memory_store, "search", return_value=[{
                    "role": "user", "content": "A saved detail", "created_at": 0.0,
                }]) as search:
            note = app._recall_memory("user", "current", "a saved detail?", model="koboldcpp/model")

        self.assertIn("A saved detail", note)
        self.assertEqual(search.call_args.kwargs["embedding_backend"], "lemonade")

    def test_kobold_exchange_uses_its_namespace_and_endpoint(self):
        with patch.object(app.kobold, "owns_model", return_value=True), \
                patch.object(app.kobold, "embed", return_value=[[1.0, 0.0], [0.0, 1.0]]) as embed:
            app._remember_exchange("user", "kobold-conversation", "question", "answer",
                                   model="koboldcpp/model")

        embed.assert_called_once_with(["question", "answer"])
        hits = memory_store.search("user", [1.0, 0.0], embedding_backend="kobold")
        self.assertEqual([hit["content"] for hit in hits], ["question"])

    def test_kobold_exchange_falls_back_to_lemonade_storage(self):
        with patch.object(app.kobold, "owns_model", return_value=True), \
                patch.object(app.kobold, "embed", side_effect=kobold_client.KoboldError("not enabled")), \
                patch.object(app, "_ensure_embedding_model_loaded"), \
                patch.object(app.lemonade, "embed", return_value=[[1.0, 0.0], [0.0, 1.0]]) as embed:
            app._remember_exchange("user", "fallback-conversation", "question", "answer",
                                   model="koboldcpp/model")

        embed.assert_called_once_with(["question", "answer"])
        hits = memory_store.search("user", [1.0, 0.0], embedding_backend="lemonade")
        self.assertEqual([hit["content"] for hit in hits], ["question"])


class KoboldEmbeddingClientTests(unittest.TestCase):
    def test_openai_embeddings_response_is_returned_in_input_order(self):
        response = Mock()
        response.json.return_value = {"data": [
            {"index": 1, "embedding": [0.0, 1.0]},
            {"index": 0, "embedding": [1.0, 0.0]},
        ]}
        with patch.object(kobold_client, "base_url", return_value="http://kobold"), \
                patch.object(kobold_client, "_auth_headers", return_value={}), \
                patch.object(kobold_client.requests, "post", return_value=response) as post:
            vectors = kobold_client.embed(["first", "second"])

        self.assertEqual(vectors, [[1.0, 0.0], [0.0, 1.0]])
        self.assertEqual(post.call_args.args[0], "http://kobold/v1/embeddings")
        self.assertEqual(post.call_args.kwargs["json"]["input"], ["first", "second"])

    def test_disabled_embeddings_endpoint_raises_kobold_error(self):
        response = Mock()
        response.raise_for_status.side_effect = kobold_client.requests.HTTPError("404")
        with patch.object(kobold_client.requests, "post", return_value=response):
            with self.assertRaises(kobold_client.KoboldError):
                kobold_client.embed(["question"])


if __name__ == "__main__":
    unittest.main()
