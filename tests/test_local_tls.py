import datetime
from pathlib import Path
import tempfile
import unittest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import local_tls


class LocalTLS(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def certificate(self):
        return x509.load_pem_x509_certificate((self.root / 'cert.pem').read_bytes())

    def old_pair(self, days=825, name=local_tls.NAME, expired=False):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.datetime.now(datetime.timezone.utc)
        start = now - datetime.timedelta(days=days + 1 if expired else 1)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(start).not_valid_after(start + datetime.timedelta(days=days))
                .sign(key, hashes.SHA256()))
        (self.root / 'cert.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (self.root / 'key.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM,
             serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))

    def test_new_certificate_short_validity_addresses_and_private_key(self):
        self.assertTrue(local_tls.ensure_certificate(self.root, addresses=['192.168.1.20']))
        cert = self.certificate()
        self.assertEqual(cert.not_valid_after_utc - cert.not_valid_before_utc, local_tls.VALIDITY)
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        self.assertIn('localhost', names.get_values_for_type(x509.DNSName))
        self.assertIn('192.168.1.20', [str(ip) for ip in names.get_values_for_type(x509.IPAddress)])
        self.assertEqual((self.root / 'key.pem').stat().st_mode & 0o777, 0o600)

    def test_legacy_long_certificate_renews_and_keeps_key(self):
        self.old_pair()
        key = (self.root / 'key.pem').read_bytes()
        self.assertTrue(local_tls.ensure_certificate(self.root, addresses=[]))
        self.assertEqual((self.root / 'key.pem').read_bytes(), key)
        self.assertEqual(self.certificate().not_valid_after_utc - self.certificate().not_valid_before_utc, local_tls.VALIDITY)

    def test_current_certificate_is_preserved(self):
        local_tls.ensure_certificate(self.root, addresses=[])
        original = (self.root / 'cert.pem').read_bytes()
        self.assertFalse(local_tls.ensure_certificate(self.root, addresses=[]))
        self.assertEqual((self.root / 'cert.pem').read_bytes(), original)

    def test_expired_certificate_renews(self):
        self.old_pair(days=90, expired=True)
        self.assertTrue(local_tls.ensure_certificate(self.root, addresses=[]))
        self.assertGreater(self.certificate().not_valid_after_utc, datetime.datetime.now(datetime.timezone.utc))

    def test_custom_certificate_is_preserved(self):
        self.old_pair(name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'custom.example')]))
        original = (self.root / 'cert.pem').read_bytes()
        self.assertFalse(local_tls.ensure_certificate(self.root, addresses=[]))
        self.assertEqual((self.root / 'cert.pem').read_bytes(), original)

    def test_incomplete_pair_does_not_overwrite_existing_file(self):
        (self.root / 'key.pem').write_text('preserve')
        with self.assertRaisesRegex(RuntimeError, 'Incomplete TLS'):
            local_tls.ensure_certificate(self.root, addresses=[])
        self.assertEqual((self.root / 'key.pem').read_text(), 'preserve')

    def test_near_expiry_certificate_renews(self):
        self.old_pair(days=5)
        self.assertTrue(local_tls.ensure_certificate(self.root, addresses=[]))
        self.assertGreater(self.certificate().not_valid_after_utc,
                           datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=80))
