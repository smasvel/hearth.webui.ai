# First-time installation (Ubuntu)

Run these commands from the Hearth project directory. This installs the web app
and PostgreSQL; model runtimes and model downloads are configured separately.
Use an ordinary account with sudo access and a graphical or terminal login session.

For an existing SQLite installation, follow [the migration guide](../POSTGRES_MIGRATION.md)
instead. First-time setup refuses to skip over stored SQLite data.

## 1. Install system dependencies

```bash
sudo bash deploy/install-host-deps.sh --base-only
```

This installs Python and venv support, Docker Engine, Docker Compose, ACL tools,
FFmpeg and curl. It starts Docker and adds your account to the Docker group.
The script's mode without `--base-only` is the original example-host host's full backend
configuration, with machine-specific paths; use `--base-only` on a new machine.

Log out and back in so your processes inherit Docker group membership. Verify:

```bash
docker info
docker compose version
```

To grant the current session access without logging out, use:

```bash
sudo setfacl -m "u:$(id -un):rw" /var/run/docker.sock
```

The ACL applies to the current socket; Docker group membership handles future
logins. This host does not provide `sg` or `newgrp`.

## 2. Install and initialize Hearth

```bash
bash deploy/install-first-time.sh
```

The installer creates `.venv`, installs Python requirements and Chromium, and then:

- Generates a random PostgreSQL password in `.env.postgres` and a matching
  `DATABASE_URL` in `.env`, both with mode `600`.
- Generates a stable Flask session-signing secret.
- Starts PostgreSQL 18 with Compose, waiting for its readiness check.
- Creates the schema and prompts twice for the initial administrator password.
  Passwords are hashed; the plaintext password is not written to configuration.
- Creates and enables `~/.config/systemd/user/ai-webui.service`, using this checkout's
  absolute path. Existing customized service files are preserved.

Existing credentials, users, and database contents are preserved on reruns. The
installer does not configure model backends, Tailscale, Caddy, or LAN proxy services.

Useful options:

```bash
bash deploy/install-first-time.sh --admin-username yourname
bash deploy/install-first-time.sh --skip-browser --no-service
.venv/bin/python tools/first_time_setup.py --help
```

With `--no-service`, start the app manually using `.venv/bin/python app.py`.
`--skip-browser` omits Chromium; web search can still fall back to snippets and
ordinary HTTP requests. If Chromium needs OS libraries after installation, run:

```bash
sudo .venv/bin/python -m playwright install-deps chromium
```

To run the user service without an active login session:

```bash
sudo loginctl enable-linger "$(id -un)"
```

## 3. Open the application and configure a model backend

Open **https://localhost:5443**. On first startup Hearth generates a self-signed
certificate, so accept the local browser certificate warning. Sign in with the
administrator credentials created above.

Install and start at least one supported model runtime, then configure its URL
and credentials under Settings. LM Studio's default endpoint is
`http://localhost:1234`; Lemonade's is `http://localhost:13305`. Download a model
and load it from Models. PostgreSQL setup does not install model weights.

## Verify, maintain, and back up

```bash
docker compose --env-file .env.postgres ps
systemctl --user status ai-webui.service
journalctl --user -u ai-webui.service -f
.venv/bin/python tools/run_tests.py --browser
```

The test runner uses an isolated PostgreSQL schema. Run it instead of invoking
browser test scripts directly against application tables.

For an application update, install requirements again and restart the service:

```bash
.venv/bin/python -m pip install -r requirements.txt
systemctl --user restart ai-webui.service
```

Keep `.env` and `.env.postgres` private and backed up. PostgreSQL lives in the
Compose named volume; changing `POSTGRES_PASSWORD` in a file does not rotate the
password inside an existing database. Do not use `docker compose down -v` to update
containers, because it deletes the database volume. Backup instructions and SQLite
cutover details are in [POSTGRES_MIGRATION.md](../POSTGRES_MIGRATION.md).
