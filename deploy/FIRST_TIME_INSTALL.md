# First-time installation

## Windows, macOS, and Linux release bundles

Published GitHub releases include platform-labelled bundles for Windows, macOS,
and Linux. These bundles install Hearth from source and ask which supported model
backend endpoint to configure. Install Python 3.12+ and Docker Desktop/Engine
before running the bundle's installer. On Windows run
`deploy/install-release.ps1`; on macOS and Linux run
`bash deploy/install-release.sh`. The installer prepares Hearth's Python
environment and PostgreSQL database, then stores the selected endpoint and
optional API key in Settings. Linux setup also builds the isolated coding-agent
image; agent execution is Linux-only. The model runtime and model weights are
separate downloads and must be installed and started independently.
Open **Downloads** to find `Ornith-1.5-9B-GGUF` preselected in the Lemonade search.
Review its result and choose Download to install the model; setup does not download
model weights automatically. On Downloads, use **Exclude unknown support** to hide
search results whose catalog metadata does not say whether they support tool calling;
the per-backend tool-calling filters remain available when you want only models that
explicitly declare support.
You can also choose **Upload a local GGUF model** on Models to browse for a `.gguf`
file on the device using the browser. Hearth stores the upload in `data/local-models`
and registers its local path with the Lemonade server configured in Settings. The
Lemonade server must run on the Hearth host or have permission to read that path, and
the Hearth host needs enough free disk space for the complete model file. Uploaded
models appear in **Models** for loading. This upload flow currently supports GGUF files
for Lemonade's `llamacpp` recipe.

In Chat, if regenerating a reply fails, the previous reply remains visible in a distinct
dimmed bubble labeled with the failure. A successful regeneration replaces it normally.
The message editor uses the full chat width, with a separate wrapping toolbar for
writing, attachments, voice, translation, and Send. Drag the editor's resize handle
for more space; longer messages grow it up to a viewport-based height limit and
then scroll within the editor. All controls remain available on narrow screens.
No additional setup is needed; refresh Chat after updating to load the new layout.
Use the paperclip beside the chat composer to attach up to five PDF, DOCX, PPTX, XLSX,
TXT, or CSV files (15 MB each). Hearth extracts their text and includes it in the saved
chat turn, so you can ask questions about the documents and follow up in that conversation.
Scanned PDFs without selectable text need OCR before attachment. Installations update the
PDF reader dependency automatically with the normal requirements installation.

After setup, start the app with `deploy/start-hearth.ps1` on Windows or
`bash deploy/start-hearth.sh` on macOS/Linux. Open `https://localhost:5443` on the
Hearth computer and create the administrator account on first visit. For a Mac laptop
outside the host's Wi-Fi, connect both devices to your tailnet and open **Help → Connect
over Tailscale → Chat**. This trusted HTTPS link appears after Tailscale Serve is configured
on the Hearth host; the startup output prints it when detected. To configure a private web
route on the host, install and sign in to Tailscale, then run:

```bash
tailscale serve --bg --https=8443 https+insecure://127.0.0.1:5443
```

Keep Tailscale connected on the Mac as well. The Serve URL is private to devices allowed
on your tailnet. `localhost` only identifies the computer where the browser is running.
These launchers run Hearth in
the foreground; close the terminal to stop the app. Docker keeps PostgreSQL
running in the background. In Chat, **Copy chat** copies all user messages and assistant
responses in the current conversation as plain text. Each user message and assistant response
also has its own copy control. Opening the site root (`/`) sends signed-out visitors to
`/login`, then returns signed-in users to the workspace; chat navigation links return to
that root workspace route. The sign-in and initial
administrator setup screens use a responsive AI workspace design with the credentials
form centered in the viewport on desktop and mobile, framed by six animated robot
minions with blinking faces, waving arms, glowing thrusters, task labels, moving route
lights, and connecting paths. A **Watch the 25-second demo** button opens a centered player
and starts the product demo with sound; visitors can pause playback or close the player. The crew
stays around the form on small screens and fills
the full background on larger screens. The animation respects the browser's reduced-motion
preference. The same
charcoal, slate, and mint color theme is used throughout Hearth's pages and controls.
Preview the [animated sign-in screen](../docs/screenshots/login-animation.gif) or view
the [login screenshot](../docs/screenshots/login.png).

## One-command Ubuntu installation

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
  agent data, model metadata, model-test reports, and private workspace tables. Setup
  applies the account, settings, secret, agent-store, and model-metadata migrations.
- Imports legacy model-test reports and KoboldCPP/vLLM registries into PostgreSQL,
  seeds reviewed model resource notes into the database, and removes the old JSON files
  and report directory before starting the service.
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

Agent assignments, activity, sharing, runtime metadata, and per-assignment project
workspace files are persisted in PostgreSQL, including shell tool status, inputs,
outputs, and completion timestamps. Scheduled runs continue from the assignment's
saved code and previous progress summary. The workspace remains stored between runs
and is deleted with its assignment. Existing unscoped workspace files move to their
assignment when ownership is unambiguous; otherwise, they remain a legacy fallback
until each assignment saves its own checkpoint. On upgrade, Hearth imports legacy agent task
and project data from the old `data/agent-tasks` and `~/agents` locations, then
removes the migrated host copies. Each task's Docker-backed runtime folder is
mounted at `/home/node/agents`; OpenCode works in its `project` subfolder, with
configuration and model relay files alongside it. PostgreSQL remains the durable
source of truth. OpenCode file tools are confined to the project folder and
external directories are denied. Agent project and OpenCode files do not use
`/tmp`; the container keeps it for system runtime needs.
Agents record verified, reusable environment and project facts in the project-root
`AGENT_MEMORY.md`, including available interpreter names, dependency setup, working
verification commands, and project conventions. Hearth combines local embedding
similarity with exact keyword matching when it retrieves notes for later private
assignments owned by the same account; a query for `python` also matches a note that
records `python3` as the available interpreter. Shared assignments do not retrieve notes from
other assignments. Retrieval counts and match scores appear in Activity. Agents check
whether recalled facts still apply and are instructed not to save credentials, personal
data, or temporary task state. The note file is part of its assignment workspace; its
search index is deleted with the assignment or user. If embeddings are unavailable,
Hearth keeps notes available for exact-term search and retries semantic indexing later.
No separate service or setup step is required.

Hearth chat exposes the configured Playwright browser and persistent Python MCP
tools on KoboldCPP, FastFlowLM, SGLang, Lemonade, and LM Studio. The MCP servers
start the first time chat needs them. Tool-enabled requests use Hearth's local
backend compatibility shims; keep the `lemonade-guard-shim`, `lmstudio-shim`, and
`flm-tool-shim` user services running for those backends. Ask the model to use the
browser for a specific page or task when you want a Playwright action. Hearth chat
offers a compact browser tool set and trims oversized FastFlowLM browser results so
they fit more reliably within smaller model context windows. After navigation and
page-changing actions, Hearth waits for content, lists and visits each open tab
(including popups), captures snapshots, and returns an explicit confirmation to the
model. The model can continue to inspect, find text, click, or navigate back.

After signing in, open **Agents**. Use **New agent** to enter a required agent name,
name the task, and describe what you want in everyday language; the agent name identifies
its model requests in **Live logs**. Advanced model and command approval settings are
optional. **More options** lets you set the OpenCode step limit before launch
(default 40, range 1–10,000), separately from the assignment work-cycle budget. You can
edit this limit on a paused, blocked, stopped, completed, or interrupted task; continuing a
blocked or interrupted task with a changed limit starts a fresh OpenCode session from
its saved project files.
Hearth project agents use OpenCode's direct file and shell tools. Shell commands still
follow the task's **Ask me first** or **Allow automatically** approval mode.
Transient provider connection resets are retried automatically in the same task cycle.
The model relay avoids sending a second HTTP response after a streamed response has begun.
The agent label is used for Hearth's request attribution and is removed by compatibility
shims before requests are forwarded to model servers.
**My agents** shows task status. In automatic shell mode, allowed
in-container actions run without permission prompts; `.env` files, external
directories, and subagents remain denied. Ask mode pauses shell commands for your
decision. Open a task to follow its live **Activity** feed.
Agent responses and structured tool inputs, outputs, and errors appear as readable
key/value text instead of raw JSON; shell inputs are shown as commands.
Hearth fetches OpenCode activity, prompts, permissions, inbox, and recent messages
concurrently, reducing the delay between a response and the task status update.
While waiting for a first response token, Activity reports the elapsed wait every
30 seconds. If OpenCode remains active without a streaming model request, Activity
reports that state too; these notes do not extend the recovery timeout. Progress is
matched to the assignment, so agents with the same name cannot hide each other's
stalls. The relay forwards SSE lines immediately and counts tool-call argument deltas
as progress. After updating, restart `ai-webui.service` to load the supervisor and
relay fixes. These changes do not require rebuilding the agent image.
The task's thinking choice is applied when its container is created. Continuing a
task with a different saved thinking setting starts a fresh session from its saved
workspace so the requested setting takes effect.
Container reuse preserves host ownership of the task workspace root directory.
When OpenCode has saved a completed assistant response, Hearth processes it even
if OpenCode's session-wide activity indicator has not caught up yet or a prompt is
stranded in the inbox. An unfinished assistant message or inbox entry does not count
as work: after 90 seconds without a model request or running project tool, Hearth
interrupts the abandoned runner, forces a current PostgreSQL checkpoint, retires its
container, and starts a fresh session. The original goal, acceptance criteria, and
refinements are carried forward without advancing the work-cycle counter.
Real model requests have a 10-minute first-token limit and a 5-minute inactivity limit
after output starts. Current token and tool progress is checked before these limits,
even when OpenCode reports idle. After two unsuccessful fresh-session recoveries,
the task moves to **Needs review** and can be continued from its saved workspace.
Three consecutive responses without project tool calls also trigger recovery, to
prevent repeated implementation text from replacing actual file changes.
Conversation and tool logs include tool error details when OpenCode provides them.
During long model turns, Activity also reports the number of tokens
received from the local model. Shell tool inputs appear as commands in the conversation
log. If the selected model is already loaded with at least 16K context, Hearth reuses
it instead of unloading and loading it again.
If OpenCode interrupts a response, Hearth continues from the saved project without
advancing the work-cycle counter. After three consecutive interrupted responses, the
assignment moves to **Needs review**. When a parked container starts again, Hearth
refreshes its OpenCode service address before reconnecting. Agents are instructed to
keep work inside the project workspace and use available tools and dependencies.
When resuming an interrupted or blocked session, Hearth restores the PostgreSQL
workspace checkpoint into the existing container before continuing the agent run.
On phones, the Agents workflow tabs stay visible while scrolling, and the header
shows all workspace page tabs with Agents selected, reflowed into a grid so every
tab stays visible on narrow screens.
When editing a task, its saved model stays selected even if it is missing from the
current model list, cannot load, or falls below the worker's 16K context requirement.
You can save other task edits while retaining that model; choosing a different model
requires one currently available. Model matching is independent of JSON property order.
Choose **Clone** beside an assignment to open a new task form prefilled with its
prompt and settings. Review the copied values before starting. The cloned assignment
uses a new project workspace and does not copy project files, activity, sharing, or
schedules.
New assignments also accept a result label and platform/output type. Web-page
assignments preview a completed `index.html`, `dist/index.html`, or
`build/index.html` in a sandboxed frame on the task page. Choose **JSON results
(HTML view)** to show a saved JSON artifact or accepted final response in a separate,
labeled sandboxed report view.
The latest agent response and labeled HTML result appear at the top of the task page,
before task details, controls, activity, and conversation logs.
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
even when live events arrive out of order. While an agent runs, the feed also reports
tool starts and completions and periodically confirms the OpenCode session is active
during long model turns; these notes do not include prompts or tool output. Questions
and permission controls appear
on the assignment page when a response is needed. If it says **Needs your help** but
the request details have not loaded, use **Refresh request** in the notice. You can
also pause or continue the work from that page. When an assignment is paused, blocked,
or interrupted, its owner can change the model before continuing. This starts a fresh
OpenCode session from the latest saved workspace checkpoint and carries forward the
previous agent summary. The model picker defaults to the currently loaded model when
available, while still allowing another model from Hearth's current downloaded-model
inventory to be selected. This includes unloaded models; Hearth loads the selected model
before the agent starts. Task data
remains in PostgreSQL if you close the browser.
When an owner shares an assignment, shared users get the same task controls: they
can send instructions, answer prompts, pause or continue work, change the model when
resuming, rerun completed work, delete the assignment, and manage who else can access
it. Anyone with assignment access can edit the share list; the owner retains ownership.
Shared users can access the assignment's project files and download its completed
project. They can also create, edit, pause, resume,
and delete recurring schedules linked to assignments shared with them.
Completed and stopped assignments have a **Run again** action that queues a fresh
execution from the latest saved project files while retaining the assignment's settings
and activity history.
Loaded support models such as embeddings do not take precedence over a loaded coding
model in that picker. LM Studio's loaded-instance data is included when detecting the
loaded coding model.
If OpenCode reports idle before its assistant result is available, Hearth uses the
90-second handoff check described above. Only real model requests or running tools
extend that check; status messages do not. OpenCode's **CRITICAL - MAXIMUM STEPS REACHED** response
also marks the assignment **Needs review** even if the session still reports active; use
**Continue** after reviewing the saved work to resume it. Resuming keeps the assignment's
selected model. If OpenCode does not acknowledge a requested pause or stop, Hearth marks
the assignment **Interrupted** (or **Needs review** after a worker failure) and enables
**Continue** so the saved work remains recoverable.
Provider transport resets such as `ECONNRESET` retry up to three times in the same
assignment session with backoff. Repeated response failures trigger bounded fresh-session
recovery; repeated polling or prompt-admission failures move the task to **Needs review**. A failed assistant response retries the same work
cycle, so provider resets do not use up the task's cycle budget. Existing relay sockets
are reused rather than replaced during concurrent checks. On Hearth startup, relay
listeners for saved agent sessions are restored before workers run, reconnecting
persistent Docker containers to the host model bridge after an app restart.
If OpenCode conversation compaction fails, Hearth retries in a fresh session from the
saved project checkpoint without advancing the work-cycle counter.
For project agents, Hearth also caps retained recent turns at one eighth of the
selected model's context and starts compaction at least one eighth before the context
limit, reducing the chance that a summary exceeds the model's output limit.
Completion markers attached to future plans or implementation placeholders are
ignored; Hearth keeps the assignment running until the agent reports actual work.
After reviewing a running, waiting, paused, blocked, or interrupted task, its owner can reset the cycle
counter to zero from the task page, then continue when ready. This keeps the saved
workspace and selected model.
Startup cleanup preserves containers with matching PostgreSQL task and runtime records.
Test runs use a private agent runtime directory so they cannot remove live assignment
containers. If a container disappears during work, Hearth rebuilds it from the latest
synchronized project checkpoint and continues the assignment.
Assignments already running when Hearth restarts are marked interrupted and can be
resumed from the task page. Assignments that were still queued remain queued and start
when the worker is ready.
After an update changes the agent worker's tool policy, continuing an older task starts
a fresh OpenCode session from its latest saved project checkpoint.
Completed, stopped, or interrupted assignments can be edited with **Edit task** in the task header
and resubmitted.
**Save changes** updates the task without starting a run. **Resubmit** saves the
current edits and queues the same assignment, keeping its saved project files and
activity history.
When a verification command is configured, Hearth includes it in the initial agent
plan and asks the agent to create and save any missing verifier scripts, fixtures,
or configuration it needs, then run and repair the command before claiming completion.
Hearth independently executes the command after the completion claim.
After a run finishes, Hearth stops but retains its Docker container. A rerun reuses
that container when the model and provider settings still match, while syncing the
saved PostgreSQL workspace into it. Deleting the assignment removes the container;
changing its model or provider configuration creates a replacement.

Recurring schedules are optional and are not created by the installer. Open
**Schedules → Create a schedule** to choose an existing agent project by name, then
**Schedules → Create a schedule** to choose an existing assignment from the
Assignments page, then set its
repeat interval and optional local run time. Each occurrence reruns that same
assignment from its saved code, project files, progress summary, goal, model, and
settings; completed parts are reused and no second agent definition is created.
Daily or weekly schedules use the browser timezone shown in
the form and keep that wall-clock time across daylight-saving changes. Completed
and stopped assignments can both be rerun by a schedule, and Hearth recovers a
scheduled run interrupted by a service restart. Without a
selected time, the first run waits one full interval. A run is skipped when its
assignment is still active or needs review, and checked again at the next interval.
Schedules remain in PostgreSQL across restarts; if Hearth was stopped when a run
became due, the worker starts one catch-up run after startup and skips the missed
intervals. The standard systemd service starts this worker after reboot.
On the **Schedules** page, edit an existing schedule to change its interval, local
run time, or timezone. Shared users can manage schedules for assignments shared with
them.
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

Open **https://localhost:5443** on the Hearth computer. On first startup Hearth
generates a self-signed certificate, so accept the local browser certificate warning.
For access from a Mac outside the host's Wi-Fi, connect both devices to Tailscale, configure
Tailscale Serve on the host as described above, then open **Help → Connect over Tailscale →
Chat** and sign in. Sign in with the
administrator credentials created above.

Install and start at least one supported model runtime, then configure its URL
and credentials under Settings. LM Studio's default endpoint is
`http://localhost:1234`; Lemonade's is `http://localhost:13305`. Download a model
and load it from Models. PostgreSQL setup does not install model weights.
Model loading and lifecycle controls are on **Models**. Deletion is blocked only for
a model currently assigned to active agent work or included in an active model test;
other offline models can be deleted while that work continues. For LM Studio, Hearth
resolves model aliases through the local installed-model index and verifies the model entry
under its models directory. Files may link into the Hugging Face cache; Hearth removes those
links without traversing their targets. Failed loads appear on their model cards and refresh
across open Models pages every five seconds. Hearth clears the current error after a
successful load and keeps its reviewed model warning dismissed until a later load attempt
fails. Open **Model tests** from
the navigation, choose an installed chat model, and run its checks individually.
The page has tabs for compatibility, tool calling, model reasoning, and model
security behavior. The reasoning tab asks 12 original questions one at a time and
scores points correct; it is a comparison benchmark, not a human IQ score. The
security tab runs 10 safe behavioral probes and reports how the model handles them;
it does not scan the host or certify security. Test prompts are sent individually,
and generated tools/actions are not executed. Results are saved as model-test reports.
Monitor compatibility repairs and review saved reports there. Completed older
reports can be deleted while another test is running; the active report stays
protected. **Test Model** on a model card starts a test of that card's model;
automatic repair stays off for this run. **Reset repair** opens that page as well.
Failed workflow steps mark dependent checks as blocked; only checks
that ran are included in repair comparisons. Automatic repair is optional and is
off by default. The Lemonade proxy translates named-function choices into a required
call with only the requested function offered. If the user message supplies exact
arguments for that function, Hearth preserves the requested call when the model
returns prose or another function. It also converts supported model-native tool markup to
structured calls only for functions offered in that request. An instruction not to
repeat a question does not suppress a later explicit tool action in the same turn. The
FastFlowLM `minicpm-v-4.7:1b` path has its own tool-use reminder and exact named-call
handling. By default, it can perform up to six tool rounds for multi-step browser tasks, then
requests a text-only summary of the results; other FastFlowLM models use their
existing behavior. Administrators can set Hearth chat's maximum tool-call retries
(rounds) per response under Settings (1–64, default 6); changes apply to the next chat request.
When browser tools are available, Hearth adds their usage guidance to the leading system
message and combines system instructions into one leading message for chat templates that
require system instructions first.
Model registries, test reports, compatibility profiles, and reviewed
resource notes are stored in PostgreSQL. A one-time startup migration imports existing
model-test reports and KoboldCPP/vLLM registry files, then removes those legacy files.
Back up PostgreSQL to preserve this metadata. Test reports can still be downloaded as JSON.
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
the unit tests and all seven Playwright suites concurrently, each against its own temporary
schema and a private agent runtime directory, so test startup cannot remove containers
owned by the running application. The admin browser coverage visits every standalone page, exercises key page actions,
opens a Live logs request trace, checks Browse history search and deletion, and verifies
that ordinary users cannot open admin-only pages or APIs.

Administrators can inspect request metrics on **Metrics** and open **Live logs** for
streaming request progress, errors, and request/response traces. The live feed polls
automatically, shows the latest 100 requests, and loads trace text only when opened.
Completed rows remain available for two days. Project-agent model requests appear with
the task's agent name through Hearth's model relay. No additional setup is required.
Administrators can use **Reset all metrics** to erase saved request history and coding
reviews after two confirmation warnings; subsequent requests are recorded normally.
The **Live logs** page also has **Reset live logs**, which clears completed history and
captured request/response traces after two confirmation warnings. In-flight rows remain
visible and their completed entries are recorded after they finish.
Rejected agent relay chat requests appear as failed 403 entries labeled with the agent
name and model; Hearth does not read or save their prompt body.
For streamed chat requests, Hearth asks Lemonade, FastFlowLM, and LM Studio for token
usage and records generated-token counts when the backend supplies them. Historical rows
created without usage counts remain unknown.
Metrics tables scroll horizontally inside their panels on phones. The model report uses
fixed shared column widths to keep headers aligned with their values; dashboard cards
and filters fit the available screen width.
Agent HTML result previews also work when workspace synchronization stores the entry
file path with a leading `./`.

Model validation reports accept spelled-out test counts and common Markdown
emphasis. The suite checks parallel calls separately from OpenCode's multi-file
read continuation scenario, so a model can report independent results for each
behavior. Automatic compatibility repair is off by default, including when a
test run starts from a model card. **Test Model** tests the model from that card.
The Model tests dropdown also selects Ornith by default when available, and allows
choosing another installed model. Select the repair option on the Model tests
page to enable repairs for a run.

Administrators can open **Browse history** to search and page through browser visits.
Delete individual entries from their rows or clear the entire history with **Delete all
history**; both actions ask for confirmation.

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

Agent assignments provide dedicated fields for technical requirements, persistent
memory notes, and HTML result requirements. For HTML output, use the accepted
format `TITLE:`, `SECTIONS:`, `COMPONENTS:`, `INTERACTIONS:`, and
`ACCESSIBILITY:`. These requirements are sent with each agent prompt. Technical
requirements and memory notes are also included in the account-scoped agent RAG
context and durable project-memory index. The task activity view streams in a
terminal-style layout while completed HTML results remain available in the preview.
