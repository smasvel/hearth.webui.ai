"""Trace persistence must commit writes and release database connections."""
from pathlib import Path
import database
import tempfile
import time
import unittest
from unittest.mock import patch

import live_requests


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
