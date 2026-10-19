"""Exercise PostgreSQL semantics across the application's public store APIs."""
import unittest
import database
import browse_history_store
import chat_store
import metrics_store
import quiz_store
import settings_store
import users_store


class PostgreSQLStoreTests(unittest.TestCase):
    def setUp(self):
        scope = database.temporary_schema()
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)

    def test_users_authenticate_and_unique_conflicts_rollback(self):
        user = users_store.create_user('test-owner', 'password', 'admin')
        self.assertEqual(users_store.verify_login('test-owner', 'password')['id'], user['id'])
        self.assertIsNone(users_store.verify_login('test-owner', 'wrong'))
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
