"""Application settings stored in PostgreSQL and applied without restarting."""

from __future__ import annotations

import database
import secret_store
import os
from pathlib import Path
from dotenv import dotenv_values, unset_key

ROOT = Path(__file__).resolve().parent

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_LEGACY_ENV_SETTINGS = {
    'LMSTUDIO_BASE_URL': 'lmstudio_base_url', 'LMSTUDIO_API_KEY': 'lmstudio_api_key',
    'LEMONADE_BASE_URL': 'lemonade_base_url', 'LEMONADE_API_KEY': 'lemonade_api_key',
    'VLLM_BASE_URL': 'vllm_base_url', 'VLLM_API_KEY': 'vllm_api_key',
    'SGLANG_BASE_URL': 'sglang_base_url', 'SGLANG_API_KEY': 'sglang_api_key',
    'KOBOLD_BASE_URL': 'kobold_base_url', 'KOBOLD_API_KEY': 'kobold_api_key',
    'FASTFLOWLM_BASE_URL': 'fastflowlm_base_url', 'FASTFLOWLM_API_KEY': 'fastflowlm_api_key',
}
_SECRET_KEYS = {key for key in _LEGACY_ENV_SETTINGS.values() if key.endswith('_api_key')}


def _connect():
    return database.connect()


def init_db() -> None:
    with _connect() as conn:
        conn.execute(SCHEMA)


def get(key: str, default: str | None = None) -> str | None:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = %s", (key,)).fetchone()
    if row is None:
        return default
    value = row['value']
    if key in _SECRET_KEYS and value.startswith('v1:'):
        return secret_store.decrypt_setting(value)
    return value


def set(key: str, value: str | None) -> None:
    """A None or empty value clears the setting, reverting to the built-in default."""
    with _connect() as conn:
        if value:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (%s, %s) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, secret_store.encrypt_setting(value) if key in _SECRET_KEYS else value),
            )
        else:
            conn.execute("DELETE FROM settings WHERE key = %s", (key,))


def migrate_legacy_environment() -> None:
    """Move legacy backend endpoint credentials from .env into PostgreSQL once."""
    if os.environ.get('HEARTH_SKIP_LEGACY_CONFIG_MIGRATION') == '1':
        return
    env_file = ROOT / '.env'
    with _connect() as conn:
        for env_name, setting_name in _LEGACY_ENV_SETTINGS.items():
            value = os.environ.get(env_name)
            if value:
                stored = value
                if setting_name in _SECRET_KEYS:
                    stored = secret_store.encrypt_setting(value)
                conn.execute(
                    'INSERT INTO settings (key, value) VALUES (%s, %s) '
                    'ON CONFLICT(key) DO NOTHING', (setting_name, stored),
                )
        # Upgrade API keys written by older versions in plaintext.
        for key in _SECRET_KEYS:
            row = conn.execute('SELECT value FROM settings WHERE key = %s', (key,)).fetchone()
            if row and not row['value'].startswith('v1:'):
                conn.execute('UPDATE settings SET value = %s WHERE key = %s',
                             (secret_store.encrypt_setting(row['value']), key))
    # Remove file/environment copies only after the database transaction committed.
    for env_name in _LEGACY_ENV_SETTINGS:
        os.environ.pop(env_name, None)
        if env_file.exists() and env_name in dotenv_values(env_file):
            unset_key(str(env_file), env_name)
