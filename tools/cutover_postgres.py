"""Start Compose PostgreSQL, stop writers, migrate/verify, and switch Hearth.

Run after installing Docker Engine + Compose and granting this user Docker access.
Credentials live in .env.postgres (mode 600); originals stay in SQLite backups.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import dotenv_values, set_key

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / '.env.postgres'
SERVICES = ('ai-webui.service', 'flm-tool-shim.service', 'lemonade-guard-shim.service', 'lmstudio-shim.service')


def prepare_config():
    if not CONFIG.exists():
        password = secrets.token_hex(32)
        content = f'POSTGRES_DB=hearth\nPOSTGRES_USER=hearth\nPOSTGRES_PASSWORD={password}\nPOSTGRES_PORT=55432\nDATABASE_URL=postgresql://hearth:{password}@127.0.0.1:55432/hearth\n'
        descriptor = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as handle:
            handle.write(content)
    os.chmod(CONFIG, 0o600)
    values = dotenv_values(CONFIG)
    if not values.get('DATABASE_URL') or not values.get('POSTGRES_PASSWORD'):
        raise RuntimeError('.env.postgres must contain DATABASE_URL and POSTGRES_PASSWORD')
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    values = prepare_config()
    if args.prepare_only:
        print('Prepared .env.postgres; existing application settings are unchanged.')
        return
    if not shutil.which('docker'):
        raise RuntimeError('Docker is not installed. Install Docker Engine and the Compose plugin first.')
    compose = ['docker', 'compose', '--env-file', str(CONFIG), '-f', str(ROOT / 'compose.yaml')]
    subprocess.run(['docker', 'info'], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(compose + ['up', '-d', '--wait', 'postgres'], check=True, cwd=ROOT)
    os.environ['DATABASE_URL'] = values['DATABASE_URL']
    import database
    from tools.migrate_sqlite_to_postgres import migrate, discover
    # Refuse a populated target before interrupting any running services.
    with database.connect() as conn:
        tables = conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema() AND table_type='BASE TABLE'").fetchall()
        from psycopg import sql
        for table in tables:
            if conn.execute(sql.SQL('SELECT 1 FROM {} LIMIT 1').format(sql.Identifier(table['table_name']))).fetchone():
                raise RuntimeError('PostgreSQL destination already contains data; refusing a second cutover.')
    active = [service for service in SERVICES if subprocess.run(
        ['systemctl', '--user', 'is-active', '--quiet', service]).returncode == 0]
    if active:
        subprocess.run(['systemctl', '--user', 'stop', *active], check=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = ROOT / 'data/sqlite-backups' / ('cutover-' + stamp)
    try:
        report = migrate(discover(ROOT), backup)
        # Save the previous environment alongside the SQLite snapshots.
        env_file = ROOT / '.env'
        if env_file.exists():
            shutil.copy2(env_file, backup / 'application.env.before')
            os.chmod(backup / 'application.env.before', 0o600)
        set_key(str(env_file), 'DATABASE_URL', values['DATABASE_URL'])
        os.chmod(env_file, 0o600)
        report_path = ROOT / 'data/postgres-migration.json'
        report_path.write_text(json.dumps(report, indent=2))
        os.chmod(report_path, 0o600)
        if active:
            subprocess.run(['systemctl', '--user', 'start', *active], check=True)
        import requests
        for attempt in range(30):
            try:
                response = requests.get('https://127.0.0.1:5443/login', verify=False, timeout=2)
                if response.status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(1)
        else:
            raise RuntimeError('PostgreSQL is migrated but Hearth did not pass its HTTP startup check. Inspect the service log; backups are retained.')
        print(f"Cutover complete: {report['rows']} rows verified across {len(report['tables'])} tables. Report: {report_path}")
    except Exception:
        # Never automatically revert a database after PostgreSQL may have accepted
        # writes. Retain both copies and report the exact failed step to the operator.
        print(f'Cutover stopped; SQLite originals and backups are retained at {backup}.', file=sys.stderr)
        raise


if __name__ == '__main__':
    main()
