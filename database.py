"""Shared PostgreSQL configuration and short-lived transactional connections."""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import uuid

from dotenv import load_dotenv
import psycopg
from psycopg import Error, IntegrityError
from psycopg import sql

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / 'data'
load_dotenv(ROOT / '.env')


class Row(dict):
    """Named rows with positional access for scalar/aggregate queries."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


def row_factory(cursor):
    names = [column.name for column in cursor.description] if cursor.description else []
    return lambda values: Row(zip(names, values))


def connect(*, autocommit=False):
    url = os.environ.get('DATABASE_URL')
    if not url:
        raise RuntimeError('DATABASE_URL is required. Start the PostgreSQL Compose service and configure .env.')
    schema = os.environ.get('HEARTH_DATABASE_SCHEMA', 'public')
    if not re.fullmatch(r'[a-z_][a-z0-9_]*', schema):
        raise ValueError('Invalid HEARTH_DATABASE_SCHEMA')
    return psycopg.connect(url, connect_timeout=10, autocommit=autocommit,
        row_factory=row_factory, options=f'-c search_path={schema}')


def init_schema():
    with connect() as conn:
        conn.execute((ROOT / 'database_schema.sql').read_text())


@contextmanager
def temporary_schema():
    """Isolate integration fixtures on the configured test PostgreSQL instance."""
    name = 'hearth_test_' + uuid.uuid4().hex
    previous = os.environ.get('HEARTH_DATABASE_SCHEMA')
    with connect(autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(name)))
    os.environ['HEARTH_DATABASE_SCHEMA'] = name
    try:
        init_schema()
        yield name
    finally:
        if previous is None:
            os.environ.pop('HEARTH_DATABASE_SCHEMA', None)
        else:
            os.environ['HEARTH_DATABASE_SCHEMA'] = previous
        with connect(autocommit=True) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(name)))
