"""Runtime-configurable backend settings (base URLs, API keys) — lets an admin change
where LM Studio/Lemonade are reached from the Settings panel instead of only via .env
+ a service restart. Shares chat_store's PostgreSQL database like the other *_store modules.

Values set here override the .env-derived defaults immediately (lmstudio_client.py /
lemonade_client.py re-check this store on every call, they don't cache a value at
import time) and persist across restarts, unlike a plain in-memory override would.
"""

from __future__ import annotations

import database


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _connect():
    return database.connect()


def init_db() -> None:
    with _connect() as conn:
        conn.execute(SCHEMA)


def get(key: str, default: str | None = None) -> str | None:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = %s", (key,)).fetchone()
    return row["value"] if row is not None else default


def set(key: str, value: str | None) -> None:
    """A None or empty value clears the override, reverting to the .env default."""
    with _connect() as conn:
        if value:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (%s, %s) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        else:
            conn.execute("DELETE FROM settings WHERE key = %s", (key,))
