import database
import contextlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import chat_jobs
import chat_store
import settings_store


class BackgroundChatTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(database.temporary_schema())
        chat_store.init_db()
        settings_store.init_db()
        self.registry = chat_jobs.JobRegistry()
        self.stack.enter_context(patch.object(chat_jobs, "registry", self.registry))
        self.stack.enter_context(patch.object(app.users_store, "get_user_by_id", side_effect=lambda uid: {
            "id": uid, "username": uid, "role": "admin",
        }))
        for backend in [app.lemonade, app.vllm, app.sglang, app.kobold, app.fastflowlm]:
            self.stack.enter_context(patch.object(backend, "owns_model", return_value=False))
        self.stack.enter_context(patch.object(app, "_recall_memory", return_value=None))
        self.stack.enter_context(patch.object(app, "_remember_exchange"))
        self.stack.enter_context(patch.object(app.web_search, "should_search_web", return_value=False))
        self.stack.enter_context(patch.object(app.metrics_store, "record"))
        self.gate = threading.Event()
        self.addCleanup(self.gate.set)
        self.closed = threading.Event()
        self.started = threading.Event()

        def backend(*args, **kwargs):
            try:
                self.started.set()
                yield self.reasoning_chunk("Thinking live")
                yield self.chunk("First ")
                self.gate.wait(5)
                yield self.chunk("finished response")
                yield "data: [DONE]"
            finally:
                self.closed.set()

        self.stack.enter_context(patch.object(app.lms, "chat_stream", side_effect=backend))
        self.client = self.client_for("owner")
        self.body = {"model": "test-model", "conversation_id": "conversation",
                     "messages": [{"role": "user", "content": "Say hello"}], "max_tokens": 32}

    @staticmethod
    def chunk(text):
        return "data: " + json.dumps({"choices": [{"delta": {"content": text}}]})

    @staticmethod
    def reasoning_chunk(text):
        return "data: " + json.dumps({"choices": [{"delta": {"reasoning_content": text}}]})

    @staticmethod
    def client_for(uid):
        client = app.app.test_client()
        with client.session_transaction() as session:
            session.update(logged_in=True, user_id=uid, username=uid, role="admin",
                           last_activity=time.time(), login_csrf_token="test-csrf")
        return client

    def start(self):
        response = self.client.post("/api/chat/start", json=self.body,
                                    headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(response.status_code, 202, response.get_json())
        response.close()
        self.assertTrue(self.started.wait(2))
        self.wait_until(lambda: self.registry.get("owner", "conversation").snapshot()["content"] == "First ")
        return self.registry.get("owner", "conversation")

    def wait_until(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        self.fail("Background job did not reach the expected state")

    def test_response_finishes_and_is_saved_after_start_response_is_closed(self):
        job = self.start()
        # No browser is attached to a stream; an entirely new client returns later.
        self.gate.set()
        self.wait_until(lambda: job.status == "completed")
        returning = self.client_for("owner")
        data = returning.get("/api/conversations/conversation").get_json()
        self.assertEqual(data["messages"][-1]["content"], "First finished response")
        self.assertEqual(data["generation"]["status"], "completed")

    def test_active_response_can_be_recovered_by_a_new_client(self):
        job = self.start()
        returning = self.client_for("owner")
        data = returning.get("/api/conversations/conversation").get_json()
        self.assertEqual(data["generation"]["content"], "First ")
        self.assertEqual(data["generation"]["reasoning"], "Thinking live")
        self.assertEqual(data["generation"]["status"], "running")
        self.gate.set()
        self.wait_until(lambda: job.status != "running")

    def test_duplicate_send_does_not_duplicate_messages_or_workers(self):
        job = self.start()
        response = self.client.post("/api/chat/start", json=self.body,
                                    headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(response.status_code, 409)
        self.gate.set()
        self.wait_until(lambda: job.status != "running")
        self.assertEqual(len(chat_store.get_conversation("conversation", "owner")["messages"]), 2)

    def test_other_account_cannot_read_or_stop_job(self):
        job = self.start()
        other = self.client_for("other")
        self.assertEqual(other.get("/api/conversations/conversation").status_code, 404)
        self.assertEqual(other.post("/api/conversations/conversation/generation/stop",
                                   headers={"X-CSRF-Token": "test-csrf"}).status_code, 404)
        self.assertFalse(job.cancel.is_set())
        self.gate.set()
        self.wait_until(lambda: job.status != "running")

    def test_explicit_stop_closes_backend_and_saves_partial_reply(self):
        job = self.start()
        response = self.client.post("/api/conversations/conversation/generation/stop",
                                    headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(response.status_code, 200)
        self.gate.set()
        self.wait_until(lambda: job.status == "stopped")
        self.assertTrue(self.closed.is_set())
        self.assertEqual(chat_store.get_conversation("conversation", "owner")["messages"][-1]["content"], "First ")

    def test_media_reply_is_drained_and_saved_after_client_leaves(self):
        finished = threading.Event()

        def media(*args, **kwargs):
            yield self.chunk("Rendering") + "\n\n"
            self.gate.wait(5)
            yield self.chunk("Image ready") + "\n\n"
            # Media saves after the last SSE yield, which previously depended on the browser.
            chat_store.add_message("conversation", "assistant", "Image ready")
            finished.set()

        self.body["messages"][0]["content"] = "Draw an apple"
        with patch.object(app, "_stream_image_reply", side_effect=media):
            response = self.client.post("/api/chat/start", json=self.body,
                                        headers={"X-CSRF-Token": "test-csrf"})
            self.assertEqual(response.status_code, 202)
            response.close()
            self.gate.set()
            self.assertTrue(finished.wait(2))
            job = self.registry.get("owner", "conversation")
            self.wait_until(lambda: job.status == "completed")
        self.assertEqual(chat_store.get_conversation("conversation", "owner")["messages"][-1]["content"], "Image ready")


if __name__ == "__main__":
    unittest.main()
