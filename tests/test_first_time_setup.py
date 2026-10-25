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
        (self.root / '.env.example').write_text('DATABASE_URL=\n')
        (self.root / 'agent_container').mkdir()
        (self.root / 'agent_container' / 'IMAGE_VERSION').write_text('2.1.2\n')
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
        self.assertNotIn('FLASK_SECRET_KEY', first)
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

    def test_setup_initializes_database_settings_and_encrypted_session_key(self):
        from unittest.mock import patch
        env_path = self.root / '.env'
        env_path.write_text('DATABASE_URL=postgresql://test/db\nFLASK_SECRET_KEY=legacy-session-secret\n')
        reports = self.root / 'data' / 'model-test-runs'
        with patch.dict('os.environ', {'FLASK_SECRET_KEY': 'legacy-session-secret'}, clear=False), \
             patch('database.DATA_DIR', self.root / 'data'), \
             patch('database.init_schema') as init_schema, \
             patch('users_store.init_db') as init_users, \
             patch('users_store.count_users', return_value=2), \
             patch('agent_store.init_db') as init_agents, \
             patch('settings_store.init_db') as init_settings, \
             patch('settings_store.migrate_legacy_environment') as migrate_settings, \
             patch('secret_store.get_or_create_flask_key', return_value='stable-key') as save_secret:
            self.assertEqual(setup.initialize_application_storage(), 2)
            self.assertNotIn('FLASK_SECRET_KEY', __import__('os').environ)
        init_schema.assert_called_once_with()
        init_users.assert_called_once_with()
        init_agents.assert_called_once_with()
        init_settings.assert_called_once_with()
        save_secret.assert_called_once_with('legacy-session-secret')
        migrate_settings.assert_called_once_with()
        self.assertNotIn('FLASK_SECRET_KEY', env_path.read_text())
        self.assertTrue(reports.is_dir())
        self.assertEqual(reports.stat().st_mode & 0o777, 0o700)

    def test_canonical_ddl_creates_runtime_settings_and_app_secrets(self):
        schema = (Path(__file__).resolve().parents[1] / 'database_schema.sql').read_text()
        self.assertIn('CREATE TABLE IF NOT EXISTS settings', schema)
        self.assertIn('CREATE TABLE IF NOT EXISTS app_secrets', schema)
        self.assertIn('CREATE TABLE IF NOT EXISTS agent_tasks', schema)
        self.assertIn('CREATE TABLE IF NOT EXISTS agent_workspace_files', schema)

    def test_empty_sqlite_has_no_data(self):
        (self.root / 'chat_history.db').touch()
        self.assertFalse(setup.has_sqlite_data())

    def test_service_handles_spaces_and_percent_specifiers(self):
        content = setup.service_text(Path('/home/user/AI Projects/100% hearth'))
        self.assertIn('WorkingDirectory=/home/user/AI Projects/100%% hearth', content)
        self.assertIn('"/home/user/AI Projects/100%% hearth/.venv/bin/python"', content)
        self.assertNotIn('/home/YOUR_USERNAME/', content)

    def test_agent_runtime_is_built_when_missing(self):
        with patch.object(setup.subprocess, 'run', side_effect=[
                setup.subprocess.CompletedProcess([], 1),
                setup.subprocess.CompletedProcess([], 0),
                setup.subprocess.CompletedProcess([], 0)]) as run:
            setup.install_agent_runtime()
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args_list[1].args[0][:5],
                         ['docker', 'build', '--pull', '--tag', 'hearth-agent-opencode:2.1.2'])

    def test_agent_runtime_rebuild_option_refreshes_existing_image(self):
        with patch.object(setup.subprocess, 'run', side_effect=[
                setup.subprocess.CompletedProcess([], 0),
                setup.subprocess.CompletedProcess([], 0),
                setup.subprocess.CompletedProcess([], 0)]) as run:
            setup.install_agent_runtime(rebuild=True)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args_list[1].args[0][1], 'build')
