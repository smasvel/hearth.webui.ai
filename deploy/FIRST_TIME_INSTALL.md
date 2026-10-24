# First-time installation (Ubuntu)

Run these commands from the Hearth project directory. This installs the web app,
PostgreSQL, and isolated agent build image; model runtimes and model downloads are
configured separately.
Use an ordinary account with sudo access and a graphical or terminal login session.

## Install and initialize Hearth

```bash
bash deploy/install-first-time.sh
```

The installer uses `sudo` to install missing Ubuntu host dependencies, starts
Docker, and grants the current user access. It creates `.venv`, installs Python
requirements and Chromium, builds the isolated agent image with Python,
Java/Maven, Node.js/npm, and build tools, and then:

- Generates a random PostgreSQL password in `.env.postgres` and a matching
  `DATABASE_URL` in `.env`, both with mode `600`.
- Generates a stable Flask session-signing secret.
- Starts PostgreSQL 18 with Compose, waiting for its readiness check.
- Loads the application DDL from `database_schema.sql` into the new PostgreSQL
  database, then creates the initial administrator account. The DDL includes the
  per-user agent workflow setting; user-store initialization applies any remaining
  idempotent account migrations.
- Prompts twice for the initial administrator password. Hearth stores only its
  password hash; the plaintext password is not written to configuration.
- Creates and enables `~/.config/systemd/user/ai-webui.service`, using this checkout's
  absolute path. Existing customized service files are preserved.
- Enables systemd lingering for the installing account, so Hearth starts after a
  machine restart even before that account logs in.

Existing credentials, users, and database contents are preserved on reruns. The
installer does not configure model backends, Tailscale, Caddy, or LAN proxy services.

Useful options:

```bash
bash deploy/install-first-time.sh --admin-username yourname
bash deploy/install-first-time.sh --skip-browser --no-service
.venv/bin/python tools/first_time_setup.py --help
```

### Initial administrator account

By default, the first-time installer creates an administrator named `admin` and
prompts you to enter and confirm a password. Choose a unique, strong password and
save it in your password manager. To choose a different account name, pass it to
the installer:

```bash
bash deploy/install-first-time.sh --admin-username yourname
```

You can also set `ADMIN_USERNAME` in `.env` before setup. For scripted installs,
pass the password through standard input; avoid putting it directly in a command
argument, shell history, or a checked-in file:

```bash
printf '%s\n' "$HEARTH_ADMIN_PASSWORD" | bash deploy/install-first-time.sh \
  --admin-username yourname --admin-password-stdin
```

Set `HEARTH_ADMIN_PASSWORD` through your deployment secret manager or another
private mechanism before running that example. The installer hashes the supplied
password and stores the hash in the private `.env`; it does not store the plaintext.
On reruns, existing users and credentials are preserved. If accounts already
exist, the installer will not create or reset an administrator password.

For a manual setup, `.venv/bin/python tools/first_time_setup.py --help` lists the
same admin options. The setup command starts the Compose PostgreSQL service,
applies `database_schema.sql`, then seeds the initial administrator if the users
table is empty.

With `--no-service`, start the app manually using `.venv/bin/python app.py`.
`--skip-browser` omits Chromium; web search can still fall back to snippets and
ordinary HTTP requests. If Chromium needs OS libraries after installation, run:

```bash
sudo .venv/bin/python -m playwright install-deps chromium
```

The installer enables lingering as part of service setup. `--no-service` skips
the systemd service and boot-time startup configuration; run the app manually in
that mode.

## Open the application and configure a model backend

Open **https://localhost:5443**. On first startup Hearth generates a self-signed
certificate, so accept the local browser certificate warning. Sign in with the
administrator credentials created above.

Install and start at least one supported model runtime, then configure its URL
and credentials under Settings. LM Studio's default endpoint is
`http://localhost:1234`; Lemonade's is `http://localhost:13305`. Download a model
and load it from Models. PostgreSQL setup does not install model weights.

## Verify and maintain

```bash
docker compose --env-file .env.postgres ps
systemctl --user status ai-webui.service
journalctl --user -u ai-webui.service -f
.venv/bin/python tools/run_tests.py --browser
```

The test runner uses an isolated PostgreSQL schema. Run it instead of invoking
browser test scripts directly against application tables.

For an application update, install requirements again, rebuild the agent image
when its recipe or OpenCode version changed, and restart the service:

```bash
.venv/bin/python -m pip install -r requirements.txt
bash deploy/install-first-time.sh --rebuild-agent-image
systemctl --user restart ai-webui.service
```

The agent image is built by the installer as the regular Hearth user, using the
existing Docker permission. Its apt and language package managers run inside
each isolated task container; host packages are not installed for agent tasks.

Keep `.env` and `.env.postgres` private. PostgreSQL lives in the
Compose named volume; changing `POSTGRES_PASSWORD` in a file does not rotate the
password inside an existing database. Do not use `docker compose down -v` to update
containers, because it deletes the database volume.
