"""Cross-chat memory recall must surface a directive-style personal-name fact.

Regression (2026-11-09, KoboldCPP Qwen3.8-2B): "what's my name?" relied purely on
embedding rank, where a stored past refusal outranked the actual "My name is ..."
chunk, and the soft recall framing let weak models answer "You didn't tell me your
name!" even with the fact injected. The name fact must now be recalled even when
the embedding search itself is unavailable.
"""
import database
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import memory_store
import app


class RecallMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "memory.db"
        self.db_patch = database.temporary_schema()
        self.db_patch.__enter__()
        memory_store.init_db()

    def tearDown(self):
        self.db_patch.__exit__(None, None, None)
        self.tempdir.cleanup()

    def _recall(self, query, conversation_id="current"):
        # Lemonade being down (embedding recall impossible) must not hide the
        # structured personal-name fact.
        with patch.object(app, "_ensure_embedding_model_loaded"), \
             patch.object(app.lemonade, "embed", side_effect=app.lemonade.LemonadeError("down")):
            return app._recall_memory("u", conversation_id, query)

    def test_name_query_returns_directive_fact_without_embeddings(self):
        memory_store.add_chunk("u", "past", "user", "My name is Maya Patel", [1.0])
        memory_store.add_chunk(
            "u", "past", "assistant",
            "I'm an AI assistant. I don't have a name - I'm just a helpful assistant.", [1.0],
        )

        note = self._recall("what's my name?")

        self.assertIsNotNone(note)
        self.assertIn("the user's name is Maya Patel", note)
        self.assertIn("do not claim that the name cannot be remembered", note)

    def test_recall_list_carries_directive_framing(self):
        with patch.object(app, "_ensure_embedding_model_loaded"), \
             patch.object(app.lemonade, "embed", return_value=[[1.0]]), \
             patch.object(memory_store, "search", return_value=[
                 {"role": "user", "content": "My name is Maya Patel", "created_at": 0.0},
             ]):
            note = app._recall_memory("u", "current", "what's my name?")

        self.assertIn("My name is Maya Patel", note)
        self.assertIn("Never claim you were never told", note)

    def test_non_name_query_returns_no_name_directive(self):
        memory_store.add_chunk("u", "past", "user", "My name is Maya Patel", [1.0])

        note = self._recall("how do I sort a list in python?")

        self.assertIsNone(note)

    def test_name_query_drops_general_recall_list(self):
        memory_store.add_chunk("u", "past", "user", "My name is Maya Patel", [1.0])
        noisy_hits = [
            {"role": "assistant", "content": "I'm an AI assistant. I don't have a name.", "created_at": 0.0},
            {"role": "assistant", "content": "Your location is 1436 Example St, Wake Forest NC", "created_at": 0.0},
        ]
        with patch.object(app, "_ensure_embedding_model_loaded"), \
             patch.object(app.lemonade, "embed", return_value=[[1.0]]), \
             patch.object(memory_store, "search", return_value=noisy_hits):
            note = app._recall_memory("u", "current", "what's my name?")

        self.assertIn("the user's name is Maya Patel", note)
        self.assertIn("not your own name", note)
        # Unrelated recalled chunks must not ride along on a name query.
        self.assertNotIn("I don't have a name", note)
        self.assertNotIn("Wake Forest", note)
        self.assertNotIn("other past conversations (may or may not", note)

    def test_location_questions_recall_the_saved_place_as_a_direct_fact(self):
        memory_store.add_chunk("u", "past", "user", "I'm located in Exampleville ZZ", [1.0])
        distracting_hits = [{
            "role": "assistant", "content": "[Clear, concise confirmation of your location]",
            "created_at": 0.0,
        }]
        for query in ("What's my location?", "Where am I located?", "What city am I in?"):
            with self.subTest(query=query), \
                    patch.object(app.kobold, "owns_model", return_value=True), \
                    patch.object(app, "_embed_rag_texts", return_value=("lemonade", [[1.0]])), \
                    patch.object(memory_store, "search", return_value=distracting_hits) as search:
                note = app._recall_memory("u", "current", query, model="koboldcpp/model")

            self.assertIn("the user's current city/location is Exampleville ZZ", note)
            self.assertIn("answer with this saved place directly and concisely", note)
            self.assertNotIn("Clear, concise confirmation", note)
            self.assertEqual(search.call_args.kwargs["embedding_backend"], "lemonade")

    def test_location_question_does_not_inject_placeholder_when_no_place_is_saved(self):
        with patch.object(app.kobold, "owns_model", return_value=True), \
                patch.object(app, "_embed_rag_texts", return_value=("lemonade", [[1.0]])), \
                patch.object(memory_store, "search", return_value=[{
                    "role": "assistant",
                    "content": "[Clear, concise confirmation of your location]",
                    "created_at": 0.0,
                }]):
            note = app._recall_memory(
                "u", "current", "What's my location?", model="koboldcpp/model")

        self.assertIsNone(note)

    def test_weather_recall_keeps_other_relevant_memory_snippets(self):
        memory_store.add_chunk("u", "past", "user", "My city is Exampleville ZZ", [1.0])
        with patch.object(app, "_ensure_embedding_model_loaded"), \
                patch.object(app.lemonade, "embed", return_value=[[1.0]]), \
                patch.object(memory_store, "search", return_value=[{
                    "role": "user", "content": "I prefer Celsius forecasts", "created_at": 0.0,
                }]) as search:
            note = app._recall_memory("u", "current", "What's the weather tomorrow?")

        self.assertIn("Exampleville ZZ", note)
        self.assertIn("I prefer Celsius forecasts", note)
        self.assertEqual(search.call_args.kwargs["embedding_backend"], "lemonade")


class MemoryInjectionPlacementTests(unittest.TestCase):
    """The recall note must reach the model adjacent to the user's question.

    Weak backends (verified live with KoboldCPP Qwen3.8-2B) ignore the note when
    it lives in the leading system message but follow it next to the question.
    """

    def setUp(self):
        import chat_store
        import settings_store
        import chat_jobs
        import contextlib
        import time
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(database.temporary_schema())
        chat_store.init_db()
        settings_store.init_db()
        self.stack.enter_context(patch.object(chat_jobs, "registry", chat_jobs.JobRegistry()))
        self.stack.enter_context(patch.object(app.users_store, "get_user_by_id", side_effect=lambda uid: {
            "id": uid, "username": uid, "role": "admin",
        }))
        for backend in [app.lemonade, app.vllm, app.kobold, app.fastflowlm, app.q38rocm, app.zen]:
            self.stack.enter_context(patch.object(backend, "owns_model", return_value=False))
        self.stack.enter_context(patch.object(app, "_recall_memory", return_value=(
            "Known personal fact explicitly saved from a past conversation: "
            "the user's name is Maya Patel."
        )))
        self.stack.enter_context(patch.object(app, "_remember_exchange"))
        self.stack.enter_context(patch.object(app.web_search, "should_search_web", return_value=False))
        self.stack.enter_context(patch.object(app.mcp_tools_client, "ensure_started",
                                              side_effect=RuntimeError("no mcp in test")))
        self.stack.enter_context(patch.object(app.metrics_store, "record"))
        self.captured = {}

        def backend_stream(messages, model, **kwargs):
            self.captured["messages"] = [dict(m) for m in messages]
            yield "data: " + json.dumps({"choices": [{"delta": {"content": "Your name is Maya Patel."}}]})
            yield "data: [DONE]"

        self.stack.enter_context(patch.object(app.lms, "chat_stream", side_effect=backend_stream))
        self.client = app.app.test_client()
        with self.client.session_transaction() as session:
            session.update(logged_in=True, user_id="u", username="u", role="admin",
                           last_activity=time.time(), login_csrf_token="test-csrf")

    def test_note_is_injected_into_latest_user_message_not_system(self):
        import chat_store
        body = {"model": "test-model", "conversation_id": "conv",
                "messages": [{"role": "user", "content": "what's my name?"}], "max_tokens": 32}
        response = self.client.post("/api/chat", json=body, headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(response.status_code, 200, response.get_json())
        # Drain the streamed response through its terminal event so the background
        # worker completes before this test tears down its temporary database schema.
        response.get_data()
        response.close()

        sent = self.captured["messages"]
        self.assertIn("Hearth cross-chat memory", sent[-1]["content"])
        self.assertIn("Maya Patel", sent[-1]["content"])
        self.assertTrue(sent[-1]["content"].endswith("recalled memory above.]"))
        # The system channel must not carry the note (weak models ignore it there).
        system = next((m for m in sent if m.get("role") == "system"), None)
        self.assertTrue(system is None or "Maya Patel" not in system["content"])
        # Saved history keeps the clean user text, not the injected note.
        saved = chat_store.get_conversation("conv", "u")["messages"]
        self.assertEqual(saved[0]["content"], "what's my name?")


if __name__ == "__main__":
    unittest.main()
