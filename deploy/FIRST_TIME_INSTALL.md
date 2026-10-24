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
- Setup generates or imports the session-signing key and stores it encrypted in
  PostgreSQL before starting the app; direct app starts do the same on first launch.
- Starts PostgreSQL 18 with Compose, waiting for its readiness check.
- Loads the application DDL from `database_schema.sql` into the new PostgreSQL
  database, leaving the users table empty on a new install. The DDL includes the
  per-user agent workflow setting, application settings and encrypted-secret tables,
  and agent task, schedule, runtime metadata, and private workspace tables. Setup
  applies the account, settings, secret, and agent-store migrations before service start.
- Displays a one-time administrator setup form on the first page visit. The
  password is saved only as a salted, one-way hash in PostgreSQL.
- Creates and enables `~/.config/systemd/user/ai-webui.service`, using this checkout's
  absolute path. Existing customized service files are preserved.
- Enables systemd lingering for the installing account, so Hearth starts after a
  machine restart even before that account logs in.
- Starts Hearth's persistent agent worker with the service. The worker checks
  PostgreSQL for due schedules while Hearth is running.

Existing credentials, users, and database contents are preserved on reruns. The
installer does not configure model backends, Tailscale, Caddy, or LAN proxy services.
Account profile fields (username, role, creation time, gateway token, and agent access)
are stored in PostgreSQL. Passwords use salted, one-way Werkzeug hashes; Hearth never
stores plaintext passwords or returns password hashes through user APIs. Hashing is
intentional because passwords should be verified, not reversibly encrypted.
The encrypted Flask session key is stored in PostgreSQL as well. Its encryption key is
derived from the PostgreSQL connection password, so changing that database password
rotates the session key and requires users to sign in again.

Useful options:

```bash
bash deploy/install-first-time.sh --skip-browser --no-service
.venv/bin/python tools/first_time_setup.py --help
```

### Initial administrator account

After installation, open Hearth in a browser. The first page visit asks you to choose
the administrator username and enter and confirm a password. This creates the first
account in PostgreSQL and signs you in. Setup is one-time: later visits show the normal
sign-in page, and the database prevents multiple simultaneous first accounts. The
username field starts with `admin`, and you can change it on that page. The installer
does not collect or store profile details or the password in local configuration; the
account and its salted, one-way password hash are stored in PostgreSQL. Backend
endpoints and credentials edited in **Settings** are stored in PostgreSQL as well;
API keys are encrypted with a key derived from the PostgreSQL connection password.
Legacy backend URL and API key entries in `.env` are migrated during setup (or at app
startup for manual upgrades) and removed from that file. Keep `.env` for the database connection URL and other
deployment-only runtime values. On reruns,
existing users and credentials are preserved, and legacy `ADMIN_USERNAME` and
`ADMIN_PASSWORD_HASH` entries are removed from `.env`. If accounts already exist, the
setup form is not shown.

For manual setup, `.venv/bin/python tools/first_time_setup.py --help` lists setup
options. The command starts PostgreSQL and applies `database_schema.sql`; the first
browser visit creates the administrator.

Agent assignments, activity, sharing, runtime metadata, and project workspace
files are persisted in PostgreSQL. On upgrade, Hearth imports legacy agent task
and project data from the old `data/agent-tasks` and `~/agents` locations, then
removes the migrated host copies. Active container working copies are runtime
scratch space; PostgreSQL is the durable source of truth.

After signing in, open **Agents**. Use **New agent** to name a task and describe what
you want in everyday language; advanced model and command approval settings are
optional. **My agents** shows task status. Open a task to follow its live **Activity**
feed, which streams task progress, the agent container's tool inputs and outputs,
and user-facing model responses as they become available, with timestamps. The
stream is scoped to tasks you own or that were explicitly shared with your account.
The result panel updates with the latest assistant response and final verification;
if a task is interrupted, its latest response remains visible alongside the error.
The activity feed and full activity history always put the newest timestamp at the top,
even when live events arrive out of order. You can answer questions
and pause or continue the work from that page. When an assignment is paused, blocked,
or interrupted, its owner can change the model before continuing. This starts a fresh
OpenCode session from the latest saved workspace checkpoint and carries forward the
previous agent summary. The model picker defaults to the currently loaded model when
available, while still allowing another configured model to be selected. Task data
remains in PostgreSQL if you close the browser.
Loaded support models such as embeddings do not take precedence over a loaded coding
model in that picker. LM Studio's loaded-instance data is included when detecting the
loaded coding model.
If OpenCode reports idle before its assistant result is available, Hearth continues
polling for up to 30 minutes. A session that stays idle is then marked **Needs review**
without repeated worker errors; resuming keeps the assignment's selected model.
Provider transport resets such as `ECONNRESET` retry automatically in the same
assignment session; a failed assistant response is retried as a new work cycle.
After reviewing a running, waiting, paused, blocked, or interrupted task, its owner can reset the cycle
counter to zero from the task page, then continue when ready. This keeps the saved
workspace and selected model.
Startup cleanup preserves containers with matching PostgreSQL task and runtime records.
If a container is missing, continuing rebuilds it from the saved project files.

Recurring schedules are optional and are not created by the installer. Open
**Schedules → Create a schedule** to save a task goal, recurring agent comment, model,
and interval. Daily or weekly schedules can specify a local run time; Hearth uses the
browser timezone shown in the form and keeps that wall-clock time across
daylight-saving changes. Without a selected time, the first run waits one full
interval. Schedules remain in PostgreSQL across restarts; if Hearth was stopped when
a run became due, the worker starts one catch-up run after startup and skips the
missed intervals. The standard systemd service starts this worker after reboot.
With `--no-service`, scheduled runs happen only while you manually run `.venv/bin/python app.py`.

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
browser test scripts directly against application tables. With `--browser`, it runs
the unit tests and all Playwright suites concurrently, each against its own temporary
schema.

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
