"""Exercise PostgreSQL semantics across the application's public store APIs."""
import unittest
import os
import tempfile
import database
import browse_history_store
import chat_store
import metrics_store
import quiz_store
import settings_store
import secret_store
import users_store


class PostgreSQLStoreTests(unittest.TestCase):
    def setUp(self):
        scope = database.temporary_schema()
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)

    def test_temporary_schema_uses_private_agent_runtime_directory(self):
        outer_runtime = os.environ['HEARTH_AGENT_RUNTIME_DIR']
        with database.temporary_schema():
            isolated_runtime = os.environ['HEARTH_AGENT_RUNTIME_DIR']
            self.assertNotEqual(isolated_runtime, outer_runtime)
            self.assertNotEqual(isolated_runtime, str(database.DATA_DIR / 'agent-runtime'))
            self.assertTrue(os.path.isdir(isolated_runtime))
        self.assertEqual(os.environ['HEARTH_AGENT_RUNTIME_DIR'], outer_runtime)
        self.assertFalse(os.path.exists(isolated_runtime))

    def test_users_authenticate_and_unique_conflicts_rollback(self):
        user = users_store.create_user('test-owner', 'password', 'admin')
        self.assertEqual(users_store.verify_login('test-owner', 'password')['id'], user['id'])
        self.assertIsNone(users_store.verify_login('test-owner', 'wrong'))
        with database.connect() as conn:
            stored = conn.execute(
                'SELECT username, password_hash, role, created_at, caddy_token, agentic_workflow_enabled '
                'FROM users WHERE id = %s', (user['id'],)
            ).fetchone()
        self.assertEqual(stored['username'], 'test-owner')
        self.assertNotEqual(stored['password_hash'], 'password')
        self.assertTrue(stored['password_hash'].startswith('scrypt:'))
        self.assertEqual(stored['role'], 'admin')
        self.assertIsInstance(stored['created_at'], float)
        self.assertTrue(stored['caddy_token'])
        self.assertTrue(stored['agentic_workflow_enabled'])
        self.assertNotIn('password_hash', users_store.get_user_by_id(user['id']))
        listed = users_store.list_users()['users'][0]
        self.assertEqual(listed['username'], 'test-owner')
        self.assertNotIn('password_hash', listed)
        with self.assertRaises(database.IntegrityError):
            users_store.create_user('test-owner', 'duplicate')
        self.assertEqual(users_store.count_users(), 1)
        self.assertEqual(users_store.count_admins(), 1)

    def test_chat_ownership_truncation_and_cascade(self):
        chat_store.ensure_conversation('c', 'owner', 'model', 'First message')
        for value in ('one', 'two', 'three'):
            chat_store.add_message('c', 'user', value)
        self.assertIsNone(chat_store.get_conversation('c', 'other'))
        self.assertFalse(chat_store.truncate_messages('c', 'other', 0))
        self.assertTrue(chat_store.truncate_messages('c', 'owner', 1))
        self.assertEqual([m['content'] for m in chat_store.get_conversation('c','owner')['messages']], ['one'])
        chat_store.delete_conversation('c', 'owner')
        with database.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 0)

    def test_history_search_remains_case_insensitive(self):
        browse_history_store.add_visit('https://example.org', 'UpperCase title', None, 'test', True)
        self.assertEqual(browse_history_store.list_history('uppercase')['total'], 1)

    def test_metrics_numeric_and_case_insensitive_text_sorting(self):
        for model, duration in [('zeta', 10), ('Alpha', 2), ('beta', 100)]:
            metrics_store.record(source='test', model=model, duration_ms=duration, ts=3600.75)
        rows = metrics_store.recent(hours=None, sort='duration_ms', direction='asc')['rows']
        self.assertEqual([r['duration_ms'] for r in rows], [2,10,100])
        rows = metrics_store.recent(hours=None, sort='model', direction='asc')['rows']
        self.assertEqual([r['model'] for r in rows], ['Alpha','beta','zeta'])
        self.assertEqual(metrics_store.recent(hours=None, model='ALPHA')['total'], 1)
        self.assertEqual(metrics_store.summary(hours=None)['series'][0]['bucket'], 3600)

    def test_settings_upsert_and_quiz_cascade(self):
        settings_store.set('test', 'one')
        settings_store.set('test', 'two')
        self.assertEqual(settings_store.get('test'), 'two')
        settings_store.set('test', None)
        self.assertIsNone(settings_store.get('test'))
        deck = quiz_store.create_deck('owner', 'A quiz', [{'q':'Question?', 'options':['A','B'], 'answer':0}])
        self.assertEqual(quiz_store.get_bank(deck['slug'], 'owner')['questions'][0]['q'], 'Question?')
        self.assertFalse(quiz_store.delete_deck('other', deck['slug']))
        self.assertTrue(quiz_store.delete_deck('owner', deck['slug']))
        with database.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM quiz_questions').fetchone()[0], 0)

    def test_settings_credentials_and_flask_session_key_are_encrypted_at_rest(self):
        settings_store.set('lmstudio_api_key', 'test-provider-secret')
        self.assertEqual(settings_store.get('lmstudio_api_key'), 'test-provider-secret')
        key = secret_store.get_or_create_flask_key()
        self.assertEqual(secret_store.get_or_create_flask_key(), key)
        with database.connect() as conn:
            api_row = conn.execute("SELECT value FROM settings WHERE key='lmstudio_api_key'").fetchone()
            session_row = conn.execute("SELECT encrypted_value FROM app_secrets WHERE name='flask_session_key'").fetchone()
        self.assertTrue(api_row['value'].startswith('v1:'))
        self.assertNotIn('test-provider-secret', api_row['value'])
        self.assertTrue(session_row['encrypted_value'].startswith('v1:'))
        self.assertNotIn(key, session_row['encrypted_value'])

    def test_legacy_backend_environment_settings_migrate_before_file_cleanup(self):
        from pathlib import Path
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / '.env'
            env_file.write_text('LMSTUDIO_BASE_URL=http://legacy.test:1234\nLMSTUDIO_API_KEY=legacy-secret\n')
            with patch.object(settings_store, 'ROOT', Path(directory)), patch.dict(os.environ, {
                'HEARTH_SKIP_LEGACY_CONFIG_MIGRATION': '0',
                'LMSTUDIO_BASE_URL': 'http://legacy.test:1234',
                'LMSTUDIO_API_KEY': 'legacy-secret',
            }, clear=False):
                settings_store.migrate_legacy_environment()
                self.assertNotIn('LMSTUDIO_BASE_URL', os.environ)
                self.assertNotIn('LMSTUDIO_API_KEY', os.environ)
            self.assertEqual(settings_store.get('lmstudio_base_url'), 'http://legacy.test:1234')
            self.assertEqual(settings_store.get('lmstudio_api_key'), 'legacy-secret')
            self.assertNotIn('legacy-secret', env_file.read_text())
            with database.connect() as conn:
                ciphertext = conn.execute("SELECT value FROM settings WHERE key='lmstudio_api_key'").fetchone()['value']
            self.assertTrue(ciphertext.startswith('v1:'))
