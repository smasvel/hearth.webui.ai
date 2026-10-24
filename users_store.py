"""PostgreSQL-backed persistence for user accounts (shares the chat history database)."""

from __future__ import annotations

import secrets
import database
import time
import uuid

from werkzeug.security import check_password_hash, generate_password_hash


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    created_at DOUBLE PRECISION NOT NULL,
    caddy_token TEXT,
    agentic_workflow_enabled BOOLEAN NOT NULL DEFAULT FALSE
);
"""

VALID_ROLES = {"admin", "user"}


def _new_caddy_token() -> str:
    # Same 64-hex-char shape as the opencode/vscode/lan tokens in the Caddyfile (see
    # help_data.py) - a personal LAN credential each user can put in their own OpenCode/
    # Continue config, or use directly, so their traffic is attributable and individually
    # revocable (regenerate_caddy_token) without touching anyone else's access.
    return secrets.token_hex(32)


def _connect():
    return database.connect()


def init_db() -> None:
    with _connect() as conn:
        conn.execute(SCHEMA)
        # Idempotent upgrade for accounts created before personal Caddy tokens.
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS caddy_token TEXT")
        has_agent_permission = conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = 'users' AND column_name = 'agentic_workflow_enabled'"
        ).fetchone()
        conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
            "agentic_workflow_enabled BOOLEAN NOT NULL DEFAULT FALSE"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_agent_share_search "
            "ON users (lower(username) text_pattern_ops) "
            "WHERE agentic_workflow_enabled = TRUE"
        )
        if not has_agent_permission:
            conn.execute("UPDATE users SET agentic_workflow_enabled = (role = 'admin')")
        for row in conn.execute("SELECT id FROM users WHERE caddy_token IS NULL").fetchall():
            conn.execute(
                "UPDATE users SET caddy_token = %s WHERE id = %s", (_new_caddy_token(), row["id"])
            )


def new_id() -> str:
    return uuid.uuid4().hex


def count_users() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def count_admins() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]


def list_users(page: int = 1, per_page: int = 20) -> dict:
    page = max(page, 1)
    per_page = max(min(per_page, 100), 1)
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        rows = conn.execute(
            "SELECT id, username, role, created_at, agentic_workflow_enabled "
            "FROM users ORDER BY created_at ASC LIMIT %s OFFSET %s",
            (per_page, (page - 1) * per_page),
        ).fetchall()
    return {
        "users": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max((total + per_page - 1) // per_page, 1),
    }


def list_agent_share_targets(owner_id: str, query: str = '', user_ids: list[str] | None = None,
                             limit: int = 20) -> list[dict]:
    """Search enabled accounts or resolve a bounded set of previously selected IDs."""
    conditions = ["id <> %s", "agentic_workflow_enabled = TRUE"]
    params: list = [owner_id]
    if user_ids is not None:
        if not user_ids:
            return []
        conditions.append("id = ANY(%s)")
        params.append(user_ids)
    else:
        query = query.strip().lower()
        if len(query) < 2:
            return []
        escaped = query.replace('!', '!!').replace('%', '!%').replace('_', '!_')
        conditions.append("lower(username) LIKE %s ESCAPE '!'")
        params.append(escaped+'%')
    params.append(max(1, min(limit, 100 if user_ids is not None else 20)))
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, username FROM users WHERE "+" AND ".join(conditions)+
            " ORDER BY lower(username) LIMIT %s",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def agent_share_targets_exist(owner_id: str, user_ids: list[str]) -> bool:
    if not user_ids:
        return True
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id FROM users WHERE id <> %s AND agentic_workflow_enabled = TRUE AND id = ANY(%s)",
            (owner_id, user_ids),
        ).fetchall()
    return len(rows) == len(set(user_ids))


def get_user_by_id(user_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, username, role, created_at, agentic_workflow_enabled FROM users WHERE id = %s",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def username_exists(username: str) -> bool:
    with _connect() as conn:
        row = conn.execute("SELECT 1 FROM users WHERE username = %s", (username,)).fetchone()
        return row is not None


def _insert_user(username: str, password_hash: str, role: str, created_at: float,
                 agentic_workflow_enabled: bool | None = None) -> dict:
    user_id = new_id()
    caddy_token = _new_caddy_token()
    if agentic_workflow_enabled is None:
        agentic_workflow_enabled = role == "admin"
    with _connect() as conn:
        conn.execute(
            "INSERT INTO users (id, username, password_hash, role, created_at, caddy_token, "
            "agentic_workflow_enabled) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (user_id, username, password_hash, role, created_at, caddy_token,
             agentic_workflow_enabled),
        )
    return {
        "id": user_id, "username": username, "role": role, "created_at": created_at,
        "caddy_token": caddy_token, "agentic_workflow_enabled": agentic_workflow_enabled,
    }


def create_user(username: str, password: str, role: str = "user",
                agentic_workflow_enabled: bool | None = None) -> dict:
    if role not in VALID_ROLES:
        raise ValueError(f"invalid role: {role!r}")
    return _insert_user(username, generate_password_hash(password), role, time.time(),
                        agentic_workflow_enabled)


def create_first_admin(username: str, password: str) -> dict | None:
    """Atomically create the initial web-admin account, only while users is empty."""
    password_hash = generate_password_hash(password)
    user_id = new_id()
    created_at = time.time()
    caddy_token = _new_caddy_token()
    with _connect() as conn:
        # Serialize simultaneous first visits so only one browser can claim setup.
        conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", (74192610, 2))
        if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            return None
        conn.execute(
            "INSERT INTO users (id, username, password_hash, role, created_at, caddy_token, "
            "agentic_workflow_enabled) VALUES (%s, %s, %s, 'admin', %s, %s, TRUE)",
            (user_id, username, password_hash, created_at, caddy_token),
        )
    return {
        "id": user_id, "username": username, "role": "admin", "created_at": created_at,
        "caddy_token": caddy_token, "agentic_workflow_enabled": True,
    }


def seed_user_with_hash(username: str, password_hash: str, role: str, created_at: float | None = None,
                        agentic_workflow_enabled: bool | None = None) -> dict:
    """Bootstrap a user from an already-hashed password (used to migrate the .env admin)."""
    return _insert_user(username, password_hash, role,
                        created_at if created_at is not None else time.time(),
                        agentic_workflow_enabled)


def set_agentic_workflow_enabled(user_id: str, enabled: bool) -> bool:
    with _connect() as conn:
        result = conn.execute(
            "UPDATE users SET agentic_workflow_enabled = %s WHERE id = %s",
            (enabled, user_id),
        )
        return result.rowcount > 0


def delete_user(user_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM users WHERE id = %s", (user_id,))


def set_password(user_id: str, new_password: str) -> None:
    """Admin-initiated reset — the secure equivalent of "recovering" a forgotten
    password: no one, including an admin, can ever see the old (or any) plaintext
    password, but a new one can always be set. Same hashing as create_user, just an
    UPDATE instead of an INSERT."""
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET password_hash = %s WHERE id = %s",
            (generate_password_hash(new_password), user_id),
        )


def get_caddy_token(user_id: str) -> str | None:
    with _connect() as conn:
        row = conn.execute("SELECT caddy_token FROM users WHERE id = %s", (user_id,)).fetchone()
        return row["caddy_token"] if row else None


def regenerate_caddy_token(user_id: str) -> str | None:
    """Revokes the old token (whatever's already pasted into the Caddyfile stops working
    the next time an admin actually copies this new one in and reloads Caddy - see
    app.py's _caddy_user_snippet) and issues a new one. Returns None if the user doesn't
    exist, same not-found contract as set_password's caller checks."""
    if get_user_by_id(user_id) is None:
        return None
    token = _new_caddy_token()
    with _connect() as conn:
        conn.execute("UPDATE users SET caddy_token = %s WHERE id = %s", (token, user_id))
    return token


def verify_login(username: str, password: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, role, agentic_workflow_enabled "
            "FROM users WHERE username = %s",
            (username,),
        ).fetchone()
    if row is None or not check_password_hash(row["password_hash"], password):
        return None
    return {"id": row["id"], "username": row["username"], "role": row["role"],
            "agentic_workflow_enabled": row["agentic_workflow_enabled"]}
