"""Back up every local SQLite database and transactionally migrate to PostgreSQL.

Stop Hearth before the final migration so no writes arrive after the snapshot.
Original SQLite files are retained. Nonempty destination tables are refused.
"""
import argparse
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import database
import psycopg
from psycopg import sql

ROOT = database.ROOT


def discover(root):
    return sorted(p for p in Path(root).rglob('*')
        if p.is_file() and p.suffix in ('.db', '.sqlite', '.sqlite3')
        and not any(part in ('.venv', 'venv', 'sqlite-backups', 'node_modules', '.git') for part in p.parts))


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def digest(rows):
    def encode(value):
        if isinstance(value, (bytes, memoryview)):
            return {'bytes': bytes(value).hex()}
        return value
    hashes = [hashlib.sha256(json.dumps([encode(v) for v in row], ensure_ascii=True,
        separators=(',', ':'), allow_nan=False).encode()).digest() for row in rows]
    return hashlib.sha256(b''.join(sorted(hashes))).hexdigest()


def snapshot(sources, directory):
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    snapshots = []
    for number, path in enumerate(sources):
        destination = directory / f'{number:03d}-{path.name}'
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as source:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()
        os.chmod(destination, 0o600)
        snapshots.append((path, destination))
    return snapshots


def read_sources(snapshots):
    tables = {}
    sequences = defaultdict(int)
    inventory = []
    for original, backup in snapshots:
        conn = sqlite3.connect(backup)
        try:
            if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError(f'SQLite integrity check failed: {original}')
            names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            inventory.append({'source': str(original), 'backup': str(backup), 'tables': names})
            for name in names:
                cursor = conn.execute('SELECT * FROM ' + quote(name))
                columns = [c[0] for c in cursor.description]
                rows = cursor.fetchall()
                if name in tables and tables[name]['columns'] != columns:
                    raise ValueError(f'Incompatible columns across source databases: {name}')
                entry = tables.setdefault(name, {'columns': columns, 'rows': []})
                entry['rows'].extend(rows)
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
                for name, value in conn.execute('SELECT name,seq FROM sqlite_sequence'):
                    sequences[name] = max(sequences[name], value)
        finally:
            conn.close()
    return tables, sequences, inventory


def verify(conn, tables):
    report = {}
    for name, entry in tables.items():
        selected = sql.SQL(',').join(map(sql.Identifier, entry['columns']))
        actual = conn.execute(sql.SQL('SELECT {} FROM {}').format(selected, sql.Identifier(name))).fetchall()
        expected_digest = digest(entry['rows'])
        actual_digest = digest([tuple(row.values()) for row in actual])
        if len(actual) != len(entry['rows']) or actual_digest != expected_digest:
            raise ValueError(f'Row count/content mismatch: {name}')
        report[name] = {'rows': len(actual), 'sha256': actual_digest}
    return report


def migrate(sources, backup_dir, *, verify_only=False):
    snapshots = [(p, p) for p in sources] if verify_only else snapshot(sources, backup_dir)
    tables, sequences, inventory = read_sources(snapshots)
    legacy_constraints = []
    with database.connect() as conn:
        # Serialize migration attempts; all schema/data changes roll back on failure.
        conn.execute("SELECT pg_advisory_xact_lock(742061861)")
        if not verify_only:
            conn.execute((ROOT / 'database_schema.sql').read_text())
            known = {r['table_name'] for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema() AND table_type='BASE TABLE'")}
            unknown = set(tables) - known
            if unknown:
                raise ValueError(f'Unrecognized source tables; add schema before migrating: {sorted(unknown)}')
            for name in known:
                if conn.execute(sql.SQL('SELECT 1 FROM {} LIMIT 1').format(sql.Identifier(name))).fetchone():
                    raise ValueError(f'Destination is not empty: {name}. Refusing to overwrite data.')
            constraints = conn.execute("""SELECT c.conname, t.relname AS table_name,
                pg_get_constraintdef(c.oid) AS definition FROM pg_constraint c
                JOIN pg_class t ON t.oid=c.conrelid JOIN pg_namespace n ON n.oid=t.relnamespace
                WHERE c.contype='f' AND n.nspname=current_schema()""").fetchall()
            for constraint in constraints:
                conn.execute(sql.SQL('ALTER TABLE {} DROP CONSTRAINT {}').format(
                    sql.Identifier(constraint['table_name']), sql.Identifier(constraint['conname'])))
            # Parents precede their FK children. All other tables are independent.
            order = ['users', 'conversations', 'quiz_decks']
            for name in sorted(tables, key=lambda n: (order.index(n) if n in order else len(order), n)):
                entry = tables[name]
                columns = sql.SQL(',').join(map(sql.Identifier, entry['columns']))
                query = sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(sql.Identifier(name),
                    columns, sql.SQL(',').join(sql.Placeholder() for _ in entry['columns']))
                with conn.cursor() as cursor:
                    cursor.executemany(query, entry['rows'])
            for constraint in constraints:
                conn.execute(sql.SQL('ALTER TABLE {} ADD CONSTRAINT {} {} NOT VALID').format(
                    sql.Identifier(constraint['table_name']), sql.Identifier(constraint['conname']),
                    sql.SQL(constraint['definition'])))
                try:
                    with conn.transaction():
                        conn.execute(sql.SQL('ALTER TABLE {} VALIDATE CONSTRAINT {}').format(
                            sql.Identifier(constraint['table_name']), sql.Identifier(constraint['conname'])))
                except psycopg.errors.ForeignKeyViolation:
                    # Preserve historical SQLite orphans without inventing parents or
                    # deleting records. NOT VALID still enforces future writes.
                    legacy_constraints.append(constraint['conname'])
            for row in conn.execute("SELECT table_name,column_name FROM information_schema.columns WHERE table_schema=current_schema() AND is_identity='YES'").fetchall():
                name, column = row['table_name'], row['column_name']
                maximum = conn.execute(sql.SQL('SELECT COALESCE(MAX({}),0) AS maximum FROM {}').format(
                    sql.Identifier(column), sql.Identifier(name))).fetchone()['maximum']
                maximum = max(maximum, sequences.get(name, 0))
                conn.execute('SELECT setval(pg_get_serial_sequence(%s,%s),%s,%s)',
                    (name, column, max(maximum, 1), maximum > 0))
        counts = verify(conn, tables)
    return {'verified_at': datetime.now(timezone.utc).isoformat(), 'sources': inventory, 'tables': counts,
        'rows': sum(t['rows'] for t in counts.values()), 'verify_only': verify_only,
        'legacy_unvalidated_constraints': legacy_constraints}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', action='append', type=Path, help='Repeat for each source; defaults to all local databases')
    parser.add_argument('--backup-dir', type=Path)
    parser.add_argument('--verify-only', action='store_true', help='Compare sources with PostgreSQL without writing')
    parser.add_argument('--report', type=Path, default=ROOT / 'data/postgres-migration.json')
    args = parser.parse_args()
    sources = args.source or discover(ROOT)
    if not sources:
        parser.error('No source databases found')
    backup = args.backup_dir or ROOT / 'data/sqlite-backups' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8])
    report = migrate(sources, backup, verify_only=args.verify_only)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2))
    os.chmod(args.report, 0o600)
    print(f"Verified {report['rows']} rows across {len(report['tables'])} tables; report: {args.report}")


if __name__ == '__main__':
    main()
