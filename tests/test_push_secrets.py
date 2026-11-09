import unittest
from unittest import mock
from io import StringIO

from tools import check_push_secrets as scanner


class PushSecretCheckTests(unittest.TestCase):
    def test_detects_credentials_and_private_identifiers_without_echoing_values(self):
        token = b'ghp_' + b'A' * 36
        data = b'api_key = "' + token + b'"\nurl = "https://device.private.' + b'ts.net"\n'
        findings = scanner.scan_content(data, 'sample.txt')
        self.assertTrue(any('provider-token' in item for item in findings))
        self.assertTrue(any('tailnet-name' in item for item in findings))
        self.assertFalse(any(token.decode() in item for item in findings))

    def test_known_documentation_placeholders_are_allowed(self):
        data = b'https://HOSTNAME.ts.net\nPOSTGRES_PASSWORD={password}\n'
        self.assertEqual(scanner.scan_content(data, 'README.md'), [])

    def test_real_private_ip_is_blocked(self):
        data = b'host = "10.' + b'44.55.66"\n'
        self.assertEqual(scanner.scan_content(data, 'settings.txt'),
                         ['settings.txt:1: private-ip'])

    def test_placeholder_in_variable_name_does_not_exempt_credential(self):
        data = b'api_key_example = "' + b'actual-private-' + b'value-123456"\n'
        self.assertEqual(scanner.scan_content(data, 'settings.py'),
                         ['settings.py:1: credential-assignment'])

    def test_sensitive_filename_in_tests_is_not_exempt(self):
        self.assertTrue(scanner.SENSITIVE_NAME.search('tests/production-credentials'))

    def test_refs_from_push_reads_local_remote_sha_pairs_and_ignores_deletions(self):
        stdin = StringIO(
            f'origin {"a" * 40} refs/heads/topic {"b" * 40}\n'
            f'origin {scanner.ZERO} refs/heads/removed {"c" * 40}\n'
        )
        with mock.patch.object(scanner.sys, 'stdin', stdin):
            self.assertEqual(scanner.refs_from_push(), [("a" * 40, "b" * 40)])

    def test_refs_from_push_rejects_malformed_input(self):
        with mock.patch.object(scanner.sys, 'stdin', StringIO('not a ref\n')):
            with self.assertRaisesRegex(RuntimeError, 'Invalid pre-push input'):
                scanner.refs_from_push()

    def test_objects_for_refs_excludes_objects_already_on_remote(self):
        with mock.patch.object(scanner, 'git', return_value=(b'abc file.py\ndef file.py\n')) as git:
            objects = scanner.objects_for_refs([('local', 'remote')])
        self.assertEqual(objects, {'abc': {'file.py'}, 'def': {'file.py'}})
        git.assert_called_once_with('rev-list', '--objects', 'local', '^remote')

    def test_objects_for_new_branch_does_not_exclude_zero_remote(self):
        with mock.patch.object(scanner, 'git', return_value=b'abc file.py\n') as git:
            objects = scanner.objects_for_refs([('local', scanner.ZERO)])
        self.assertEqual(objects, {'abc': {'file.py'}})
        git.assert_called_once_with('rev-list', '--objects', 'local')

    def test_scan_objects_skips_non_blobs_and_scans_blob_paths(self):
        def git(*args):
            if args[:2] == ('cat-file', '-t'):
                return b'tree' if args[-1] == 'tree-sha' else b'blob'
            return b'pass' + b'word="prod-secret"\n'

        with mock.patch.object(scanner, 'git', side_effect=git):
            findings = scanner.scan_objects({'tree-sha': {'tree'}, 'blob-sha': {'config.py'}})
        self.assertEqual(findings, ['config.py:1: credential-assignment @ blob-sha'])

    def test_working_files_combines_tracked_and_untracked_paths(self):
        with mock.patch.object(scanner, 'git', side_effect=[b'a.py\0shared.py\0', b'b.py\0shared.py\0']):
            self.assertEqual(scanner.working_files(), ['a.py', 'b.py', 'shared.py'])

    def test_main_reports_findings_and_returns_failure(self):
        output = StringIO()
        with mock.patch.object(scanner, 'scan_working_tree', return_value=['config.py:1: private-key']), \
                mock.patch.object(scanner.sys, 'argv', ['check_push_secrets.py', '--working-tree']), \
                mock.patch.object(scanner.sys, 'stdout', output), \
                mock.patch.object(scanner.sys, 'stderr', StringIO()):
            result = scanner.main()
        self.assertEqual(result, 1)
        self.assertIn('config.py:1: private-key', output.getvalue())
        self.assertIn('1 finding(s)', output.getvalue())

    def test_main_returns_error_code_when_git_scan_fails(self):
        error = StringIO()
        with mock.patch.object(scanner, 'git', side_effect=RuntimeError('git unavailable')), \
                mock.patch.object(scanner.sys, 'argv', ['check_push_secrets.py', '--all-history']), \
                mock.patch.object(scanner.sys, 'stdout', StringIO()), \
                mock.patch.object(scanner.sys, 'stderr', error):
            result = scanner.main()
        self.assertEqual(result, 2)
        self.assertIn('Secret check could not complete', error.getvalue())
