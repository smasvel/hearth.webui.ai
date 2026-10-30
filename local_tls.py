"""Short-lived local TLS certificates shared by setup and application startup."""
import datetime
import ipaddress
import os
from pathlib import Path
import socket
import subprocess

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

VALIDITY = datetime.timedelta(days=90)
RENEW_BEFORE = datetime.timedelta(days=7)
NAME = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'ai-webui.local')])


def local_ipv4_addresses():
    try:
        out = subprocess.run(['hostname', '-I'], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ''
    if not out.strip():
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(('192.0.2.1', 80))
                out = probe.getsockname()[0]
        except OSError:
            return []
    addresses = []
    for token in out.split():
        try:
            address = ipaddress.ip_address(token)
        except ValueError:
            continue
        if address.version == 4 and not address.is_loopback:
            addresses.append(str(address))
    return addresses


def ensure_certificate(directory, addresses=None):
    """Create/renew Hearth's local certificate; retain externally issued/custom certificates."""
    directory = Path(directory)
    cert_path, key_path = directory / 'cert.pem', directory / 'key.pem'
    now = datetime.datetime.now(datetime.timezone.utc)
    if cert_path.exists() != key_path.exists():
        raise RuntimeError('Incomplete TLS certificate pair: restore both certs/cert.pem and certs/key.pem before starting Hearth.')
    key = None
    if cert_path.exists():
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        if cert.subject != NAME or cert.issuer != NAME:
            return False
        cert.verify_directly_issued_by(cert)
        lifetime = cert.not_valid_after_utc - cert.not_valid_before_utc
        if (lifetime <= VALIDITY and cert.not_valid_before_utc <= now
                and cert.not_valid_after_utc > now + RENEW_BEFORE):
            return False
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    directory.mkdir(parents=True, exist_ok=True)
    if key is None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        # A private key must never inherit a permissive umask.
        descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    os.chmod(key_path, 0o600)
    entries = [x509.DNSName('localhost'), x509.DNSName('ai-webui.local'),
               x509.IPAddress(ipaddress.ip_address('127.0.0.1')),
               x509.IPAddress(ipaddress.ip_address('::1'))]
    for address in sorted(set(local_ipv4_addresses() if addresses is None else addresses)):
        entries.append(x509.IPAddress(ipaddress.ip_address(address)))
    # Backdate by five minutes for clock skew; keep the total validity at 90 days.
    start = now - datetime.timedelta(minutes=5)
    cert = (x509.CertificateBuilder().subject_name(NAME).issuer_name(NAME)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(start).not_valid_after(start + VALIDITY)
            .add_extension(x509.SubjectAlternativeName(entries), critical=False)
            .sign(key, hashes.SHA256()))
    temporary = cert_path.with_suffix('.pem.new')
    temporary.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    temporary.replace(cert_path)
    print(f'Created/renewed 90-day local TLS certificate at {cert_path}', flush=True)
    return True
