"""Console API keys must be stored encrypted in PostgreSQL, configured and tested
directly from the Settings panel."""
import contextlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import database
import settings_store


class OpenCodeKeyStorageTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(database.temporary_schema())
        settings_store.init_db()
        import app
        self.app = app
        self.stack.enter_context(patch.object(
            app.users_store, 'get_user_by_id',
            side_effect=lambda uid: {'id': uid, 'username': uid, 'role': 'admin'}))
        self.client = app.app.test_client()
        with self.client.session_transaction() as session:
            session.update(logged_in=True, user_id='admin', username='admin', role='admin',
                           login_csrf_token='token', last_activity=time.time())
        self.headers = {'X-CSRF-Token': 'token'}
        # The intended upstream is not reachable from tests; the storage assertions
        # below must happen without any network access.
        self.stack.enter_context(patch.object(
            app.requests, 'post', side_effect=AssertionError('no network in storage test')))

    def _submit(self, payload):
        return self.client.post('/api/settings', json=payload, headers=self.headers)

    def _raw_db_value(self):
        import database
        with database.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = 'opencode_api_key'").fetchone()
        return row['value'] if row else None

    def test_key_is_configured_and_persisted_encrypted(self):
        import app
        with patch.object(app.zen, 'api_key', return_value=''):
            self.assertEqual(self.client.get('/api/settings').get_json()['opencode_api_key'], '')
        response = self._submit({'opencode_api_key': 'sk-live-zen-key'})
        self.assertEqual(response.status_code, 200)
        stored = self._raw_db_value()
        self.assertTrue(stored.startswith('v1:'), 'the key must never be written in plaintext')
        self.assertNotIn('sk-live-zen-key', stored)
        with patch.object(self.app.zen, 'api_key', return_value=settings_store.get('opencode_api_key')):
            snapshot = self.client.get('/api/settings').get_json()
        self.assertEqual(snapshot['opencode_api_key'], 'sk-live-zen-key')

    def test_key_only_written_when_the_panel_sends_it(self):
        self._submit({'opencode_api_key': 'sk-live-zen-key'})
        self._submit({'kobold_gpu_layers': '32'})
        self.assertEqual(self._raw_db_value().startswith('v1:'), True)

    def test_clearing_the_key_removes_it(self):
        self._submit({'opencode_api_key': 'sk-live-zen-key'})
        self.assertTrue(self._raw_db_value())
        self._submit({'opencode_api_key': ''})
        self.assertIsNone(self._raw_db_value())

    def test_unknown_test_target_is_rejected(self):
        import app
        with patch.object(app, '_read_caddy_info', return_value=None):
            response = self.client.post('/api/settings/test',
                json={'target': 'opencode_typo'}, headers=self.headers)
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
