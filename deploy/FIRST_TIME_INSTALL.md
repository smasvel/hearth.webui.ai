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
- Creates the private `data/model-test-runs` directory for saved model-test reports.
  Reports are JSON files, separate from PostgreSQL, and can be deleted individually
  from **Model tests**, including while another report is being generated.
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
files are persisted in PostgreSQL, including shell tool status, inputs, outputs,
and completion timestamps. On upgrade, Hearth imports legacy agent task
and project data from the old `data/agent-tasks` and `~/agents` locations, then
removes the migrated host copies. Each task's Docker-backed runtime folder is
mounted at `/home/node/agents`; OpenCode works in its `project` subfolder, with
configuration and model relay files alongside it. PostgreSQL remains the durable
source of truth. OpenCode file tools are confined to the project folder and
external directories are denied. Agent project and OpenCode files do not use
`/tmp`; the container keeps it for system runtime needs.

After signing in, open **Agents**. Use **New agent** to name a task and describe what
you want in everyday language; advanced model and command approval settings are
optional. **My agents** shows task status. In automatic shell mode, allowed
in-container actions run without permission prompts; `.env` files, external
directories, and subagents remain denied. Ask mode pauses shell commands for your
decision. Open a task to follow its live **Activity** feed.
New assignments also accept a result label and platform/output type. Web-page
assignments preview a completed `index.html`, `dist/index.html`, or
`build/index.html` in a sandboxed frame on the task page.
feed, which streams task progress, the agent container's tool inputs and outputs,
and user-facing model responses as they become available, with timestamps on
agent messages and completed container tools. The
stream is scoped to tasks you own or that were explicitly shared with your account.
If a reconnect briefly returns no live messages, the page keeps showing the
transcript already saved with the assignment. The execution page continues
refreshing saved activity and conversation logs after live updates stop, including
for interrupted and completed tasks. The transcript is shown under **Conversation
and tool logs**.
The result panel updates with the latest assistant response and final verification;
if a task is interrupted, its latest response remains visible alongside the error.
The activity feed and full activity history always put the newest timestamp at the top,
even when live events arrive out of order. Questions and permission controls appear
on the assignment page when a response is needed. If it says **Needs your help** but
the request details have not loaded, use **Refresh request** in the notice. You can
also pause or continue the work from that page. When an assignment is paused, blocked,
or interrupted, its owner can change the model before continuing. This starts a fresh
OpenCode session from the latest saved workspace checkpoint and carries forward the
previous agent summary. The model picker defaults to the currently loaded model when
available, while still allowing another configured model to be selected. Task data
remains in PostgreSQL if you close the browser.
When an owner shares an assignment, shared users get the same task controls: they
can send instructions, answer prompts, pause or continue work, change the model when
resuming, rerun completed work, and delete the assignment. Only the owner can edit
the share list or revoke access. Shared users can access the assignment's project
files and download its completed project.
Completed assignments have a **Run again** action that queues a fresh execution from the
latest saved project files while retaining the assignment's settings and activity history.
Loaded support models such as embeddings do not take precedence over a loaded coding
model in that picker. LM Studio's loaded-instance data is included when detecting the
loaded coding model.
If OpenCode reports idle before its assistant result is available, Hearth continues
polling for up to 30 minutes. A session that stays idle is then marked **Needs review**
without repeated worker errors; resuming keeps the assignment's selected model.
Provider transport resets such as `ECONNRESET` retry automatically in the same
assignment session with backoff. A failed assistant response retries the same work
cycle, so provider resets do not use up the task's cycle budget. Existing relay sockets
are reused rather than replaced during concurrent checks.
Completion markers attached to future plans or implementation placeholders are
ignored; Hearth keeps the assignment running until the agent reports actual work.
After reviewing a running, waiting, paused, blocked, or interrupted task, its owner can reset the cycle
counter to zero from the task page, then continue when ready. This keeps the saved
workspace and selected model.
Startup cleanup preserves containers with matching PostgreSQL task and runtime records.
If a container is missing, continuing rebuilds it from the saved project files.

Recurring schedules are optional and are not created by the installer. Open
**Schedules → Create a schedule** to choose an existing agent project by name, then
**Schedules → Create a schedule** to choose an existing assignment from the
Assignments page, then set its
repeat interval and optional local run time. Each occurrence reruns that same
assignment with its saved goal, model, settings, and project files; no second agent
definition is created. Daily or weekly schedules use the browser timezone shown in
the form and keep that wall-clock time across daylight-saving changes. Without a
selected time, the first run waits one full interval. A run is skipped when its
assignment is still active or needs review, and checked again at the next interval.
Schedules remain in PostgreSQL across restarts; if Hearth was stopped when a run
became due, the worker starts one catch-up run after startup and skips the missed
intervals. The standard systemd service starts this worker after reboot.
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
Model loading and lifecycle controls are on **Models**. Open **Model tests** from
the navigation, choose an installed chat model, and run its checks individually.
Monitor compatibility repairs and review saved reports there. Completed older
reports can be deleted while another test is running; the active report stays
protected. The **Test tools** and **Reset repair** actions on model cards open that
dedicated page. The JSON reports live in `data/model-test-runs`, so back up that
directory separately from PostgreSQL if you want to retain them across migrations.
On the **Models** page, Lemonade model cards include an **Enable thinking** checkbox. It
sets the per-model reasoning preference for Hearth chat, overriding the global Lemonade
default in Settings. Hearth sends the preference through Lemonade's request control and
chat-template option. Models whose runtime does not support thinking controls may ignore it.

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
