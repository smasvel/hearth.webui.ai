"""PostgreSQL persistence for agent assignments and private project workspaces."""
import json
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import time
import uuid
import zipfile

import numpy as np

import database
import secret_store

MAX_PROJECT_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_PROJECT_ARCHIVE_BYTES = 16 * 1024 * 1024 * 1024
MAX_PROJECT_ARCHIVE_EXPANDED_BYTES = 64 * 1024 * 1024 * 1024
MAX_PROJECT_ARCHIVE_ENTRIES = 100_000
MAX_PROJECT_ARCHIVE_LARGE_FILES = 10_000
SKIP_PARTS = {'.git', 'node_modules', '.venv', 'venv', '__pycache__'}
MAX_SKILL_BODY_CHARS = 200_000


def skill_folder(name):
    """Folder identifier OpenCode uses for a skill inside its skill directory."""
    folder = re.sub(r'[^a-z0-9]+', '-', str(name or '').casefold()).strip('-')
    if not folder:
        raise ValueError('A skill name needs at least one letter or number.')
    # Avoid turning distinct long names into the same ID after truncation.
    return folder[:64].strip('-')


def compose_skill_markdown(skill):
    """Render a stored skill as the SKILL.md file installed into agent containers."""
    # OpenCode's skill tool resolves by the directory ID, which is derived from
    # the saved library name. Imported frontmatter may contain another `name`
    # (for example `imported_project_review_checklist`), so normalize it to the installed ID.
    name = skill_folder(skill.get('name'))
    description = str(skill.get('description') or '').strip().replace('"', '\\"')
    body = str(skill.get('body') or '').strip()
    if body.startswith('---'):
        lines = body.splitlines()
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == '---'), None)
        if end is not None:
            metadata = []
            replaced = False
            for line in lines[1:end]:
                key, sep, _value = line.partition(':')
                if sep and key.strip().casefold() == 'name':
                    if not replaced:
                        metadata.append(f'name: "{name}"')
                        replaced = True
                else:
                    metadata.append(line)
            if not replaced:
                metadata.insert(0, f'name: "{name}"')
            return '\n'.join(['---', *metadata, '---', *lines[end + 1:]]) + '\n'
    return (f'---\nname: "{name}"\ndescription: "{description}"\n---\n\n'
            + body + '\n')


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
            CREATE TABLE IF NOT EXISTS agent_project_archives (
                task_id TEXT PRIMARY KEY REFERENCES agent_tasks(id) ON DELETE CASCADE,
                owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                archive_oid OID NOT NULL,
                filename TEXT NOT NULL,
                archive_size BIGINT NOT NULL,
                expanded_size BIGINT NOT NULL,
                large_paths TEXT[] NOT NULL DEFAULT '{}',
                checkpointed_at DOUBLE PRECISION,
                updated_at DOUBLE PRECISION NOT NULL
            )
        """)
        conn.execute("""
            CREATE OR REPLACE FUNCTION hearth_unlink_agent_project_archive() RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    PERFORM lo_unlink(OLD.archive_oid);
                    RETURN OLD;
                END IF;
                IF OLD.archive_oid IS DISTINCT FROM NEW.archive_oid THEN
                    PERFORM lo_unlink(OLD.archive_oid);
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """)
        conn.execute("ALTER TABLE agent_project_archives ADD COLUMN IF NOT EXISTS large_paths TEXT[] NOT NULL DEFAULT '{}'")
        conn.execute("ALTER TABLE agent_project_archives ADD COLUMN IF NOT EXISTS checkpointed_at DOUBLE PRECISION")
        conn.execute("DROP TRIGGER IF EXISTS agent_project_archive_unlink ON agent_project_archives")
        conn.execute("""
            CREATE TRIGGER agent_project_archive_unlink
            BEFORE UPDATE OF archive_oid OR DELETE ON agent_project_archives
            FOR EACH ROW EXECUTE FUNCTION hearth_unlink_agent_project_archive()
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_research_credentials (
                task_id TEXT PRIMARY KEY REFERENCES agent_tasks(id) ON DELETE CASCADE,
                encrypted_value TEXT NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_project_memory (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                source_task_id TEXT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
                content TEXT NOT NULL,
                embedding BYTEA,
                updated_at DOUBLE PRECISION NOT NULL,
                UNIQUE(source_task_id, content)
            )
        """)
        conn.execute("ALTER TABLE agent_project_memory ALTER COLUMN embedding DROP NOT NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_project_memory_owner ON agent_project_memory(owner_id)")
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_skills (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL,
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL,
                UNIQUE (owner_id, name)
            )
        """)


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


def validate_project_archive(stream):
    """Validate an uploaded ZIP without extracting it on the Hearth host."""
    try:
        stream.seek(0, 2)
        archive_size = stream.tell()
        stream.seek(0)
        if archive_size <= 0:
            raise ValueError('The project ZIP is empty.')
        if archive_size > MAX_PROJECT_ARCHIVE_BYTES:
            raise ValueError('Project ZIP uploads are limited to 16 GiB.')
        with zipfile.ZipFile(stream) as archive:
            entries = archive.infolist()
            if not entries:
                raise ValueError('The project ZIP does not contain any files.')
            if len(entries) > MAX_PROJECT_ARCHIVE_ENTRIES:
                raise ValueError('The project ZIP contains too many entries.')
            expanded_size = 0
            checkpoint_size = 0
            large_paths = []
            file_count = 0
            seen = set()
            for entry in entries:
                normalized = entry.filename.replace('\\', '/')
                while normalized.startswith('./'):
                    normalized = normalized[2:]
                if normalized in ('', '.'):
                    continue
                path = PurePosixPath(normalized.rstrip('/'))
                if (path.is_absolute() or not path.parts or
                        any(part in ('', '.', '..') for part in path.parts) or
                        re.match(r'^[A-Za-z]:', normalized) or '\x00' in normalized or
                        '\n' in normalized or '\r' in normalized or len(normalized) > 4096):
                    raise ValueError('The project ZIP contains an unsafe path.')
                normalized_path = path.as_posix()
                if normalized_path in seen:
                    raise ValueError('The project ZIP contains duplicate paths.')
                seen.add(normalized_path)
                mode = (entry.external_attr >> 16) & 0xFFFF
                kind = stat.S_IFMT(mode)
                if kind == stat.S_IFLNK or kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ValueError('The project ZIP may contain only regular files and folders.')
                if entry.flag_bits & 0x1:
                    raise ValueError('Encrypted project ZIP files are not supported.')
                if entry.is_dir() or kind == stat.S_IFDIR:
                    continue
                if any(part in SKIP_PARTS for part in path.parts):
                    continue
                file_count += 1
                expanded_size += entry.file_size
                if expanded_size > MAX_PROJECT_ARCHIVE_EXPANDED_BYTES:
                    raise ValueError('Expanded project ZIP contents are limited to 64 GiB.')
                if entry.file_size > MAX_FILE_BYTES:
                    large_paths.append(normalized_path)
                    if len(large_paths) > MAX_PROJECT_ARCHIVE_LARGE_FILES:
                        raise ValueError('Project ZIPs can contain at most 10,000 archive-backed large files.')
                else:
                    checkpoint_size += entry.file_size
                    if checkpoint_size > MAX_PROJECT_BYTES:
                        raise ValueError('Project files up to 512 MiB each are limited to 2 GiB total.')
                if entry.file_size and entry.compress_size == 0:
                    raise ValueError('The project ZIP contains an invalid compressed file entry.')
            if not file_count:
                raise ValueError('The project ZIP does not contain any files.')
        stream.seek(0)
        return {'archive_size': archive_size, 'expanded_size': expanded_size,
                'large_paths': large_paths}
    except zipfile.BadZipFile as exc:
        stream.seek(0)
        raise ValueError('Choose a valid, unencrypted ZIP project archive.') from exc


def save_project_archive(task_id, owner_id, filename, stream, archive_info):
    """Store a project ZIP in PostgreSQL's 64-bit large-object store."""
    archive_size = int(archive_info['archive_size'])
    if archive_size <= 0 or archive_size > MAX_PROJECT_ARCHIVE_BYTES:
        raise ValueError('Project ZIP uploads are limited to 16 GiB.')
    stream.seek(0)
    with database.connect() as conn:
        with conn.transaction():
            archive_oid = conn.execute('SELECT lo_create(0) AS oid').fetchone()['oid']
            offset = 0
            while offset < archive_size:
                chunk = stream.read(min(8 * 1024 * 1024, archive_size - offset))
                if not chunk:
                    raise ValueError('The project ZIP upload ended before all bytes were stored.')
                conn.execute('SELECT lo_put(%s, %s, %s)', (archive_oid, offset, chunk))
                offset += len(chunk)
            if stream.read(1):
                raise ValueError('The project ZIP changed while it was being stored.')
            conn.execute(
                "INSERT INTO agent_project_archives "
                "(task_id, owner_id, archive_oid, filename, archive_size, expanded_size, large_paths, checkpointed_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, NULL, %s) "
                "ON CONFLICT (task_id) DO UPDATE SET owner_id = EXCLUDED.owner_id, "
                "archive_oid = EXCLUDED.archive_oid, filename = EXCLUDED.filename, "
                "archive_size = EXCLUDED.archive_size, expanded_size = EXCLUDED.expanded_size, "
                "large_paths = EXCLUDED.large_paths, checkpointed_at = NULL, "
                "updated_at = EXCLUDED.updated_at",
                (task_id, owner_id, archive_oid, filename, archive_size,
                 int(archive_info['expanded_size']), archive_info['large_paths'], time.time()),
            )


def get_project_archive(task_id):
    with database.connect() as conn:
        return conn.execute(
            "SELECT archive_oid, filename, archive_size, expanded_size, large_paths, checkpointed_at "
            "FROM agent_project_archives WHERE task_id = %s", (task_id,)
        ).fetchone()


def get_project_archive_large_paths(task_id):
    archive = get_project_archive(task_id)
    return list(archive['large_paths'] or []) if archive else []


def mark_project_archive_checkpointed(task_id, archive_oid):
    with database.connect() as conn:
        conn.execute(
            "UPDATE agent_project_archives SET checkpointed_at = %s "
            "WHERE task_id = %s AND archive_oid = %s",
            (time.time(), task_id, archive_oid),
        )


def has_workspace_checkpoint(owner_id, workspace_id):
    with database.connect() as conn:
        return conn.execute(
            "SELECT 1 FROM agent_workspace_checkpoints WHERE owner_id = %s AND workspace_id = %s",
            (owner_id, workspace_id),
        ).fetchone() is not None


def stream_project_archive(task_id, sink):
    """Stream the saved large object directly into a binary sink in bounded chunks."""
    with database.connect() as conn:
        row = conn.execute(
            "SELECT archive_oid, archive_size FROM agent_project_archives WHERE task_id = %s",
            (task_id,),
        ).fetchone()
        if not row:
            return False
        offset = 0
        while offset < row['archive_size']:
            chunk = conn.execute(
                'SELECT lo_get(%s, %s, %s) AS chunk',
                (row['archive_oid'], offset, min(8 * 1024 * 1024, row['archive_size'] - offset)),
            ).fetchone()['chunk']
            if not chunk:
                raise RuntimeError('The saved project ZIP large object is truncated.')
            sink.write(bytes(chunk))
            offset += len(chunk)
        return True


def save_research_credentials(task_id, credentials):
    """Store provider credentials separately from task payloads and API responses."""
    if credentials:
        encrypted = secret_store.encrypt_setting(json.dumps(credentials, sort_keys=True))
        with database.connect() as conn:
            conn.execute(
                "INSERT INTO agent_research_credentials (task_id, encrypted_value, updated_at) "
                "VALUES (%s, %s, %s) ON CONFLICT (task_id) DO UPDATE SET "
                "encrypted_value = EXCLUDED.encrypted_value, updated_at = EXCLUDED.updated_at",
                (task_id, encrypted, time.time()),
            )
    else:
        with database.connect() as conn:
            conn.execute("DELETE FROM agent_research_credentials WHERE task_id = %s", (task_id,))


def get_research_credentials(task_id):
    with database.connect() as conn:
        row = conn.execute(
            "SELECT encrypted_value FROM agent_research_credentials WHERE task_id = %s", (task_id,)
        ).fetchone()
    if not row:
        return {}
    return json.loads(secret_store.decrypt_setting(row['encrypted_value']))


def replace_agent_project_memory(owner_id, task_id, chunks, embeddings):
    """Replace explicit project notes, retaining text for lexical search if embedding is offline."""
    embeddings = embeddings if embeddings is not None else [None] * len(chunks)
    with database.connect() as conn:
        with conn.transaction():
            conn.execute("DELETE FROM agent_project_memory WHERE source_task_id = %s", (task_id,))
            now = time.time()
            for content, embedding in zip(chunks, embeddings):
                vector = np.asarray(embedding, dtype=np.float32).tobytes() if embedding is not None else None
                conn.execute(
                    "INSERT INTO agent_project_memory (owner_id, source_task_id, content, embedding, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (owner_id, task_id, content, vector, now),
                )


def search_agent_project_memory(owner_id, query_text, query_embedding=None, keyword_query=None,
                                top_k=5, min_similarity=0.30,
                                min_lexical_score=0.08):
    """Hybrid semantic and exact-term retrieval for this user's private project notes."""
    import re
    stop_words = {'the', 'and', 'for', 'from', 'with', 'that', 'this', 'into', 'when', 'where',
                  'what', 'how', 'are', 'was', 'were', 'will', 'should', 'could', 'would', 'can',
                  'use', 'using', 'used', 'project', 'task', 'agent', 'need', 'make', 'build'}
    def tokenize(text):
        raw = re.findall(r"[a-z0-9_+.]+", (text or '').casefold())
        # A task may ask for `python` while a note records that only `python3`
        # exists. Keep both forms retrievable together so the saved fact can
        # correct the command choice.
        return {'python_interpreter' if re.fullmatch(r'python(?:[0-9]+(?:\.[0-9]+)*)?', term) else term
                for term in raw} - stop_words

    terms = tokenize(keyword_query or query_text)
    if not terms:
        return []
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT source_task_id, content, embedding, updated_at FROM agent_project_memory "
            "WHERE owner_id = %s", (owner_id,),
        ).fetchall()
    if not rows:
        return []
    query = np.asarray(query_embedding, dtype=np.float32) if query_embedding is not None else None
    query_norm = np.linalg.norm(query) if query is not None else 0
    if query is not None and (not query_norm or not np.all(np.isfinite(query))):
        query, query_norm = None, 0
    documents = [tokenize(row['content']) for row in rows]
    frequencies = {term: sum(term in document for document in documents) for term in terms}
    weights = {term: np.log((len(rows) + 1) / (frequency + 1)) + 1.0
               for term, frequency in frequencies.items()}
    query_weight = sum(weights.values()) or 1.0
    candidates = []
    for row, document in zip(rows, documents):
        lexical = sum(weights[term] for term in terms & document) / query_weight
        similarity = 0.0
        if query is not None and row['embedding'] is not None:
            try:
                vector = np.frombuffer(row['embedding'], dtype=np.float32)
            except (TypeError, ValueError):
                vector = np.array([], dtype=np.float32)
            if vector.size == query.size and np.all(np.isfinite(vector)):
                norm = np.linalg.norm(vector)
                if norm:
                    similarity = float(np.dot(query, vector) / (query_norm * norm))
        if similarity >= min_similarity or lexical >= min_lexical_score:
            score = max(similarity, 0.75 * lexical)
            candidates.append((score, similarity, lexical, row))
    ranked = sorted(candidates, key=lambda item: item[0], reverse=True)
    results, seen = [], set()
    for score, similarity, lexical, row in ranked:
        normalized = ' '.join(row['content'].casefold().split())
        if normalized in seen:
            continue
        seen.add(normalized)
        results.append(dict(source_task_id=row['source_task_id'], content=row['content'], score=score,
                            similarity=similarity, lexical_score=lexical, updated_at=row['updated_at']))
        if len(results) >= top_k:
            break
    return results


def has_agent_project_memory(owner_id):
    with database.connect() as conn:
        return conn.execute(
            "SELECT 1 FROM agent_project_memory WHERE owner_id = %s LIMIT 1", (owner_id,)
        ).fetchone() is not None


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


def save_skill(skill):
    """Insert or update one of a user's skills; returns the stored record."""
    name = str(skill['name']).strip()
    skill_folder(name)
    description = str(skill.get('description') or '').strip()
    body = str(skill.get('body') or '').strip()
    if not body or len(body) > MAX_SKILL_BODY_CHARS:
        raise ValueError('Enter skill instructions (up to 200,000 characters).')
    if len(description) > 300:
        raise ValueError('Skill descriptions must be at most 300 characters.')
    with database.connect() as conn:
        row = conn.execute(
            "SELECT id, created_at FROM agent_skills WHERE owner_id = %s AND name = %s",
            (skill['owner'], name),
        ).fetchone()
        skill_id = skill.get('id') or (row['id'] if row else None) or uuid.uuid4().hex
        now = time.time()
        created_at = row['created_at'] if row else (skill.get('created_at') or now)
        conn.execute(
            "INSERT INTO agent_skills (id, owner_id, name, description, body, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET "
            "owner_id = EXCLUDED.owner_id, name = EXCLUDED.name, description = EXCLUDED.description, "
            "body = EXCLUDED.body, updated_at = EXCLUDED.updated_at",
            (skill_id, skill['owner'], name, description, body, created_at, now),
        )
    return get_skill(skill['owner'], skill_id)


def uuid_skill_id():
    import uuid
    return uuid.uuid4().hex


def list_skills(owner_id):
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT id, name, description, body, created_at, updated_at FROM agent_skills "
            "WHERE owner_id = %s ORDER BY lower(name)", (owner_id,),
        ).fetchall()
    return [dict(row, owner=owner_id) for row in rows]


def get_skill(owner_id, skill_id):
    with database.connect() as conn:
        row = conn.execute(
            "SELECT id, name, description, body, created_at, updated_at FROM agent_skills "
            "WHERE owner_id = %s AND id = %s", (owner_id, skill_id),
        ).fetchone()
    return dict(row, owner=owner_id) if row else None


def get_skill_by_name(owner_id, name):
    with database.connect() as conn:
        row = conn.execute(
            "SELECT id, name, description, body, created_at, updated_at FROM agent_skills "
            "WHERE owner_id = %s AND name = %s", (owner_id, str(name).strip()),
        ).fetchone()
    return dict(row, owner=owner_id) if row else None


def get_skills_by_names(owner_id, names):
    wanted = [str(name).strip() for name in names if str(name).strip()]
    if not wanted:
        return []
    skills = list_skills(owner_id)
    by_name = {skill['name']: skill for skill in skills}
    return [by_name[name] for name in dict.fromkeys(wanted) if name in by_name]


def delete_skill(owner_id, skill_id):
    with database.connect() as conn:
        return conn.execute(
            "DELETE FROM agent_skills WHERE owner_id = %s AND id = %s RETURNING id",
            (owner_id, skill_id),
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
