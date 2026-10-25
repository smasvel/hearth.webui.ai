"""PostgreSQL storage for Hearth-managed model registries, profiles, and test reports."""
import json
import os
import time

import database


def init_db():
    with database.connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS model_registry (
                backend TEXT NOT NULL,
                model_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL,
                PRIMARY KEY (backend, model_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS model_test_runs (
                id TEXT PRIMARY KEY,
                started_at DOUBLE PRECISION NOT NULL,
                payload TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_model_test_runs_started ON model_test_runs(started_at DESC)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS model_resource_warnings (
                backend TEXT NOT NULL,
                model_id TEXT NOT NULL,
                reviewed_at TEXT NOT NULL,
                scope TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (backend, model_id)
            )
        """)
        conn.execute("CREATE TABLE IF NOT EXISTS model_metadata_migrations (key TEXT PRIMARY KEY, migrated_at DOUBLE PRECISION NOT NULL)")


def registry(backend):
    with database.connect() as conn:
        rows = conn.execute("SELECT model_id, payload FROM model_registry WHERE backend = %s", (backend,)).fetchall()
    result = {}
    for row in rows:
        try:
            result[row['model_id']] = json.loads(row['payload'])
        except (TypeError, ValueError):
            continue
    return result


def save_registry(backend, values):
    now = time.time()
    with database.connect() as conn:
        conn.execute("DELETE FROM model_registry WHERE backend = %s", (backend,))
        for model_id, payload in values.items():
            conn.execute(
                "INSERT INTO model_registry(backend, model_id, payload, updated_at) VALUES (%s, %s, %s, %s)",
                (backend, str(model_id), json.dumps(payload), now),
            )


def migrate_registry_file(backend, path):
    """Import a legacy registry once, then remove its file after the DB commit."""
    if os.environ.get('HEARTH_SKIP_LEGACY_CONFIG_MIGRATION') == '1':
        return
    migration_key = f'model-registry:{backend}'
    with database.connect() as conn:
        done = conn.execute("SELECT 1 FROM model_metadata_migrations WHERE key = %s", (migration_key,)).fetchone()
    if done:
        return
    legacy = {}
    if path.exists():
        try:
            value = json.loads(path.read_text())
            if isinstance(value, dict):
                legacy = value
        except (OSError, ValueError):
            legacy = {}
    if legacy:
        current = registry(backend)
        merged = {**legacy, **current}
        save_registry(backend, merged)
    with database.connect() as conn:
        conn.execute("INSERT INTO model_metadata_migrations(key, migrated_at) VALUES (%s, %s) ON CONFLICT(key) DO NOTHING",
                     (migration_key, time.time()))
    if path.exists():
        path.unlink()
        try:
            path.parent.rmdir()
        except OSError:
            pass


def save_test_run(job):
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO model_test_runs(id, started_at, payload) VALUES (%s, %s, %s) "
            "ON CONFLICT(id) DO UPDATE SET started_at = EXCLUDED.started_at, payload = EXCLUDED.payload",
            (job['id'], job['started_at'], json.dumps(job)),
        )
        conn.execute("""DELETE FROM model_test_runs WHERE id IN
                       (SELECT id FROM model_test_runs ORDER BY started_at DESC OFFSET 30)""")


def get_test_run(job_id):
    with database.connect() as conn:
        row = conn.execute("SELECT payload FROM model_test_runs WHERE id = %s", (job_id,)).fetchone()
    return json.loads(row['payload']) if row else None


def list_test_runs():
    with database.connect() as conn:
        rows = conn.execute("SELECT payload FROM model_test_runs ORDER BY started_at DESC LIMIT 30").fetchall()
    return [json.loads(row['payload']) for row in rows]


def delete_test_run(job_id):
    with database.connect() as conn:
        return conn.execute("DELETE FROM model_test_runs WHERE id = %s RETURNING id", (job_id,)).fetchone() is not None


def import_test_runs(path):
    """Migrate legacy JSON reports to PostgreSQL and remove the runtime report folder."""
    if os.environ.get('HEARTH_SKIP_LEGACY_CONFIG_MIGRATION') == '1':
        return
    migration_key = 'model-test-runs'
    with database.connect() as conn:
        done = conn.execute("SELECT 1 FROM model_metadata_migrations WHERE key = %s", (migration_key,)).fetchone()
    if done:
        return
    if path.is_dir():
        for report in sorted(path.glob('*.json'), key=lambda item: item.stat().st_mtime, reverse=True)[:30]:
            try:
                job = json.loads(report.read_text())
                if isinstance(job, dict) and job.get('id') and isinstance(job.get('started_at'), (int, float)):
                    save_test_run(job)
            except (OSError, ValueError, TypeError):
                continue
        for report in path.iterdir():
            if report.is_symlink() or report.is_file():
                report.unlink(missing_ok=True)
        try:
            path.rmdir()
        except OSError:
            pass
    with database.connect() as conn:
        conn.execute("INSERT INTO model_metadata_migrations(key, migrated_at) VALUES (%s, %s) ON CONFLICT(key) DO NOTHING",
                     (migration_key, time.time()))


def seed_resource_warnings(catalog):
    with database.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM model_resource_warnings").fetchone()[0]
        if count:
            return
        for item in catalog.get('models', []):
            backend, model_id = item.get('backend'), item.get('model')
            if not backend or not model_id:
                continue
            conn.execute(
                "INSERT INTO model_resource_warnings(backend, model_id, reviewed_at, scope, payload) VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (backend, model_id, catalog.get('reviewed_at', ''), catalog.get('scope', ''), json.dumps(item)),
            )


def resource_warnings():
    with database.connect() as conn:
        rows = conn.execute("SELECT backend, model_id, reviewed_at, scope, payload FROM model_resource_warnings").fetchall()
    result = {}
    for row in rows:
        try:
            item = json.loads(row['payload'])
            item['reviewed_at'] = row['reviewed_at']
            item['scope'] = row['scope']
            result[(row['backend'], row['model_id'].casefold())] = item
        except (TypeError, ValueError):
            continue
    return result
