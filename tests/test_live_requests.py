"""Trace persistence must commit writes and release database connections."""
from pathlib import Path
from hearth.storage import database
import tempfile
import time
import unittest
from unittest.mock import patch

from hearth.core import live_requests


class TraceStorageTests(unittest.TestCase):
    def setUp(self):
        self.scope = database.temporary_schema()
        self.scope.__enter__()
        self.addCleanup(self.scope.__exit__, None, None, None)
        self.connections = []
        original_connect = database.connect

        def connect(*args, **kwargs):
            conn = original_connect(*args, **kwargs)
            self.connections.append(conn)
            return conn

        for patcher in (patch.object(live_requests, '_recent', []),
                        patch.object(database, 'connect', side_effect=connect)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def assert_connections_closed(self):
        self.assertTrue(self.connections)
        self.assertTrue(all(conn.closed for conn in self.connections))

    def test_initialize_and_persist_survive_reopen(self):
        self.assertEqual(live_requests._init_trace_store(), 1)
        now = time.monotonic()
        live_requests._persist_finished({'id': 7, 'started': now - 2,
            'updated': now, 'finished_at': time.time(), 'response_content': 'saved'})
        self.assertEqual(live_requests._init_trace_store(), 8)
        self.assertEqual(live_requests._recent[-1]['response_content'], 'saved')
        self.assert_connections_closed()

    def test_unfinished_trace_is_recovered_as_interrupted(self):
        live_requests._init_trace_store()
        live_requests._persist_started({
            "id": 7, "status": "streaming", "started_at": time.time() - 2,
            "request_content": "saved request", "tokens": 0,
        })
        self.assertEqual(live_requests._init_trace_store(), 8)
        self.assertEqual(live_requests._recent, [])
        self.assertEqual(live_requests._init_trace_store(recover_interrupted=True), 8)
        entry = live_requests._recent[-1]
        self.assertEqual(entry["status"], "error")
        self.assertIn("restarted", entry["error_detail"])
        self.assertEqual(entry["request_content"], "saved request")
        self.assertIsNotNone(entry["finished_at"])
        self.assert_connections_closed()

    def test_reset_preserves_active_checkpoint_and_removes_finished_trace(self):
        live_requests._init_trace_store()
        now = time.monotonic()
        live_requests._persist_finished({
            "id": 6, "started": now, "updated": now, "finished_at": time.time(),
        })
        live_requests._persist_started({
            "id": 7, "status": "streaming", "started_at": time.time(),
        })
        with patch.object(live_requests, "_active", {7: {"id": 7}}):
            live_requests.clear_all()

        self.assertEqual(live_requests._init_trace_store(recover_interrupted=True), 8)
        self.assertEqual([row["id"] for row in live_requests._recent], [7])
        self.assert_connections_closed()

    def test_failed_persistence_closes_connection(self):
        live_requests._init_trace_store()
        now = time.monotonic()
        live_requests._persist_finished({'id': 2, 'started': now,
            'updated': now, 'finished_at': time.time(), 'invalid': object()})
        self.assert_connections_closed()
        self.assertEqual(live_requests._init_trace_store(), 1)

    def test_failed_initialization_closes_connection(self):
        with self.assertRaises(database.Error):
            with patch.object(live_requests, '_RECENT_MAX', object()):
                live_requests._init_trace_store()
        self.assert_connections_closed()


if __name__ == '__main__':
    unittest.main()
