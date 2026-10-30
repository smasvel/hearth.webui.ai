"""Initialize a new Hearth installation without overwriting existing data or secrets."""
import argparse
import getpass
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys

from dotenv import dotenv_values, set_key, unset_key

ROOT = Path(__file__).resolve().parents[1]


def install_agent_runtime(rebuild=False):
    version_path = ROOT / 'agent_container' / 'IMAGE_VERSION'
    if not version_path.is_file():
        raise RuntimeError('Agent runtime image version is missing.')
    version = version_path.read_text().strip()
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
        raise RuntimeError('Agent runtime image version is invalid.')
    image = 'hearth-agent-opencode:' + version
    inspect = subprocess.run(['docker', 'image', 'inspect', image],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if inspect.returncode == 0 and not rebuild:
        print(f'Agent runtime image is already installed: {image}')
        return
    subprocess.run(['docker', 'build', '--pull', '--tag', image,
                    '--build-arg', 'OPENCODE_VERSION=' + version,
                    str(ROOT / 'agent_container')], check=True, cwd=ROOT)
    subprocess.run(['docker', 'image', 'inspect', image], check=True, stdout=subprocess.DEVNULL)
    print(f'Installed isolated agent runtime: {image}')
sys.path.insert(0, str(ROOT))


def write_private(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        handle.write(content)


def prepare_environment(port):
    env_path = ROOT / '.env'
    postgres_path = ROOT / '.env.postgres'
    values = dotenv_values(env_path) if env_path.exists() else {}
    if values.get('DATABASE_URL') and not postgres_path.exists():
        raise RuntimeError('An existing DATABASE_URL has no matching .env.postgres. Restore that configuration; setup will not replace it.')
    if not postgres_path.exists():
        password = secrets.token_hex(32)
        write_private(postgres_path, f'POSTGRES_DB=hearth\nPOSTGRES_USER=hearth\nPOSTGRES_PASSWORD={password}\nPOSTGRES_PORT={port}\nDATABASE_URL=postgresql://hearth:{password}@127.0.0.1:{port}/hearth\n')
    postgres = dotenv_values(postgres_path)
    if not postgres.get('DATABASE_URL') or not postgres.get('POSTGRES_PASSWORD'):
        raise RuntimeError('.env.postgres is incomplete; restore its database URL and password.')
    if values.get('DATABASE_URL') and values['DATABASE_URL'] != postgres['DATABASE_URL']:
        raise RuntimeError('.env and .env.postgres select different databases. Setup will not switch an existing installation.')
    if not env_path.exists():
        write_private(env_path, (ROOT / '.env.example').read_text())
    if not values.get('DATABASE_URL'):
        set_key(str(env_path), 'DATABASE_URL', postgres['DATABASE_URL'])
    # Older installers placed account names/password hashes in .env. Remove those
    # legacy account fields so PostgreSQL is the only source of user profile data.
    stored_values = dotenv_values(env_path)
    for key in ('ADMIN_USERNAME', 'ADMIN_PASSWORD_HASH'):
        if key in stored_values:
            unset_key(str(env_path), key)
    for path in (env_path, postgres_path):
        os.chmod(path, 0o600)
    return dotenv_values(env_path)


def has_sqlite_data():
    # Fresh setup must never silently strand an old installation's history.
    import sqlite3
    from contextlib import closing
    for path in (ROOT / 'chat_history.db', *(ROOT / 'data').glob('*.db')):
        if not path.exists() or not path.stat().st_size:
            continue
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            for name, in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
                quoted = '"' + name.replace('"', '""') + '"'
                if conn.execute('SELECT 1 FROM ' + quoted + ' LIMIT 1').fetchone():
                    return True
    return False


def initialize_application_storage():
    """Apply the canonical PostgreSQL DDL and private application migrations."""
    import database
    import agent_store
    import model_metadata_store
    import model_resource_warnings
    import secret_store
    import settings_store
    import users_store

    database.init_schema()
    users_store.init_db()
    agent_store.init_db()
    settings_store.init_db()
    model_metadata_store.init_db()
    model_resource_warnings.init_db()
    model_metadata_store.import_test_runs(database.DATA_DIR / 'model-test-runs')
    model_metadata_store.migrate_registry_file('kobold', Path.home() / '.config/koboldcpp/models.json')
    model_metadata_store.migrate_registry_file('vllm', Path.home() / '.config/vllm/models.json')
    # Persist the Flask signing key before deleting any legacy file copy. New installs
    # generate it here; direct app starts retain the same idempotent startup fallback.
    env_path = ROOT / '.env'
    legacy_session_key = os.environ.get('FLASK_SECRET_KEY')
    secret_store.get_or_create_flask_key(legacy_session_key)
    os.environ.pop('FLASK_SECRET_KEY', None)
    if env_path.exists() and 'FLASK_SECRET_KEY' in dotenv_values(env_path):
        unset_key(str(env_path), 'FLASK_SECRET_KEY')

    # Move legacy backend endpoints and encrypted API keys before the systemd service
    # is installed. This is idempotent and leaves an existing database value intact.
    settings_store.migrate_legacy_environment()
    return users_store.count_users()


def service_text(root):
    # ExecStart accepts quoted arguments; WorkingDirectory takes an unquoted
    # whole-line path. Both interpret percent specifiers.
    if '\n' in str(root) or '\r' in str(root):
        raise ValueError('The installation path must not contain newlines.')
    def quote(value):
        return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
    return f'''[Unit]
Description=Hearth local AI web UI
After=network.target

[Service]
WorkingDirectory={str(root).replace('%', '%%')}
ExecStart={quote(root / '.venv/bin/python')} {quote(root / 'app.py')}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
'''


def install_service():
    directory = Path.home() / '.config/systemd/user'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'ai-webui.service'
    # Preserve customized services from earlier installations.
    if not path.exists():
        path.write_text(service_text(ROOT))
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', '--user', 'enable', 'ai-webui.service'], check=True)
    # enable --now leaves an already-running service on its old code/certificate.
    subprocess.run(['systemctl', '--user', 'restart', 'ai-webui.service'], check=True)
    # Keep the user systemd manager active at boot before an interactive login,
    # so the app's enabled user service starts after a machine restart.
    subprocess.run(['loginctl', 'enable-linger', getpass.getuser()], check=True)


def validate_existing_installation():
    existing = dotenv_values(ROOT / '.env')
    if not existing.get('DATABASE_URL') and has_sqlite_data():
        raise RuntimeError('Existing local database files contain data. Configure DATABASE_URL for your installation before running setup.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-service', action='store_true', help='Configure the app without installing or starting a user service')
    parser.add_argument('--postgres-port', type=int, default=55432, help='Host database port for a new configuration')
    parser.add_argument('--project-name', help='Optional separate Compose project name')
    parser.add_argument('--rebuild-agent-image', action='store_true',
                        help='Rebuild the isolated agent image even if it is already installed')
    from tools import backend_installer
    backend_installer.add_arguments(parser)
    args = parser.parse_args()
    if not 1 <= args.postgres_port <= 65535:
        parser.error('--postgres-port must be between 1 and 65535')
    if os.name != 'nt' and os.geteuid() == 0:
        parser.error('Run as your regular user, not sudo; use the host dependency installer for privileged packages.')
    if not args.no_service and not Path('/run/systemd/system').is_dir():
        parser.error('systemd is not running. Use --no-service and start Hearth manually.')
    if not shutil.which('docker'):
        raise RuntimeError('Run deploy/install-first-time.sh to install missing host dependencies first.')
    subprocess.run(['docker', 'info'], check=True, stdout=subprocess.DEVNULL)
    validate_existing_installation()
    backends = backend_installer.select_backends(args)
    if backends:
        backend_installer.install_backends(backends)
    install_agent_runtime(args.rebuild_agent_image)
    values = prepare_environment(args.postgres_port)
    # This process must use the selected configuration even if the caller exported
    # a different URL for a previous development/test session.
    os.environ['DATABASE_URL'] = values['DATABASE_URL']
    compose = ['docker', 'compose', '--env-file', str(ROOT / '.env.postgres'), '-f', str(ROOT / 'compose.yaml')]
    if args.project_name:
        compose += ['--project-name', args.project_name]
    subprocess.run(compose + ['up', '-d', '--wait', 'postgres'], check=True, cwd=ROOT)
    user_count = initialize_application_storage()
    from local_tls import ensure_certificate
    ensure_certificate(ROOT / 'certs')
    if user_count == 0:
        print('No users exist. The first page visit will prompt you to create the administrator account.')
    else:
        print('Existing user accounts preserved.')
    if not args.no_service:
        install_service()
    print('Setup complete. Open https://localhost:5443 and create the administrator account.'
          if not args.no_service else
          'Database ready. Start Hearth with .venv/bin/python app.py, then open it to create the administrator account.')


if __name__ == '__main__':
    main()
