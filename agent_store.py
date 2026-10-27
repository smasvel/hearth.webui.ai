"""PostgreSQL persistence for agent assignments and private project workspaces."""
import json
from pathlib import Path, PurePosixPath
import tarfile
import time

import database

MAX_PROJECT_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILE_BYTES = 512 * 1024 * 1024
SKIP_PARTS = {'.git', 'node_modules', '.venv', 'venv', '__pycache__'}


def init_db():
    with database.connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_tasks (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                payload TEXT NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_tasks_owner ON agent_tasks(owner_id)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_workspace_files (
                owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                workspace_id TEXT NOT NULL DEFAULT '',
                path TEXT NOT NULL,
                content BYTEA NOT NULL DEFAULT ''::bytea,
                is_dir BOOLEAN NOT NULL DEFAULT FALSE,
                executable BOOLEAN NOT NULL DEFAULT FALSE,
                updated_at DOUBLE PRECISION NOT NULL,
                PRIMARY KEY (owner_id, workspace_id, path)
            )
        """)
        conn.execute("ALTER TABLE agent_workspace_files ADD COLUMN IF NOT EXISTS workspace_id TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE agent_workspace_files ADD COLUMN IF NOT EXISTS executable BOOLEAN NOT NULL DEFAULT FALSE")
        conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'agent_workspace_files'::regclass
                      AND contype = 'p'
                      AND pg_get_constraintdef(oid) = 'PRIMARY KEY (owner_id, workspace_id, path)'
                ) THEN
                    ALTER TABLE agent_workspace_files DROP CONSTRAINT IF EXISTS agent_workspace_files_pkey;
                    ALTER TABLE agent_workspace_files ADD CONSTRAINT agent_workspace_files_pkey
                        PRIMARY KEY (owner_id, workspace_id, path);
                END IF;
            END $$
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_workspace_checkpoints (
                owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                workspace_id TEXT NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL,
                PRIMARY KEY (owner_id, workspace_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_runtime_metadata (
                task_id TEXT PRIMARY KEY REFERENCES agent_tasks(id) ON DELETE CASCADE,
                container_name TEXT UNIQUE,
                payload TEXT NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL
            )
        """)
        conn.execute("ALTER TABLE agent_runtime_metadata ADD COLUMN IF NOT EXISTS container_name TEXT UNIQUE")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_schedules (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                payload TEXT NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                next_run_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_schedules_due ON agent_schedules(enabled, next_run_at)")


def save_task(job):
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO agent_tasks (id, owner_id, payload, updated_at) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET owner_id = EXCLUDED.owner_id, payload = EXCLUDED.payload, "
            "updated_at = EXCLUDED.updated_at",
            (job['id'], job['owner'], json.dumps(job), job.get('updated_at', time.time())),
        )


def list_tasks():
    with database.connect() as conn:
        rows = conn.execute("SELECT payload FROM agent_tasks ORDER BY updated_at DESC").fetchall()
    return [json.loads(row['payload']) for row in rows]


def get_task(task_id):
    with database.connect() as conn:
        row = conn.execute("SELECT payload FROM agent_tasks WHERE id = %s", (task_id,)).fetchone()
    return json.loads(row['payload']) if row else None


def delete_task(task_id):
    with database.connect() as conn:
        with conn.transaction():
            row = conn.execute("SELECT owner_id FROM agent_tasks WHERE id = %s", (task_id,)).fetchone()
            if row:
                conn.execute("DELETE FROM agent_workspace_files WHERE owner_id = %s AND workspace_id = %s",
                             (row['owner_id'], task_id))
                conn.execute("DELETE FROM agent_workspace_checkpoints WHERE owner_id = %s AND workspace_id = %s",
                             (row['owner_id'], task_id))
                conn.execute("DELETE FROM agent_tasks WHERE id = %s", (task_id,))
                remaining = conn.execute("SELECT 1 FROM agent_tasks WHERE owner_id = %s LIMIT 1",
                                         (row['owner_id'],)).fetchone()
                if not remaining:
                    conn.execute("DELETE FROM agent_workspace_files WHERE owner_id = %s AND workspace_id = ''",
                                 (row['owner_id'],))


def delete_workspace(owner_id, workspace_id=''):
    with database.connect() as conn:
        with conn.transaction():
            conn.execute("DELETE FROM agent_workspace_files WHERE owner_id = %s AND workspace_id = %s",
                         (owner_id, workspace_id))


def assign_legacy_workspace(owner_id, task_id):
    """Move an unscoped legacy workspace to its sole assignment, when unambiguous."""
    with database.connect() as conn:
        with conn.transaction():
            count = conn.execute("SELECT COUNT(*) AS n FROM agent_tasks WHERE owner_id = %s",
                                 (owner_id,)).fetchone()['n']
            if count != 1 or conn.execute(
                "SELECT 1 FROM agent_workspace_checkpoints WHERE owner_id = %s AND workspace_id = %s",
                (owner_id, task_id),
            ).fetchone():
                return False
            conn.execute("INSERT INTO agent_workspace_checkpoints (owner_id, workspace_id, updated_at) "
                         "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                         (owner_id, task_id, time.time()))
            conn.execute("INSERT INTO agent_workspace_files "
                         "(owner_id, workspace_id, path, content, is_dir, executable, updated_at) "
                         "SELECT owner_id, %s, path, content, is_dir, executable, updated_at "
                         "FROM agent_workspace_files WHERE owner_id = %s AND workspace_id = '' "
                         "ON CONFLICT (owner_id, workspace_id, path) DO NOTHING",
                         (task_id, owner_id))
            conn.execute("DELETE FROM agent_workspace_files WHERE owner_id = %s AND workspace_id = ''",
                         (owner_id,))
            conn.execute("DELETE FROM agent_workspace_checkpoints WHERE owner_id = %s AND workspace_id = ''",
                         (owner_id,))
            return True
            conn.execute("DELETE FROM agent_workspace_checkpoints WHERE owner_id = %s AND workspace_id = %s",
                         (owner_id, workspace_id))


def user_exists(user_id):
    with database.connect() as conn:
        return conn.execute("SELECT 1 FROM users WHERE id = %s", (user_id,)).fetchone() is not None


def user_agent_access_enabled(user_id):
    with database.connect() as conn:
        row = conn.execute(
            "SELECT agentic_workflow_enabled FROM users WHERE id = %s", (user_id,)
        ).fetchone()
    return bool(row and row['agentic_workflow_enabled'])


def list_user_ids():
    with database.connect() as conn:
        return [row['id'] for row in conn.execute("SELECT id FROM users ORDER BY id").fetchall()]


def save_runtime(task_id, payload):
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO agent_runtime_metadata (task_id, container_name, payload, updated_at) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (task_id) DO UPDATE SET "
            "container_name = EXCLUDED.container_name, payload = EXCLUDED.payload, updated_at = EXCLUDED.updated_at",
            (task_id, payload.get('container'), json.dumps(payload), time.time()),
        )


def get_runtime(task_id):
    with database.connect() as conn:
        row = conn.execute("SELECT payload FROM agent_runtime_metadata WHERE task_id = %s", (task_id,)).fetchone()
    return json.loads(row['payload']) if row else None


def get_runtime_by_container(container_name):
    with database.connect() as conn:
        row = conn.execute(
            "SELECT payload FROM agent_runtime_metadata WHERE container_name = %s", (container_name,)
        ).fetchone()
    return json.loads(row['payload']) if row else None


def delete_runtime(task_id):
    with database.connect() as conn:
        conn.execute("DELETE FROM agent_runtime_metadata WHERE task_id = %s", (task_id,))


def save_schedule(schedule):
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO agent_schedules (id, owner_id, payload, enabled, next_run_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET "
            "owner_id = EXCLUDED.owner_id, payload = EXCLUDED.payload, enabled = EXCLUDED.enabled, "
            "next_run_at = EXCLUDED.next_run_at, updated_at = EXCLUDED.updated_at",
            (schedule['id'], schedule['owner'], json.dumps(schedule), schedule['enabled'],
             schedule['next_run_at'], time.time()),
        )


def list_schedules(owner_id):
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT payload FROM agent_schedules WHERE owner_id = %s ORDER BY next_run_at, id",
            (owner_id,),
        ).fetchall()
    return [json.loads(row['payload']) for row in rows]


def list_due_schedules(now, limit=10):
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT payload FROM agent_schedules WHERE enabled = TRUE AND next_run_at <= %s "
            "ORDER BY next_run_at LIMIT %s", (now, limit),
        ).fetchall()
    return [json.loads(row['payload']) for row in rows]


def delete_schedule(schedule_id, owner_id):
    with database.connect() as conn:
        return conn.execute(
            "DELETE FROM agent_schedules WHERE id = %s AND owner_id = %s RETURNING id",
            (schedule_id, owner_id),
        ).fetchone() is not None


def list_workspace_files(owner_id, workspace_id=''):
    with database.connect() as conn:
        initialized = (not workspace_id or conn.execute(
            "SELECT 1 FROM agent_workspace_checkpoints WHERE owner_id = %s AND workspace_id = %s",
            (owner_id, workspace_id),
        ).fetchone())
        lookup_id = workspace_id if initialized else ''
        rows = conn.execute(
            "SELECT path, content, is_dir, executable, updated_at FROM agent_workspace_files "
            "WHERE owner_id = %s AND workspace_id = %s ORDER BY path",
            (owner_id, lookup_id),
        ).fetchall()
    return [dict(row) for row in rows]


def save_workspace_file(owner_id, path, content=b'', is_dir=False, executable=False, workspace_id=''):
    _validate_path(path)
    if len(content) > MAX_FILE_BYTES:
        raise ValueError('A project file exceeds the 512 MiB database storage limit.')
    with database.connect() as conn:
        with conn.transaction():
            now = time.time()
            conn.execute("INSERT INTO agent_workspace_checkpoints (owner_id, workspace_id, updated_at) "
                         "VALUES (%s, %s, %s) ON CONFLICT (owner_id, workspace_id) DO UPDATE SET updated_at = EXCLUDED.updated_at",
                         (owner_id, workspace_id, now))
            conn.execute(
                "INSERT INTO agent_workspace_files (owner_id, workspace_id, path, content, is_dir, executable, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (owner_id, workspace_id, path) DO UPDATE SET "
                "content = EXCLUDED.content, is_dir = EXCLUDED.is_dir, executable = EXCLUDED.executable, "
                "updated_at = EXCLUDED.updated_at",
                (owner_id, workspace_id, path, content, is_dir, executable, now),
            )


def replace_workspace_from_tar(owner_id, source, finish=None, workspace_id=''):
    """Atomically replace a workspace from an untrusted container tar stream."""
    total = 0
    with database.connect() as conn:
        with conn.transaction():
            now = time.time()
            conn.execute("INSERT INTO agent_workspace_checkpoints (owner_id, workspace_id, updated_at) "
                         "VALUES (%s, %s, %s) ON CONFLICT (owner_id, workspace_id) DO UPDATE SET updated_at = EXCLUDED.updated_at",
                         (owner_id, workspace_id, now))
            conn.execute("DELETE FROM agent_workspace_files WHERE owner_id = %s AND workspace_id = %s",
                         (owner_id, workspace_id))
            with tarfile.open(fileobj=source, mode='r|') as archive:
                for member in archive:
                    if not member.isdir() and not member.isfile():
                        continue
                    path = member.name.rstrip('/')
                    if path in ('', '.'):
                        continue
                    _validate_path(path)
                    parts = PurePosixPath(path).parts
                    if any(part in SKIP_PARTS for part in parts):
                        continue
                    is_dir = member.isdir()
                    content = b''
                    if member.isfile():
                        total += member.size
                        if total > MAX_PROJECT_BYTES or member.size > MAX_FILE_BYTES:
                            raise ValueError('Agent project export exceeds the PostgreSQL workspace size limit.')
                        source_file = archive.extractfile(member)
                        if source_file is None:
                            continue
                        content = source_file.read(MAX_FILE_BYTES + 1)
                        if len(content) != member.size:
                            raise ValueError('Agent project export contains a truncated file.')
                    conn.execute(
                        "INSERT INTO agent_workspace_files (owner_id, workspace_id, path, content, is_dir, executable, updated_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (owner_id, workspace_id, path) DO UPDATE SET "
                        "content = EXCLUDED.content, is_dir = EXCLUDED.is_dir, executable = EXCLUDED.executable, "
                        "updated_at = EXCLUDED.updated_at",
                        (owner_id, workspace_id, path, content, is_dir, bool(member.mode & 0o111), time.time()),
                    )
            if finish:
                finish()


def _validate_path(path):
    pure = PurePosixPath(path)
    if not path or not pure.parts or pure.is_absolute() or any(part in ('', '.', '..') for part in pure.parts):
        raise ValueError('Agent project contains an unsafe relative path.')


def import_legacy_workspace(owner_id, directory, workspace_id=''):
    root = Path(directory)
    agents_root = Path.home().resolve()/'agents'
    if (not root.is_dir() or root.is_symlink()
            or not root.resolve().is_relative_to(agents_root)):
        return False
    current = agents_root
    for part in root.relative_to(agents_root).parts:
        current = current/part
        if current.is_symlink():
            return False
    total = 0
    with database.connect() as conn:
        with conn.transaction():
            if workspace_id:
                conn.execute("INSERT INTO agent_workspace_checkpoints (owner_id, workspace_id, updated_at) "
                             "VALUES (%s, %s, %s) ON CONFLICT (owner_id, workspace_id) DO NOTHING",
                             (owner_id, workspace_id, time.time()))
            for path in sorted(root.rglob('*')):
                if path.is_symlink():
                    continue
                relative = path.relative_to(root).as_posix()
                _validate_path(relative)
                if any(part in SKIP_PARTS for part in PurePosixPath(relative).parts):
                    continue
                is_dir = path.is_dir()
                content = b''
                if path.is_file():
                    if path.stat().st_size > MAX_FILE_BYTES:
                        raise ValueError(f'Legacy agent project file is too large to migrate: {relative}')
                    total += path.stat().st_size
                    if total > MAX_PROJECT_BYTES:
                        raise ValueError('Legacy agent workspace exceeds the PostgreSQL storage limit.')
                    content = path.read_bytes()
                conn.execute(
                    "INSERT INTO agent_workspace_files (owner_id, workspace_id, path, content, is_dir, executable, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (owner_id, workspace_id, path) DO NOTHING",
                    (owner_id, workspace_id, relative, content, is_dir,
                     bool(path.stat().st_mode & 0o111) if not is_dir else False, time.time()),
                )
    return True
