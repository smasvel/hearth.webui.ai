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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS model_load_successes (
                backend TEXT NOT NULL,
                model_id TEXT NOT NULL,
                loaded_at DOUBLE PRECISION NOT NULL,
                PRIMARY KEY (backend, model_id)
            )
        """)
        conn.execute("CREATE TABLE IF NOT EXISTS model_metadata_migrations (key TEXT PRIMARY KEY, migrated_at DOUBLE PRECISION NOT NULL)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS service_status (
                service TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL,
                detail TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS restart_jobs (
                id TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at DOUBLE PRECISION NOT NULL,
                finished_at DOUBLE PRECISION,
                progress INTEGER NOT NULL DEFAULT 0,
                total INTEGER NOT NULL DEFAULT 0,
                detail TEXT,
                error TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS model_load_events (
                id SERIAL PRIMARY KEY,
                backend TEXT NOT NULL,
                model_id TEXT NOT NULL,
                event TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                recorded_at DOUBLE PRECISION NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS model_configured_context (
                backend TEXT NOT NULL,
                model_id TEXT NOT NULL,
                context_tokens INTEGER NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL
            )
        """)
        # Model ids are matched case-insensitively (backends report the same model with
        # different casing between catalog and health endpoints), so this expression index
        # is the only unique constraint and therefore the ON CONFLICT arbiter used by
        # set_model_configured_context(). A second unique constraint (such as a plain
        # (backend, model_id) primary key) is not allowed here: a concurrent upsert whose
        # arbiter is one index can still fail on the other with a duplicate-key error.
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_model_configured_context_id
            ON model_configured_context (backend, lower(model_id))
        """)
        conn.execute("""
            ALTER TABLE model_configured_context
            DROP CONSTRAINT IF EXISTS model_configured_context_pkey
        """)


def downloaded_backend(model_id):
    """Return the backend a model was downloaded under, or None if unknown."""
    with database.connect() as conn:
        row = conn.execute(
            "SELECT backend FROM model_registry WHERE model_id = %s AND payload LIKE %s",
            (model_id, '%downloaded%'),
        ).fetchone()
    return row["backend"] if row else None


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


def mark_model_downloaded(backend, model_id):
    """Record which backend a model was downloaded under, so the backend can be
    identified even when the live catalog is unreachable (e.g. Lemonade is down)."""
    payload = json.dumps({"downloaded": True})
    now = time.time()
    with database.connect() as conn:
        conn.execute(
            """INSERT INTO model_registry (backend, model_id, payload, updated_at)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (backend, model_id) DO UPDATE SET payload = EXCLUDED.payload, updated_at = EXCLUDED.updated_at""",
            (backend, model_id, payload, now),
        )


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


def get_test_run(job_id):
    with database.connect() as conn:
        row = conn.execute("SELECT payload FROM model_test_runs WHERE id = %s", (job_id,)).fetchone()
    return json.loads(row['payload']) if row else None


def list_test_runs(limit=None):
    with database.connect() as conn:
        if limit is None:
            rows = conn.execute("SELECT payload FROM model_test_runs ORDER BY started_at DESC").fetchall()
        else:
            rows = conn.execute("SELECT payload FROM model_test_runs ORDER BY started_at DESC LIMIT %s",
                                (max(0, int(limit)),)).fetchall()
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
        for report in sorted(path.glob('*.json'), key=lambda item: item.stat().st_mtime, reverse=True):
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


def mark_model_load_success(backend, model_id):
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO model_load_successes(backend, model_id, loaded_at) VALUES (%s, %s, %s) "
            "ON CONFLICT(backend, model_id) DO UPDATE SET loaded_at = EXCLUDED.loaded_at",
            (backend, str(model_id), time.time()),
        )


def set_model_configured_context(backend, model_id, context_tokens):
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO model_configured_context (backend, model_id, context_tokens, updated_at) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (backend, lower(model_id)) "
            "DO UPDATE SET context_tokens = excluded.context_tokens, updated_at = excluded.updated_at",
            (backend, str(model_id), int(context_tokens), time.time()),
        )


def get_model_configured_context(backend, model_id):
    with database.connect() as conn:
        row = conn.execute(
            "SELECT context_tokens FROM model_configured_context WHERE backend = %s AND lower(model_id) = lower(%s)",
            (backend, str(model_id)),
        ).fetchone()
    return row['context_tokens'] if row and row['context_tokens'] else None


def clear_model_configured_context(backend, model_id):
    with database.connect() as conn:
        conn.execute(
            "DELETE FROM model_configured_context WHERE backend = %s AND lower(model_id) = lower(%s)",
            (backend, str(model_id)),
        )


def clear_model_load_success(backend, model_id):
    with database.connect() as conn:
        conn.execute(
            "DELETE FROM model_load_successes WHERE backend = %s AND lower(model_id) = lower(%s)",
            (backend, str(model_id)),
        )


def successful_model_loads():
    with database.connect() as conn:
        rows = conn.execute("SELECT backend, model_id FROM model_load_successes").fetchall()
    return {(row['backend'], str(row['model_id']).casefold()) for row in rows}


def record_service_status(service, status, detail=None):
    now = time.time()
    with database.connect() as conn:
        conn.execute(
            """INSERT INTO service_status (service, status, updated_at, detail)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (service) DO UPDATE SET status = EXCLUDED.status, updated_at = EXCLUDED.updated_at, detail = EXCLUDED.detail""",
            (service, status, now, detail),
        )


def get_service_status(service=None):
    with database.connect() as conn:
        if service:
            row = conn.execute("SELECT * FROM service_status WHERE service = %s", (service,)).fetchone()
            return dict(row) if row else None
        rows = conn.execute("SELECT * FROM service_status ORDER BY service").fetchall()
        return [dict(r) for r in rows]


def create_restart_job(job_id, scope, total):
    now = time.time()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO restart_jobs (id, scope, status, started_at, progress, total) VALUES (%s, %s, %s, %s, %s, %s)",
            (job_id, scope, "running", now, 0, total),
        )


def update_restart_job(job_id, progress=None, status=None, detail=None, error=None):
    now = time.time()
    with database.connect() as conn:
        parts = []
        params = []
        if progress is not None:
            parts.append("progress = %s")
            params.append(progress)
        if status is not None:
            parts.append("status = %s")
            params.append(status)
            if status in ("done", "error"):
                parts.append("finished_at = %s")
                params.append(now)
        if detail is not None:
            parts.append("detail = %s")
            params.append(detail)
        if error is not None:
            parts.append("error = %s")
            params.append(error)
        if parts:
            params.append(job_id)
            conn.execute("UPDATE restart_jobs SET " + ", ".join(parts) + " WHERE id = %s", params)


def get_restart_job(job_id):
    with database.connect() as conn:
        row = conn.execute("SELECT * FROM restart_jobs WHERE id = %s", (job_id,)).fetchone()
        return dict(row) if row else None


def list_restart_jobs(limit=20):
    with database.connect() as conn:
        rows = conn.execute("SELECT * FROM restart_jobs ORDER BY started_at DESC LIMIT %s", (limit,)).fetchall()
        return [dict(r) for r in rows]


def record_model_load_event(backend, model_id, event, status, error=None):
    now = time.time()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO model_load_events (backend, model_id, event, status, error, recorded_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (backend, model_id, event, status, error, now),
        )
