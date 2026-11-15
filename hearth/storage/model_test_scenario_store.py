"""PostgreSQL storage for user-authored model test scenarios."""
import time
import uuid

from hearth.storage import database


def init_db():
    with database.connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS model_test_scenarios (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                prompt TEXT NOT NULL,
                expected_answer TEXT NOT NULL,
                match_mode TEXT NOT NULL DEFAULT 'contains'
                    CHECK (match_mode IN ('contains', 'exact')),
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                created_by TEXT REFERENCES users(id) ON DELETE SET NULL,
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_model_test_scenarios_enabled ON model_test_scenarios(enabled, updated_at DESC)")


def list_scenarios(*, enabled_only=False):
    query = "SELECT * FROM model_test_scenarios"
    if enabled_only:
        query += " WHERE enabled=TRUE"
    query += " ORDER BY updated_at DESC, name ASC"
    with database.connect() as conn:
        return [dict(row) for row in conn.execute(query).fetchall()]


def get_scenario(scenario_id):
    with database.connect() as conn:
        row = conn.execute("SELECT * FROM model_test_scenarios WHERE id=%s", (scenario_id,)).fetchone()
    return dict(row) if row else None


def save_scenario(*, name, prompt, expected_answer, match_mode='contains',
                  created_by=None, scenario_id=None, enabled=True):
    now = time.time()
    scenario_id = scenario_id or uuid.uuid4().hex
    with database.connect() as conn:
        existing = conn.execute("SELECT created_at FROM model_test_scenarios WHERE id=%s",
                                (scenario_id,)).fetchone()
        if existing:
            conn.execute("""UPDATE model_test_scenarios SET name=%s, prompt=%s,
                expected_answer=%s, match_mode=%s, enabled=%s, updated_at=%s WHERE id=%s""",
                (name, prompt, expected_answer, match_mode, enabled, now, scenario_id))
        else:
            conn.execute("""INSERT INTO model_test_scenarios
                (id,name,prompt,expected_answer,match_mode,enabled,created_by,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (scenario_id, name, prompt, expected_answer, match_mode, enabled,
                 created_by, now, now))
    return get_scenario(scenario_id)


def set_enabled(scenario_id, enabled):
    with database.connect() as conn:
        row = conn.execute("""UPDATE model_test_scenarios SET enabled=%s, updated_at=%s
            WHERE id=%s RETURNING *""", (enabled, time.time(), scenario_id)).fetchone()
    return dict(row) if row else None


def delete_scenario(scenario_id):
    with database.connect() as conn:
        return conn.execute("DELETE FROM model_test_scenarios WHERE id=%s RETURNING id",
                            (scenario_id,)).fetchone() is not None
