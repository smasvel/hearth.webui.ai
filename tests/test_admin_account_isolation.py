"""Account boundaries and the administrator-only Live Logs exception."""
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

import app
from hearth.storage import database


class AdminAccountIsolationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(database.temporary_schema())
        self.owner, self.owner_id = self.create_admin("owner")
        self.admin, self.admin_id = self.create_admin("second-admin")
        self.csrf = {"X-CSRF-Token": "test"}
        self.stack.enter_context(patch.object(app, "_model_test_registry", app.model_tests.Registry()))

    @staticmethod
    def create_admin(username):
        user_id = app.users_store.create_user(username, "fixture-password", "admin")["id"]
        client = app.app.test_client()
        with client.session_transaction() as session:
            session.update(logged_in=True, user_id=user_id, last_activity=time.time(),
                           login_csrf_token="test")
        return client, user_id

    def test_admin_cannot_take_over_another_account(self):
        path = f"/api/users/{self.owner_id}"
        token = self.owner.get(path + "/caddy-token").json["caddy_token"]
        with self.admin:
            self.assertEqual(self.admin.get(path + "/caddy-token").status_code, 404)
            self.assertEqual(self.admin.post(path + "/regenerate-caddy-token",
                                             headers=self.csrf).status_code, 404)
            self.assertEqual(self.admin.post(path + "/reset-password", headers=self.csrf,
                                             json={"new_password": "different-password"}).status_code, 404)
        self.assertEqual(app.users_store.get_caddy_token(self.owner_id), token)
        self.assertIsNotNone(app.users_store.verify_login("owner", "fixture-password"))
        created = self.admin.post("/api/users", headers=self.csrf, json={
            "username": "new-account", "password": "test-initial-password",
        })
        self.assertEqual(created.status_code, 201)
        self.assertNotIn("caddy_token", created.json["user"])
        self.assertNotIn("caddy_snippet", created.json)

    def test_generated_media_is_private_even_with_a_known_filename(self):
        root = Path(self.stack.enter_context(TemporaryDirectory()))
        for setting, directory, filename in (("IMAGES_DIR", "images", "fixture.png"),
                                             ("VIDEOS_DIR", "videos", "fixture.webm")):
            with self.subTest(directory=directory), patch.object(app, setting, root / directory):
                app._save_private_media(root / directory, self.owner_id, filename, b"private media")
                path = f"/api/{directory}/{filename}"
                with self.owner.get(path) as response:
                    self.assertEqual(response.get_data(), b"private media")
                    self.assertIn("no-store", response.headers["Cache-Control"])
                self.assertEqual(self.admin.get(path).status_code, 404)

    def test_admin_live_log_visibility_does_not_allow_cancelling_or_clearing_anothers_trace(self):
        store = app.live_requests
        request_id = store.start(source="fixture", backend="fixture", model="fixture",
                                 request_content="private trace", owner_id=self.owner_id)
        self.addCleanup(store.clear_all, owner_id=self.owner_id)
        self.addCleanup(store.finish, request_id)
        path = f"/api/metrics/live/{request_id}"
        self.assertEqual(self.owner.get(path + "/trace").json["request_content"], "private trace")
        rows = self.admin.get("/api/metrics/live").json["rows"]
        self.assertFalse(next(row for row in rows if row["id"] == request_id)["can_cancel"])
        owner_rows = self.owner.get("/api/metrics/live").json["rows"]
        self.assertTrue(next(row for row in owner_rows if row["id"] == request_id)["can_cancel"])
        self.assertEqual(self.admin.get(path + "/trace").json["request_content"], "private trace")
        account = app.users_store.create_user("regular", "fixture-password")
        regular = app.app.test_client()
        with regular.session_transaction() as session:
            session.update(logged_in=True, user_id=account["id"], last_activity=time.time())
        self.assertEqual(regular.get("/api/metrics/live").status_code, 403)
        self.assertEqual(regular.get(path + "/trace").status_code, 403)
        self.assertEqual(self.admin.post(path + "/cancel", headers=self.csrf).status_code, 409)
        self.assertFalse(store.cancel_event(request_id).is_set())
        store.finish(request_id)
        self.assertEqual(self.admin.post("/api/metrics/live/reset", headers=self.csrf).status_code, 200)
        self.assertEqual(self.admin.get(path + "/trace").status_code, 200)
        self.assertEqual(self.owner.get(path + "/trace").status_code, 200)

    def test_browse_history_search_and_clear_are_account_scoped(self):
        store = app.browse_history_store
        token = store.current_owner.set(self.owner_id)
        try:
            store.add_visit("https://example.org/fixture", "Private article", "private lookup", "test", True)
        finally:
            store.current_owner.reset(token)
        path = "/api/browse-history"
        visit = self.owner.get(path + "?q=private").json["entries"][0]
        self.assertEqual(self.admin.get(path + "?q=private").json["entries"], [])
        self.assertEqual(self.admin.delete(f"{path}/{visit['id']}", headers=self.csrf).status_code, 404)
        self.assertEqual(self.admin.delete(path, headers=self.csrf).json["deleted"], 0)
        self.assertEqual(self.owner.get(path).json["total"], 1)

    def test_metrics_and_auth_activity_do_not_show_other_accounts(self):
        app.metrics_store.record(source="fixture", model="private-model", backend="fixture",
                                 request_type="chat_completion", ask_category="code", user_id=self.owner_id)
        app.users_store.record_auth_event("owner", "login", True, user_id=self.owner_id)
        row = self.owner.get("/api/metrics/recent").json["rows"][0]
        self.assertEqual(self.owner.get("/api/metrics").json["totals"]["requests"], 1)
        self.assertEqual(self.admin.get("/api/metrics").json["totals"]["requests"], 0)
        self.assertEqual(self.admin.get("/api/metrics/recent").json["rows"], [])
        self.assertEqual(self.admin.get("/api/auth-events").json["events"], [])
        self.assertEqual(self.owner.get("/api/auth-events").json["total"], 1)
        path = f"/api/metrics/requests/{row['id']}/coding-review"
        self.assertEqual(self.admin.post(path, json={"passed": True}, headers=self.csrf).status_code, 404)
        self.assertEqual(self.admin.post("/api/metrics/reset", headers=self.csrf).status_code, 200)
        self.assertEqual(self.owner.get("/api/metrics/recent").json["total"], 1)

    def test_model_scenarios_and_saved_runs_are_private_to_their_creator(self):
        app.model_test_scenario_store.init_db()
        config = {"name": "Private test", "prompt": "Private question", "expected_answer": "Private answer"}
        base = "/api/model-tests"
        scenario = self.owner.post(base + "/scenarios", json=config,
                                   headers=self.csrf).json["scenario"]
        scenario_path = f"{base}/scenarios/{scenario['id']}"
        self.assertEqual(self.admin.get(base + "/scenarios").json["scenarios"], [])
        for method, data in (("PUT", config), ("PATCH", {"enabled": False}), ("DELETE", {})):
            with self.subTest(method=method):
                response = self.admin.open(scenario_path, method=method, json=data, headers=self.csrf)
                self.assertEqual(response.status_code, 404)
        self.assertEqual(self.owner.get(base + "/scenarios").json["scenarios"][0]["prompt"], config["prompt"])

        job = {"id": "private-run", "started_at": time.time(), "status": "completed",
               "owner_id": self.owner_id, "results": [{"response": "Private reply"}]}
        app.model_metadata_store.save_test_run(job)
        path = f"{base}/{job['id']}"
        self.assertEqual(self.owner.get(path + "?download=1").json["results"], job["results"])
        self.assertEqual(self.admin.get(base).json["jobs"], [])
        self.assertEqual(self.admin.get(path + "?download=1").status_code, 404)
        self.assertEqual(self.admin.post(path + "/cancel", headers=self.csrf).status_code, 404)
        self.assertEqual(self.admin.delete(path, headers=self.csrf).status_code, 404)
        self.assertEqual(self.owner.get(path).status_code, 200)
