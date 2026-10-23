from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from dotenv import dotenv_values
from tools import first_time_setup as setup


class FirstTimeSetupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / '.env.example').write_text('ADMIN_USERNAME=admin\nFLASK_SECRET_KEY=\nDATABASE_URL=\n')
        patcher = patch.object(setup, 'ROOT', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_generates_matching_private_credentials_and_preserves_them(self):
        first = setup.prepare_environment(55435)
        original = (self.root / '.env.postgres').read_bytes()
        second = setup.prepare_environment(55436)
        self.assertEqual(first, second)
        self.assertEqual(original, (self.root / '.env.postgres').read_bytes())
        self.assertIn(':55435/', first['DATABASE_URL'])
        self.assertEqual(first['DATABASE_URL'], dotenv_values(self.root / '.env.postgres')['DATABASE_URL'])
        self.assertEqual(len(first['FLASK_SECRET_KEY']), 64)
        for name in ('.env', '.env.postgres'):
            self.assertEqual((self.root / name).stat().st_mode & 0o777, 0o600)

    def test_conflicting_existing_database_is_not_rewired(self):
        setup.prepare_environment(55435)
        path = self.root / '.env'
        path.write_text('DATABASE_URL=postgresql://other/database\nFLASK_SECRET_KEY=existing\n')
        original = path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'different databases'):
            setup.prepare_environment(55435)
        self.assertEqual(original, path.read_bytes())

    def test_existing_database_without_compose_config_is_not_replaced(self):
        (self.root / '.env').write_text('DATABASE_URL=postgresql://existing/database\n')
        with self.assertRaisesRegex(RuntimeError, 'no matching'):
            setup.prepare_environment(55435)
        self.assertFalse((self.root / '.env.postgres').exists())

    def test_detects_populated_sqlite_and_leaves_it_intact(self):
        (self.root / 'data').mkdir()
        path = self.root / 'data/chat_history.db'
        conn = sqlite3.connect(path)
        try:
            conn.executescript("CREATE TABLE old_data(value TEXT); INSERT INTO old_data VALUES('keep');")
        finally:
            conn.close()
        original = path.read_bytes()
        self.assertTrue(setup.has_sqlite_data())
        self.assertEqual(path.read_bytes(), original)

    def test_empty_sqlite_has_no_data(self):
        (self.root / 'chat_history.db').touch()
        self.assertFalse(setup.has_sqlite_data())

    def test_service_handles_spaces_and_percent_specifiers(self):
        content = setup.service_text(Path('/home/user/AI Projects/100% hearth'))
        self.assertIn('WorkingDirectory=/home/user/AI Projects/100%% hearth', content)
        self.assertIn('"/home/user/AI Projects/100%% hearth/.venv/bin/python"', content)
        self.assertNotIn('/home/YOUR_USERNAME/', content)
