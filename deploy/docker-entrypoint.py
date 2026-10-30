"""Initialize persistent application storage before starting the Docker app."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from tools.first_time_setup import initialize_application_storage
    initialize_application_storage()
    from local_tls import ensure_certificate
    ensure_certificate(ROOT / 'certs')
    import database
    # A second instance must not eagerly load support models on shared host runtimes.
    # Users can opt in under Settings; preserve their choice on every restart.
    with database.connect() as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES ('support_models_keep_loaded', 'false') ON CONFLICT(key) DO NOTHING")
    os.execv(sys.executable, [sys.executable, str(ROOT / 'app.py')])


if __name__ == '__main__':
    main()
