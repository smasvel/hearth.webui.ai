import unittest

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
