import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import database
from tools.migrate_sqlite_to_postgres import migrate, discover


class PostgreSQLMigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / 'legacy.db'
        self.scope = database.temporary_schema()
        self.scope.__enter__()
        self.addCleanup(self.scope.__exit__, None, None, None)

    def sqlite(self, script):
        conn = sqlite3.connect(self.source)
        try:
            conn.executescript(script)
        finally:
            conn.close()

    def migrate(self):
        return migrate([self.source], self.root / 'backup')

    def test_preserves_values_blobs_ids_and_sequences(self):
        self.sqlite("""CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT INTO settings VALUES('sample','unicode — 100% ?');
            CREATE TABLE memory_chunks(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id TEXT,
                conversation_id TEXT,role TEXT,content TEXT,embedding BLOB,created_at REAL);
            INSERT INTO memory_chunks VALUES(42,'u','c','user','saved',X'0000803F',1792000000.123456);
            UPDATE sqlite_sequence SET seq=100 WHERE name='memory_chunks';""")
        report = self.migrate()
        self.assertEqual(report['rows'], 2)
        with database.connect() as conn:
            self.assertEqual(conn.execute('SELECT value FROM settings').fetchone()[0], 'unicode — 100% ?')
            row = conn.execute('SELECT * FROM memory_chunks').fetchone()
            self.assertEqual(row['embedding'], bytes.fromhex('0000803F'))
            self.assertEqual(row['created_at'], 1792000000.123456)
            self.assertEqual(conn.execute("SELECT nextval(pg_get_serial_sequence('memory_chunks','id'))").fetchone()[0], 101)
        self.assertTrue((self.root / 'backup/000-legacy.db').exists())
        self.assertEqual(migrate([self.source], None, verify_only=True)['rows'], 2)

    def test_preserves_historical_orphans_and_enforces_future_foreign_keys(self):
        self.sqlite("""CREATE TABLE messages(id INTEGER PRIMARY KEY,conversation_id TEXT,
            role TEXT,content TEXT,created_at REAL);
            INSERT INTO messages VALUES(7,'missing-parent','user','keep me',1);""")
        report = self.migrate()
        self.assertIn('messages_conversation_id_fkey', report['legacy_unvalidated_constraints'])
        with self.assertRaises(database.IntegrityError):
            with database.connect() as conn:
                conn.execute("INSERT INTO messages(conversation_id,role,content,created_at) VALUES('another-missing','user','invalid',1)")
        with database.connect() as conn:
            self.assertEqual(conn.execute('SELECT content FROM messages').fetchone()[0], 'keep me')

    def test_nonempty_target_is_never_overwritten(self):
        self.sqlite('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT); INSERT INTO settings VALUES(\'source\',\'original\');')
        with database.connect() as conn:
            conn.execute("INSERT INTO settings VALUES('target','keep')")
        with self.assertRaisesRegex(ValueError, 'not empty'):
            self.migrate()
        with database.connect() as conn:
            self.assertEqual(dict(conn.execute('SELECT * FROM settings').fetchone()), {'key':'target','value':'keep'})

    def test_unknown_table_rolls_back_instead_of_dropping_data(self):
        self.sqlite('CREATE TABLE unknown(data TEXT); INSERT INTO unknown VALUES(\'keep\');')
        with self.assertRaisesRegex(ValueError, 'Unrecognized'):
            self.migrate()
        with database.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM settings').fetchone()[0], 0)

    def test_discovery_excludes_backups(self):
        self.source.touch()
        (self.root/'sqlite-backups').mkdir()
        (self.root/'sqlite-backups'/'old.db').touch()
        self.assertEqual(discover(self.root), [self.source])
