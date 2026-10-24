"""Encrypted persistence for secrets that must be recovered by the application."""
import base64
import binascii
import logging
import os
import secrets
import time
from urllib.parse import unquote, urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

import database

_NAME = 'flask_session_key'
_AAD = b'hearth:flask-session-key:v1'


def _encryption_key() -> bytes:
    """Derive an encryption key from the existing PostgreSQL connection secret."""
    url = os.environ.get('DATABASE_URL', '')
    password = urlsplit(url).password
    if not password:
        raise RuntimeError(
            'Encrypted app secrets require a password in DATABASE_URL; configure the PostgreSQL credential.'
        )
    return HKDF(
        algorithm=hashes.SHA256(), length=32,
        salt=b'hearth-postgres-secret-store-v1',
        info=b'hearth/flask-session-key',
    ).derive(unquote(password).encode())


def _encrypt(value: str, key: bytes, aad: bytes = _AAD) -> str:
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(nonce, value.encode(), aad)
    return 'v1:' + base64.urlsafe_b64encode(nonce + ciphertext).decode()


def _decrypt(value: str, key: bytes, aad: bytes = _AAD) -> str:
    version, encoded = value.split(':', 1)
    if version != 'v1':
        raise ValueError('Unsupported encrypted secret version.')
    packed = base64.urlsafe_b64decode(encoded.encode())
    return AESGCM(key).decrypt(packed[:12], packed[12:], aad).decode()


def encrypt_setting(value: str) -> str:
    return _encrypt(value, _encryption_key(), b'hearth:setting-secret:v1')


def decrypt_setting(value: str) -> str:
    return _decrypt(value, _encryption_key(), b'hearth:setting-secret:v1')


def get_or_create_flask_key(legacy_key: str | None = None) -> str:
    """Return the stable session key, migrating a legacy environment value once."""
    key = _encryption_key()
    with database.connect() as conn:
        conn.execute(
            'CREATE TABLE IF NOT EXISTS app_secrets ('
            'name TEXT PRIMARY KEY, encrypted_value TEXT NOT NULL, updated_at DOUBLE PRECISION NOT NULL)'
        )
        conn.execute('SELECT pg_advisory_xact_lock(%s, %s)', (74192610, 3))
        row = conn.execute('SELECT encrypted_value FROM app_secrets WHERE name = %s', (_NAME,)).fetchone()
        if row:
            try:
                return _decrypt(row['encrypted_value'], key)
            except (InvalidTag, ValueError, UnicodeDecodeError, binascii.Error):
                # A rotated database password changes the derived key. Replace the
                # unreadable secret; existing browser sessions will need to sign in again.
                logging.warning('Could not decrypt the stored Flask session key; rotating it and invalidating existing sessions.')
        value = legacy_key or secrets.token_urlsafe(48)
        encrypted = _encrypt(value, key)
        conn.execute(
            'INSERT INTO app_secrets (name, encrypted_value, updated_at) VALUES (%s, %s, %s) '
            'ON CONFLICT (name) DO UPDATE SET encrypted_value = EXCLUDED.encrypted_value, '
            'updated_at = EXCLUDED.updated_at',
            (_NAME, encrypted, time.time()),
        )
        return value
