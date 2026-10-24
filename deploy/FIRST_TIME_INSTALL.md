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
  per-user agent workflow setting and the agent task, schedule, runtime metadata,
  and private workspace tables; setup applies idempotent account and agent-store
  migrations.
- Prompts twice for the initial administrator password. Hearth stores only its
  password hash; the plaintext password is not written to configuration.
- Creates and enables `~/.config/systemd/user/ai-webui.service`, using this checkout's
  absolute path. Existing customized service files are preserved.
- Enables systemd lingering for the installing account, so Hearth starts after a
  machine restart even before that account logs in.
- Starts Hearth's persistent agent worker with the service. The worker checks
  PostgreSQL for due schedules while Hearth is running.

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
The newest activity and conversation entries appear first. You can answer questions
and pause or continue the work from that page. When an assignment is paused, blocked,
or interrupted, its owner can change the model before continuing. This starts a fresh
OpenCode session from the latest saved workspace checkpoint and carries forward the
previous agent summary. Task data remains in PostgreSQL if you close the browser.

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
