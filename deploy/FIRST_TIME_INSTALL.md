# First-time installation

The Lemonade gateway removes disabled tool schemas for `tool_choice: none`, avoiding
native parser failures on ordinary answers. Restart `lemonade-guard-shim.service`
after updating the gateway code to apply this behavior.

Lemonade's `Ornith-1.5-35B-A3B-GGUF` and `K2-Horizon-MoVA-36B-A4B-GGUF` use the CPU backend when Hearth detects an
AMD shared GPU memory pool below 32 GiB. This avoids Vulkan memory exhaustion at
load time, at the cost of slower replies. Restart Hearth and
`lemonade-guard-shim.service` after updating, then reload the model from Models.
The 9B variant and devices with larger GPU pools keep their default backend.

Hearth checks other backends before switching reasoning models, including when
loading a Lemonade model such as `Ornith-1.5-35B-A3B-GGUF`. If a load reports that
LM Studio cannot be reached, start its configured API server (for a local
installation, `bash deploy/ensure-lmstudio-server.sh` or `lms server start --port 1234`)
and retry from Models. On Linux, `lmstudio.service` can appear active while the API
server has stopped; the system `lmstudio-server.service` user unit installed by Hearth
keeps both processes healthy at boot, so only a manual LM Studio restart should ever put
them out of sync. Confirm the configured endpoint responds instead of relying on
service status alone.

## Windows, macOS, and Linux release bundles

Published GitHub releases include platform-labelled bundles for Windows, macOS,
and Linux. These bundles install Hearth from source and ask which supported model
backend endpoint to configure. Install Python 3.12+ and Docker Desktop/Engine
before running the bundle's installer. On Windows run
`deploy/install-release.ps1`; on macOS and Linux run
`bash deploy/install-release.sh`. The installer prepares Hearth's Python
environment and PostgreSQL database, then stores the selected endpoint and
optional API key in Settings. Linux setup also builds the isolated coding-agent
image; agent execution is Linux-only. Setup offers optional runtime installation;
model weights remain separate downloads. Use `--install-backends lmstudio lemonade`
for unattended Ubuntu setup or `--skip-backend-install` to preserve an existing or
remote runtime.
Open **Downloads** with backend search fields empty. Catalogs and suggestions that support
browsing load automatically; search for a model to narrow results or search backends that
require a query. Choose Download to install a model. During first-time setup, the installer
can separately offer to download `Ornith-1.5-9B-GGUF` before starting Hearth when Lemonade
is reachable. On Downloads, use **Exclude unknown support** to hide
search results whose catalog metadata does not say whether they support tool calling;
the per-backend tool-calling filters remain available when you want only models that
explicitly declare support.
When loading a model, Hearth checks whether currently available RAM can fit the new
model's known weight size plus 1 GB headroom. If it fits, the currently loaded model
stays resident; otherwise Hearth evicts it before loading. Backend-specific model-size
checks and runtime limits still apply. No extra setting is required.
A cold load of a large model (especially a large MoE GGUF) can briefly return `500` from
Lemonade's `/v1/load` endpoint while the backend process is spawning, or a connection error
while it is being evicted. Hearth retries such transient load failures a bounded number of
times with backoff before reporting them, so you usually do not need to retry by hand. A
permanent rejection — an unknown model, or a load refused because it does not fit in RAM —
is not retried and is reported immediately. If a load still fails after the automatic
retries, check that enough memory is available and retry from Models.
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
Attachments are submitted with the chat request, so the extracted document text is
published to the selected agent/model and retained in that conversation. If extraction
fails, Hearth shows the error and does not start the turn; retry after correcting the
file or converting it to a supported text-bearing format.

Live chat displays separate reasoning streams from backends that expose them. Hearth
normalizes the reasoning fields used by KoboldCpp and OpenCode for both the live panel
and saved conversation history; models that do not emit a separate reasoning field show
their answer without a thinking panel.

Chat applies a final context-size guard after adding saved history, attachments, and
web-research results. Cross-chat memory recall treats personal facts specially: the
user's name and home location, once stated in any past conversation ("My name is …",
"call me …", "my city is …"), are re-injected as an explicit known-fact directive so
even small local models (verified with KoboldCPP Qwen3.8-2B) answer "what's my name?"
correctly instead of claiming they were never told, and on a name question only the
name fact is recalled — unrelated memory snippets are withheld so small models don't
answer with the wrong fact. This also keeps working when the
Lemonade embedding model is briefly unavailable. These explicit facts are recovered
from saved chat messages even if embeddings were unavailable when the conversation was
stored. The recall note is placed directly
beside the user's latest message — clearly labeled as Hearth memory — rather than in
the system prompt, because small models ignore facts buried there; the saved chat
history keeps the original message text. It uses the selected model's active/configured maximum context
when the backend reports one, reserves space for the reply, and keeps system instructions
and newest turns while omitting older turns when necessary. When the requested output
budget exceeds the available window, Hearth caps `max_tokens` to the space left after
a prompt reserve; a vLLM model loaded at 2,048 tokens therefore no longer rejects the
default 32,768-token request. vLLM's live `/v1/models` `max_model_len` is honored even
when the model was started outside Hearth and is not yet in Hearth's registry. If model
metadata is temporarily unavailable, it uses a conservative fallback. This avoids backend
context-overflow errors.
The local model proxies also preserve the blank-line delimiters required by streamed SSE
responses, so OpenAI-compatible clients can receive tool and answer events reliably.
No additional setup is required for Lemonade embeddings. To enable separate cross-chat
vector retrieval for KoboldCpp chats, install or select an embedding GGUF and add
`--embeddingsmodel /absolute/path/to/embedding-model.gguf` to the KoboldCpp service's
startup arguments, then restart `kobold.service`. KoboldCpp must be able to read that
file. This setting affects only KoboldCpp: Hearth stores and searches its vectors in a
separate namespace, and every other backend continues using Lemonade embeddings.
Previously indexed Lemonade vectors stay available to other backends. If the KoboldCpp
embeddings endpoint is unavailable, Hearth falls back to Lemonade embeddings for that
KoboldCpp conversation, preserving RAG while the optional sidecar is offline. Once the
sidecar responds, KoboldCpp uses its own vector namespace. Start a new conversation when
older context is essential.

Generated OpenCode configurations enable automatic compaction by default so older turns
are summarized before the loaded context fills. Settings can disable it or change retained
history and buffer. After changing a model's loading ctx-size, copy the new LAN or Tailscale
configuration from Help and restart OpenCode, or run `python3 tools/sync_opencode_config.py`
to refresh the desktop configuration. The generated limit uses the actual served context
for a loaded model; a saved larger maximum applies only after the model is reloaded. Continue
for VS Code receives each model's advertised loaded context, while its own settings control
compaction. OpenCode requests routed through Hearth's Lemonade relay load an unloaded model
with its saved Models-page ctx-size, and reapply that size if the model is already loaded
with a smaller context. Every client remains subject to the model's actual loaded limit.
Search all backends on Downloads checks the latest public Lemonade catalog and LM
Studio's model catalog as well as the backend and Hugging Face searches. These public
matches require internet access; new Lemonade GGUF entries download through Hugging
Face when the installed Lemonade catalog does not contain them.
For Lemonade downloads, a typed search also queries Hugging Face for current GGUF
repositories, including recent releases not yet listed in Lemonade's catalog. Duplicate
catalog/search hits for the same checkpoint are combined without hiding distinct
quantizations. Cached checkpoints show as downloaded using Lemonade's required-file
status and shared cache, even when its catalog download flag is stale.
The Help page's generated OpenCode config uses a 65,536-token context limit for models
supporting at least that maximum, and 8,192 tokens otherwise. The Models page's model
loading ctx-size is a separate load setting.
Loaded models show a separate **loaded ctx-size** line with the runtime's active context;
unloaded models do not show this line.
Hearth retries a failed compaction in one fresh session per assignment. If compaction fails
again, the assignment stops for review; load a larger supported context or model before continuing.

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
The authenticated Caddy model gateways also listen on the detected Tailscale IP. If
Tailscale was connected after Caddy was configured, refresh the gateway site addresses
without changing client tokens:

```bash
sudo "$(pwd)/.venv/bin/python" tools/caddy_gateway_setup.py \
  --state-file "$HOME/.local/share/hearth/Caddyfile" \
  --owner "$USER" --system-file /etc/caddy/Caddyfile
```

These launchers run Hearth in
the foreground; close the terminal to stop the app. Docker keeps PostgreSQL
running in the background. In Chat, **Copy chat** copies all user messages and assistant
responses in the current conversation as plain text. Each user message and assistant response
also has its own copy control. Opening the site root (`/`) sends signed-out visitors to
`/login`, then returns signed-in users to the workspace; chat navigation links return to
that root workspace route. The post-sign-in redirect stays instant even when a backend is
slow: the workspace page's help and network data is served from a server-side snapshot
warmed at startup and refreshed in the background, rather than rebuilt from live
backend, Tailscale, and Caddy probes on every sign-in. The sign-in and initial
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
PostgreSQL, and isolated agent build image. Model runtimes can be installed during
setup. When Lemonade is reachable, it offers the recommended model download before
starting Hearth, after checking catalog size, free home-filesystem space, and available
RAM. The prompt includes a transfer-time example at 25 Mbps so you can judge your
connection. Confirm with `y` to download; press Enter to skip. Use
`--skip-model-download` for unattended setup. If the model is already downloaded,
the installer reuses it. A missing runtime, catalog entry, or insufficient resources
skips the download and leaves the model available through Downloads later.
Use an ordinary account with sudo access and a graphical or terminal login session.

## Install and initialize Hearth

```bash
bash deploy/install-first-time.sh
```

The installer uses `sudo` to install missing Ubuntu host dependencies, starts
Docker, and grants the current user access. It reuses an existing `.venv` when
one is already present and only installs the `python3-venv` apt package when the
project does not already have a usable venv. It creates `.venv` when missing,
installs Python requirements and Chromium, builds the isolated agent image with
Python, Java/Maven, Node.js/npm, and build tools, and then:

The Python requirements include `pypdf` for PDF attachments. The installer checks
that PDF reader imports successfully before continuing; DOCX, PPTX, XLSX, TXT, and
CSV attachment extraction is built into Hearth.

- Generates a random PostgreSQL password in `.env.postgres` and a matching
  `DATABASE_URL` in `.env`, both with mode `600`.
- Setup generates or imports the session-signing key and stores it encrypted in
  PostgreSQL before starting the app; direct app starts do the same on first launch.
- Starts PostgreSQL 18 with Compose, waiting for its readiness check.
- Loads the application DDL from `hearth/storage/database_schema.sql` into the new PostgreSQL
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
- When Caddy is already installed, generates the model gateway bearer tokens and local
  CA, and publishes the managed gateway block through passwordless sudo when available
  (otherwise it prints the exact `sudo` command).

Existing credentials, users, and database contents are preserved on reruns. The basic
installer does not install model backends, Tailscale, or Caddy; the full host backend
setup (`sudo bash deploy/install-host-deps.sh`) installs Caddy and generates the model
gateway keys and local CA.
Account profile fields (username, role, creation time, gateway token, and agent access)
are stored in PostgreSQL. Passwords use salted, one-way Werkzeug hashes; Hearth never
stores plaintext passwords or returns password hashes through user APIs. Hashing is
intentional because passwords should be verified, not reversibly encrypted.
The encrypted Flask session key is stored in PostgreSQL as well. Its encryption key is
derived from the PostgreSQL connection password, so changing that database password
rotates the session key and requires users to sign in again. The session cookie is named
after the listen port (`hearth_session_5443`, `hearth_session_5444`, and so on), because
browsers share cookies across ports on the same host. Several Hearth installations on
one machine can therefore be signed in at the same time without their logins
overwriting each other; changing a port starts a new cookie and requires one new login.

Useful options:

```bash
bash deploy/install-first-time.sh --skip-browser --no-service
bash deploy/install-first-time.sh --skip-backend-broker --skip-lmstudio-server
bash deploy/install-first-time.sh --skip-model-download
.venv/bin/python tools/first_time_setup.py --help
```

Before any Git push from this checkout, run
`git config --local core.hooksPath .githooks` once to enable the versioned pre-push
secret check. It blocks pushes containing credentials, private keys, private IP
addresses, or tailnet names in files or commit history. To audit the entire local
history and working tree, run
`python3 tools/check_push_secrets.py --all-history --working-tree`. Resolve any
findings before pushing and do not use `git push --no-verify`.
Placeholder exceptions apply to credential values, and sensitive filenames are
checked in test files too.

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

## Shared admin data

Model test runs and performance metrics are stored in PostgreSQL and shared across all admin users. Any admin can view, cancel, or delete test runs started by another admin. The metrics page aggregates data from all users.

## Model test report

The metrics page compares each supported local model's latest comprehensive test run (All tests or Complete) from the full saved history; narrow suites do not replace it, and saved reports are not automatically trimmed to the newest 30. Hosted OpenCode catalog results are excluded from this report, including historical saved runs. It shows pass/fail/skip counts, pass rate (skips excluded), reasoning benchmark score, generated tokens per second, generated tokens per test, and average test time. Recommendation is quality-first: pass rate is weighted at 70%, reasoning benchmark at 20%, speed at 5%, and lower token use at 5%; values are scaled against the other models, with available dimensions reweighted. The reasoning score uses the 12-question benchmark when present and is not a human IQ score. Token, speed, and reasoning fields show as unavailable when a run did not record those results. On narrow screens, scroll the report horizontally; its 12 columns stay aligned with the headers.

## Backend restarts and service tracking

Hearth tracks backend and service lifecycle in PostgreSQL:

- **Selective backend restarts**: `POST /api/admin/restart-backends` accepts a `backends` list (e.g. `["Lemonade", "vLLM"]`) and a `preserve_mandatory` flag. Mandatory support models (RAG/embedding, STT, TTS) are preserved during restart by default.
- **Restart progress**: each restart creates a row in the `restart_jobs` table with `progress`/`total` counters. Poll `GET /api/admin/restart-status/<job_id>` for live status. The Settings page now auto-refreshes service labels every 5 seconds and streams restart progress live during a restart.
- **Service status**: the `service_status` table records each service's current state (`started`, `stopped`, `restarting`, `error`) with a timestamp and optional detail. Query via `GET /api/admin/service-status`. The API now also returns real-time status for the Lemonade service via Docker container inspection for a container named `lemonade-server` (e.g., `docker run -d --name lemonade-server -p 13305:13305 -v lemonade-cache:/opt/lemonade/.cache/huggingface -v lemonade-llama:/opt/lemonade/llama -v lemonade-data:/opt/lemonade/.cache/lemonade -v lemonade-config:/opt/lemonade/.config/lemonade ghcr.io/lemonade-sdk/lemonade-server:latest`), displayed prominently in Settings.
- **Model load/unload events**: the `model_load_events` table records every model load and unload attempt with backend, model ID, event type, status, error text, and timestamp. Errors include full text for copy/display.
- **Model backend tracking**: when a model is downloaded, its backend is recorded in the `model_registry` table so the backend can be identified even when the live catalog is unreachable (e.g. Lemonade is down).
Legacy backend URL and API key entries in `.env` are migrated during setup (or at app
startup for manual upgrades) and removed from that file. Keep `.env` for the database connection URL and other
deployment-only runtime values. On reruns,
existing users and credentials are preserved, and legacy `ADMIN_USERNAME` and
`ADMIN_PASSWORD_HASH` entries are removed from `.env`. If accounts already exist, the
setup form is not shown.

For manual setup, `.venv/bin/python tools/first_time_setup.py --help` lists setup
options. The command starts PostgreSQL and applies `hearth/storage/database_schema.sql`; the first
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
Each agent reaches its selected model through a task-specific Unix socket mounted
only inside that isolated container; the relay port is bound to container loopback.
When creating or editing an agent, you can attach an optional ZIP project archive. In
**Edit task**, archive files merge into the current project and replace matching paths;
the new ZIP replaces the saved source archive, so include any large archive-backed
files that should remain available. Hearth stores
the compressed bytes as a PostgreSQL large object in the separate
`agent_project_archives` table and streams them into the agent container for safe
extraction before work begins. Uploads can be up to 16 GiB compressed and 64 GiB
expanded. Files up to 512 MiB each and 2 GiB total are checkpointed normally; larger
uploaded files remain archive-backed source data and are restored when a container is
rebuilt. Agents should leave those source files unchanged and write results separately.
Encrypted ZIPs and archives with unsafe paths or links are rejected. The archive is
removed when its assignment is deleted. Docker builds the updated versioned agent image
on first use. Keep enough free space in Hearth's upload temp storage and PostgreSQL for
the compressed archive, and on Docker host storage for its staging copy plus extracted
project contents.
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

Hearth can also run models from opencode.ai: supply your opencode.ai API key from
https://opencode.ai/console in Settings, pick the account plan (Pay-as-you-go, or
OpenCode Go when you have that subscription — it uses opencode.ai's dedicated
/inference/go endpoint), then Test connection saves the selected plan and key and
verifies the key against that plan's model list. The model inventory appears on the
Models page, chat, agent assignments, and model tests. Failed checks display the
response body returned by opencode.ai.
Models are remote and externally managed; the key is stored encrypted in PostgreSQL
and only sent to opencode.ai's inference endpoint. Test connection saves a verified
key, an empty field never wipes the stored key, and the card's Remove stored key
button removes it explicitly. Deletion and download controls stay
with the opencode.ai console. Hosted model cards are informational in Hearth: they do not
offer local load or unload actions, direct unload requests for hosted model IDs are rejected
rather than sent to a local inference backend, and selecting a hosted model does not prompt
to unload local models. Choose a hosted model in Chat or an agent assignment to use it; no
backend service restart is needed when switching hosted models.

### Hosted opencode.ai models (free tier or Go subscription)

The opencode.ai hosted catalog works beside the local engines for chat, codebase-RAG
answers, agentic assignments, and model tests. Plans: the free `-free` models (for
example `glm-5.3-flash-free`) cost nothing, OpenCode Go is the $10/month subscription
with curated coding models and generous per-model allowances (its requests use
opencode.ai's dedicated `/inference/go` endpoint), and pay-as-you-go bills Console
credits. Configure it from **Settings → opencode.ai**: paste the key, choose the plan,
press **Test connection** (a successful test saves and verifies the key in one step).
The key is stored encrypted in PostgreSQL; only an explicit **Remove stored key** clears
it. Hearth sends a stable per-conversation `x-opencode-session` routing header as the Go
gateway requires, agent relays forward the client's own session header with a stable
per-task fallback, and both 402 (no credits on the selected plan) and 401/403 (key
rejected) errors surface as plain guidance in chat.

Project-agent requests are relayed from the Hearth host. For hosted models, the relay
uses the active credential saved by the host OpenCode CLI for the selected plan, then
falls back to Hearth's encrypted Settings key. The agent container does not need access
to the host OpenCode auth database. Connect the host OpenCode CLI to Go or pay-as-you-go
with `opencode auth login` if no credential is available, then restart Hearth and resume
an assignment blocked by an invalid credential. Relayed requests use Hearth's client
User-Agent and a stable session ID for OpenCode Go routing. Within the isolated agent,
Hearth registers the hosted model under a private compatible-provider ID so OpenCode's
native provider-auth lookup cannot bypass the host relay.
The hosted agent config omits the llama.cpp-only `chat_template_kwargs` extension,
which opencode.ai rejects as an unsupported request argument.

Hosted agent assignments check model availability without loading or unloading local
models. After updating Hearth and restarting `ai-webui.service`, use **Resume** on any
assignment previously blocked by `Not an opencode.ai model id`; it retries with the
selected hosted model and the saved workspace. No API key change is needed for this fix.
The agent picker and new hosted relays use the current catalog, account plan, and key
without requiring a desktop OpenCode reload. If a saved model is absent from the current
plan's catalog, select an available model before resuming the assignment.
When an agent receives HTTP 402 (credits or plan allowance) or 429 (rate limit), Hearth
records the affected hosted model and checks the provider's `Retry-After` header. The
task list and task page show that estimated next-available time when supplied; otherwise
they state that the provider did not give a reset time. A later successful request clears
the model's prior warning. This records provider signals; it does not automatically retry
the assignment before the provider's stated time.

Hearth chat exposes the configured Playwright browser and persistent Python MCP
tools on KoboldCPP, FastFlowLM, opencode.ai, Lemonade, and LM Studio. The MCP servers
start the first time chat needs them. Tool-enabled requests use Hearth's local
backend compatibility shims; keep the `lemonade-guard-shim`, `lmstudio-shim`, and
`flm-tool-shim` user services running for those backends. Ask the model to use the
browser for a specific page or task when you want a Playwright action. Hearth chat
offers a compact browser tool set and trims oversized FastFlowLM browser results so
they fit more reliably within smaller model context windows. After navigation and
page-changing actions, Hearth waits for content, lists and visits each open tab
(including popups), captures snapshots, and returns an explicit confirmation to the
model. The chat browser tools support navigation and inspection; they omit page clicks
and form submission so research cannot trigger page actions. Online research settings
allow up to 500 relevant pages per request. Organic results are relevance-filtered and
domain-diversified before navigation, while ad and tracker requests are blocked. Set
the limit to 0 for snippets only; pasted URLs still follow the separate direct-URL setting.
For general online research, chat uses Hearth's supplied search evidence instead of making
the model repeat the lookup through many browser tool calls. The tool-call round budget
defaults to 100 and is configurable in Settings for specific multi-step browser tasks.
For local KoboldCPP, Hearth runs a persistent compatibility shim on 127.0.0.1:13308
via the systemd user service `kobold-shim.service`. The service starts
`python -m hearth.backends.kobold_tool_shim` and runs
`python -m hearth.backends.wire_kobold_proxy` on ExecStartPost to patch Caddy's
8446 gateway block so LAN OpenCode/Continue requests are proxied through the shim
instead of directly to KoboldCPP's 5001. Each proxied request is reported to Hearth's
live metrics feed with `backend=kobold`, making KoboldCPP traffic visible in Live logs
and Metrics. The shim withholds tool schemas for an explicit "do not call more tools"
follow-up and repairs a missing parallel call only when the request names two literal
values for the same required string field. KoboldCPP model tests use this bridge too.

After signing in, open **Agents**. Use **New agent** to enter a required agent name,
name the task, and describe what you want in everyday language; the agent name identifies
its model requests in **Live logs**. Advanced model and command approval settings are
optional. **More options** lets you set the OpenCode step limit before launch
(default 40, range 1–10,000), separately from the assignment work-cycle budget. You can
edit this limit on a paused, blocked, stopped, completed, or interrupted task; continuing a
blocked or interrupted task with a changed limit starts a fresh OpenCode session from
its saved project files.
For a completed, stopped, interrupted, blocked, or paused assignment, choose **Edit task**
and expand **Online research** to enable or remove DuckDuckGo, Exa, Firecrawl, Parallel,
Tavily, or TinyFish. DuckDuckGo uses a community-maintained MCP server bundled in the
agent image and requires no key. See the [server project](https://github.com/nickclyde/duckduckgo-mcp-server).
Saved providers are checked automatically. When an assignment needs current or external
information, the agent uses those providers directly and infers search terms from its
goal and project context; it does not ask you to provide search terms or confirm use of
already-saved providers. Enter a replacement API key to
change it; a blank key field retains the saved key, and deselecting a provider removes
its saved key. Tavily requires a key while selected.
The **Skills** tab on the Agents page manages your private agent skill library. Add a
skill with a name, short description, and Markdown instructions; upload a `.md`/`.txt`
skill file or a `.zip` containing `SKILL.md` files; or download a skill directly from an
`http(s)` URL. Each saved skill can be edited, downloaded back out as a `SKILL.md` file,
or deleted. Select skills under **More options** when creating (or editing) an assignment
to install them into that agent's container as OpenCode skills; the agent is reminded to
use each skill's exact OpenCode ID to load it when relevant. Hearth mounts skills in
OpenCode's global discovery directory before its service starts. Skill selection or
content changes take effect on the assignment's next run, and skills are stored per
account in the `agent_skills` table created automatically by Hearth's migrations.
Restart Hearth after updating so the table is created before first use.
The first resumed agent run after this update rebuilds older containers so OpenCode
registers selected skills before agent sessions start. When installing imported skills,
Hearth normalizes frontmatter `name` to the OpenCode ID derived from the saved library
name; this keeps the skill tool's requested ID aligned with the mounted directory.
For example, imported `project_review_checklist` frontmatter is installed and
requested as `project-review-checklist` when that is the saved library name. If an
agent reports that a skill cannot be loaded, check **More options** for the selected
skill's ID and resume the assignment; Hearth rebuilds the session when the installed
skill bundle differs from the saved library.
Hearth project agents use OpenCode's direct file and shell tools. Shell commands still
follow the task's **Ask me first** or **Allow automatically** approval mode.
Generated OpenCode configs explicitly mark KoboldCPP models as supporting tools, and
agent relays apply that capability to the selected KoboldCPP model even if the desktop
config is older or its model catalog has stale capability data. Other backends keep
their existing model capability settings.
Pausing or stopping an assignment closes that container's active model response,
including buffered tool output. Restart Hearth after updating to activate this behavior.
For LM Studio, the agent relay propagates a per-request cancellation ID through
both local gateways so stopping an agent closes the upstream generation.
After a model request completes, Hearth times the OpenCode handoff from that
completion. If a pending tool remains without progress, Hearth recovers the
session after its progress and handoff limits instead of waiting indefinitely.
Transient provider connection resets are retried automatically in the same task cycle.
The model relay avoids sending a second HTTP response after a streamed response has begun.
The agent label is used for Hearth's request attribution and is removed by compatibility
shims before requests are forwarded to model servers.
**My agents** shows task status. Completed assignments include the latest execution's
elapsed time and generated model tokens per second. The rate is based on completed model
requests recorded by the local model relay; older or unavailable request records may
leave the rate blank. In automatic shell mode, allowed
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
labeled sandboxed report view. A saved HTML report is previewed directly; otherwise
JSON records and nested objects render as HTML tables rather than raw JSON text.
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
submit an answer with **Answer and continue**; Hearth keeps the prompt stable and
disables its controls while the reply is being sent. You can also pause or continue
the work from that page. OpenCode permission request IDs are sent as encoded path
components, so allowing or rejecting requests such as web search addresses the
correct pending permission. On **New agent**, expand **Online research** to allow
online research and select DuckDuckGo, Exa, Firecrawl, Parallel, Tavily, or TinyFish.
DuckDuckGo uses a community-maintained [MCP server](https://github.com/nickclyde/duckduckgo-mcp-server)
bundled in the isolated agent image and requires no key. Multiple
providers can be enabled for parallel searches. Exa, Firecrawl, and Parallel offer
limited keyless access; Tavily requires an API key. Enter provider keys on the setup
form when needed. Hearth encrypts keys in PostgreSQL separately from task details and
passes them only to that assignment's isolated OpenCode MCP configuration. Agents are
instructed to put search terms in the required `query` field and retry with a nonempty
query if a search tool reports that it is missing. Agents must use the exact available
search tool names. OpenCode MCP names include the configured server prefix and tool
name; agents must not invent generic aliases such as `web_search` or `web_fetch`. Before fetching full pages, agents
check search titles and snippets against the task's goal, entities, location, and time
period; they discard unrelated or duplicate hits and refine poor-fit queries before
deep-diving. Existing agent sessions refresh on resume or the next run to load corrected
search-tool guidance.
Provider
terms, rate limits, and any usage charges are controlled by the provider. No Hearth
restart is needed. The first agent run after an update builds a new versioned runtime
image containing DuckDuckGo's MCP server; Docker and internet access to the package
index are needed for that build. When an assignment is paused, blocked,
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
Automatic OpenCode compaction is on by default for desktop and project agents. Hearth
generates a separate compaction-enabled OpenCode config inside each agent container. For
Lemonade Muse models, Hearth keeps the known 65,536-token context window if Lemonade
reports only temporarily available compaction headroom (for example 11,264 tokens),
preventing repeated context-limit rejections from triggering compaction after every step. Its
OpenCode relay also shortens oversized historical tool results to their useful beginnings
and endings. A project assignment continues with its OpenCode conversation while it fits
the loaded model. Agent startup applies the selected model's saved loading ctx-size when it
needs to load that model. If context overflow still occurs, Hearth saves the project
checkpoint and automatically retries once in a fresh OpenCode session without advancing
the work-cycle counter. Its first prompt is kept short and does not repeat project memory or
verification instructions. A repeated overflow remains saved for review. When compaction fails,
Hearth also retries in a fresh session from the saved project checkpoint without advancing
the work-cycle counter.
For project agents, Hearth also caps retained recent turns at one eighth of the
selected model's context up to a 65K-token window; Lemonade/Muse-style 65K+ models
get a large recent-context budget (`keep.tokens` capped at 65,536). On a 65,536-token
window `keep` is 40,960 and `buffer` 16,384 — `keep` never equals the window, because
OpenCode's own compaction must fit a summarization request (`keep` + `buffer` + prompt
overhead) inside it; `keep` equal to the whole window fails with "The compaction
request cannot be reduced further without losing the latest exchange or checkpoint".
The buffer is also the headroom a single agentic step has when compaction fires: one
step can reach ~11K tokens, so 65K+ windows reserve a quarter of the window as the
buffer (an older 8,192 buffer let a 10,830-token step overrun the 9,984 tokens left
and fail the session). Smaller windows still
scale to one eighth of their context. The selected model's own context is the
floor, too: a smaller sibling in the shared config cannot leave a larger-window
assignment with only a tiny retained history.
This formula lives in one place (`help_data.compaction_for_window`). Hearth now
shows the generated OpenCode config in Help for manual copy/save and no longer
rewrites `~/.config/opencode/opencode.jsonc` automatically. Run
`python3 tools/sync_opencode_config.py` to print the current generated config;
save it manually when you want OpenCode to use the updated model limits and
compaction budgets.
This prevents the agent repeatedly re-reading the same large file after compaction.
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
activity history. Manual and scheduled reruns use the latest saved goal, acceptance
criteria, technical and output requirements, and user refinements; the editor refreshes
saved values from other sessions unless it has local unsaved edits.
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
settings, including the latest saved acceptance criteria, technical and output
requirements, and user refinements; completed parts are reused and no second agent
definition is created.
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
ordinary HTTP requests. The automatic detector skips live lookups for social/greeting
messages aimed at Hearth itself ("How are you today?"); the reply comes from the model,
not from scraped page copy. A topical follow-up in the same message still triggers the
lookup, and a plain "search for X", "browse X", or a pasted URL keeps the earlier behavior. If Chromium needs OS libraries after installation, run:

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

If you skipped automatic runtime installation, install and start a supported runtime. Configure its URL
and credentials under Settings. LM Studio's default endpoint is
`http://localhost:1234`; Lemonade's is `http://localhost:13305`. Download a model
and load it from Models. The first-time Ubuntu installer offers a confirmed
download when Lemonade is available; other models remain available in Downloads.
For one-command installation of Hearth with the optional q38rocm Qwen 3.8
backend on Ubuntu or Debian, run this from the project checkout:

```bash
bash deploy/install-hearth-q38rocm.sh
```

The installer accepts the model path as its first argument, or finds it if there
is exactly one `Qwen3.8-27B-Q3_K_M.gguf` in the Hugging Face cache or already in
the backend folder. For example:

```bash
bash deploy/install-hearth-q38rocm.sh /path/to/Qwen3.8-27B-Q3_K_M.gguf --skip-browser
```

The model weights are not downloaded by this installer. It installs the Mesa
RADV Vulkan runtime and build dependencies with `sudo apt-get`, checks whether
Vulkan detects the Radeon GPU, copies the model into the backend, builds q38rocm,
installs its on-demand user service, and runs the standard Hearth first-time
setup. The service expects this exact filename; it is a Q3_K_M quant, not a
ROCmFP4 FAST model. On Radeon 890M (gfx1150), q38rocm uses Vulkan hardware
acceleration with a default 131,072-token loaded context (native maximum: 262,144)
and FP16 KV cache (about 8 GiB of attention cache at the default window).
Hearth installs the `speed` profile with logical batches of 2,048, physical
microbatches of 64 to bound workspace, and prompt caching limited to 2 GiB. Flash Attention
is disabled because it corrupts this model's output on the pinned Radeon engine,
including with FP16 cache; CPU and Vulkan without Flash Attention pass the tool probe.
On this Vulkan
device Hearth disables MTP: upstream reports roughly 0% MTP draft acceptance
on Vulkan0 in this engine, while MTP is supported on ROCm0. Upstream's higher
published decode results were measured on a Radeon 8060S, not this 890M, so expect
lower throughput on this host. Full-context prefill and generation can be slow,
and available shared GPU memory must be sufficient for the model and
KV cache. If GPU memory allocation limits throughput, the upstream checkout
provides optional root-level clock and TTM/GTT tuning via
`sudo bash data/q38rocm/apply_hardware_tweaks.sh`; this changes runtime kernel
settings. Hearth lists the model on **Models** and starts/stops `q38rocm.service` from its Load/Unload
controls. It serves local Hearth chat and model tests, and the desktop OpenCode
config sync adds the model to `~/.config/opencode/opencode.jsonc` while the GGUF
is installed. Load it from Hearth's Models page before selecting it in local
OpenCode. Its Models card has a per-model **Enable thinking** toggle, off by
default and applied to each Hearth chat request without a service restart.
q38rocm is selectable in the **Model tests** page; compatibility checks continue
to disable reasoning for concise checks.
The endpoint listens on `127.0.0.1:8008`, so it is not included
in LAN or Tailscale coding-client configs. q38rocm uses service sampling defaults;
explicit caller frequency penalties are supported. Chat and model tests share
the same request adapter and use the model's Qwen XML tool template through the
engine's OpenAI API. Compatibility tests disable extended reasoning and preserve
fixture output budgets (normally 512 tokens for chat and 2,048 for tools), and limit the large-code
fixture to about 4K tokens to reduce slow generation and prefill time. The synthetic
overflow probe is skipped for this large-context backend because constructing an
over-limit request requires a slow full-window prefill; other backends retain that
probe. Hearth forwards q38rocm's OpenAI-compatible parallel-call and
reasoning-template options. Thinking booleans are mapped to
`chat_template_kwargs.enable_thinking`. Named choices use a required
call with only the selected tool's schema because this server build accepts
only auto/none/required choice strings. Change its URL in Settings only if you move it.
Hearth requests the final streaming usage event so q38rocm prompt and completion
token counts are included in Metrics.
UI test runs resolve the live served model ID. For standalone checks run
`.venv/bin/python -m tools.test_flm_model --backend q38rocm --url http://127.0.0.1:8008/v1/chat/completions --model '<served model ID>' --suite core`,
using the ID from `/v1/models`. This uses the UI's adapter and bounded input
fixture; full tool budgets can require several minutes on this GPU.
Without `--url`, the q38rocm CLI uses the saved endpoint and API key. Deliberately
skipped probes do not make the CLI fail. Reports retain backend decode and prefill
rates when available to distinguish prompt processing from token generation.
The [q38rocm schema audit](../docs/q38rocm-schema-audit.md) records the verified
installed schemas and Flash Attention diagnosis. Existing installations apply
the fix with `bash deploy/install-q38rocm-service.sh` and then
`systemctl --user restart q38rocm.service`. GPU acceleration and the per-model
reasoning toggle remain available.
Set `Q38ROCM_CONTEXT` when running the service installer to request another
window between 1,024 and 262,144 tokens. Larger windows require more GPU memory;
the corrected 262K configuration exceeded this desktop's allocation limit.
Models reports the live loaded context separately from the model's 262K maximum.
Model-test handoff checks use each backend's live inventory to avoid false failures
from stale loaded-state metadata in the combined Models view.
Hearth chat also preserves the requested single- or multi-select mode for structured
OpenCode questions when a backend omits that optional field.
The Downloads page lists `julianmb/Qwen-3.8-27B-ROCmFP4-FAST-GGUF` under q38rocm
using its actual Hugging Face repository ID, not a generated slug, and downloads the
Vulkan-compatible Q3_K_M GGUF into q38rocm's model directory. If the same file is
already in Lemonade's Hugging Face cache, Hearth copies that cached file rather
than downloading another copy. The model is assigned to q38rocm in Models, not
Lemonade.
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
Hosted opencode.ai catalog models are not offered for model tests; they remain available
for chat and agent assignments.
LM Studio model cards offer administrators a per-model **Enable thinking** switch.
It is saved in PostgreSQL and applies to chat, model tests, and gateway requests
on the next request. Some models may ignore the thinking controls.
OpenCode assignments send both LM Studio thinking controls. The LM Studio gateway
forwards streamed reasoning before buffered tool arguments finish; restart Hearth
and `lmstudio-shim.service` after updating to activate this behavior.
The page has tabs for the compatibility run, **All tests** (compatibility plus
reasoning and security in one report), core checks, the OpenCode coding workflows,
extra tool-behavior checks, tool calling, model reasoning, and model security behavior.
The reasoning tab asks 12 original questions one at a time and
scores points correct; it is a comparison benchmark, not a human IQ score. The
security tab runs 10 safe behavioral probes and reports how the model handles them;
it does not scan the host or certify security. Test prompts are sent individually,
and generated tools/actions are not executed. Results are saved as model-test reports.
Choose **All tests** to run the selected model's complete compatibility checks,
reasoning questions, and security probes together in one report. Tool checks already
included in the compatibility suite are not duplicated.
For KoboldCPP, core, OpenCode, and comprehensive runs also report whether generated
OpenCode model metadata enables agent tools. This distinguishes endpoint tool-call
support from the capability OpenCode uses to expose agent tools; the diagnostic is
shown in the report and excluded from model recommendation scoring.
Metrics recorded for these checks carry the selected backend name, making validation
requests filterable with that backend's chat traffic on the Metrics page.
Context overflow checks accept a clear rejection or a successful response whose
reported token usage stays within the model's context window. Hearth chat preserves
an explicit single- or multi-select mode when normalizing structured question calls.
Tool-enabled chat requests also explicitly allow compatible backends to return
multiple independent tool calls in one response.
Tool checks use a 2,048-token completion budget to leave room for internal reasoning
and complete tool arguments. Malformed argument failures include finish reasons,
completion token counts, and a bounded argument excerpt in the saved error details.
OpenCode workflow checks also inherit this budget on every turn. Start a new run
after restarting Hearth; previously saved reports retain their original results.
Tool-call mismatch errors include finish reasons, the requested response budget,
and completion token usage when supplied by the backend.
When a request names one function and gives its exact JSON arguments, the LM Studio
proxy rebuilds that call if the backend mangles, duplicates, or drops it. Ordinary
tool choices and other requests are unaffected.
No additional configuration is needed; restart Hearth after updating to use these checks.
Monitor compatibility repairs and review saved reports there. Completed older
reports can be deleted while another test is running; the active report stays
protected. **Test Model** on a model card starts a test of that card's model;
automatic repair stays off for this run. **Reset repair** opens that page as well.
Failed workflow steps mark dependent checks as blocked; only checks
that ran are included in repair comparisons. An upstream 502/503 or unreachable
backend is recorded once, and later dependent checks are skipped. Automatic repair
is optional and is off by default. The Lemonade proxy translates named-function
choices into a required call with only the requested function offered. If the user
message supplies exact
arguments for that function, Hearth preserves the requested call when the model
returns prose or another function. It also converts supported model-native tool markup to
structured calls only for functions offered in that request. An instruction not to
repeat a question does not suppress a later explicit tool action in the same turn. The
FastFlowLM `minicpm-v-4.7:1b` path has its own tool-use reminder and exact named-call
handling. By default, it can perform up to six tool rounds for multi-step browser tasks, then
requests a text-only summary of the results; other FastFlowLM models use their
existing behavior. Administrators can set Hearth chat's maximum tool-call retries
(rounds) per response under Settings (1–100, default 100); changes apply to the next chat request.
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
Disabling thinking also overrides enabled request flags in model tests and coding clients
using Hearth's Lemonade gateway, including agent assignments and older OpenCode configs.
Restart Hearth and `lemonade-guard-shim.service` after updating; the saved preference applies
to new requests without reloading the model. An already running response keeps its original
setting. Large input histories and CPU inference can still take time with thinking disabled.
Lemonade, LM Studio, vLLM, and KoboldCPP model cards also include an editable
**ctx-size** field. KoboldCPP reads each GGUF file's native context maximum.
Saving a value stores it for that model and reloads the model when it is already loaded;
the next Load reuses the saved value. **Set all to max context** saves every adjustable chat model's
reported maximum at once and leaves already-loaded models running until their next Load.
Enter the model's reported maximum to use its full context window instead of the backend
default. Generated LAN and Tailscale configurations for OpenCode and Continue use the
actual served window of a loaded model and the saved value for an unloaded model.
Reload a loaded model to activate the saved maximum. This allows longer exact conversations on a
large-window model. A larger window needs enough system or GPU memory, and a reload
interrupts requests using that model.
FastFlowLM retains its verified per-model NPU limits; increasing those without a
supported kernel can prevent the model from loading. q38rocm uses the context
installed in `q38rocm.service`; its 131,072-token desktop setting fits the GPU,
while the native 262,144-token maximum exceeded the available allocation.
Desktop OpenCode config sync advertises the installed q38rocm setting.
Lemonade's gateway shortens oversized historical tool outputs to useful head and tail
excerpts before forwarding them to OpenCode. Automatic OpenCode compaction is enabled
by default, and long requests still must fit the model's loaded context window.

## Verify and maintain

The **Models** page has separate **Local models** and **Online models** tabs. Local models
remain grouped by backend and show their load state and lifecycle controls; hosted
opencode.ai models appear on the Online models tab and are managed by the provider.
The **Loaded now** summary lists local runtime models only.

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
owned by the running application. The admin browser coverage visits every standalone page,
checks rendered table column counts and alignment at desktop and phone widths, exercises
key page actions, opens a Live logs request trace, checks Browse history search and deletion,
and verifies that ordinary users cannot open admin-only pages or APIs.
The unit discovery includes RAG provider routing for every chat backend, KoboldCpp
embedding endpoint handling, fail-soft behavior, and separate-vector-space retrieval checks.

Administrators can inspect request metrics on **Metrics** and open **Live logs** for
streaming request progress, errors, and request/response traces. The live feed polls
automatically, shows requests from the last 48 hours (up to 500 most recent), and loads trace text only when opened.
Streaming rows stay at the top of the feed and update their displayed token count, rate,
and elapsed time. They hide rates that round to zero; completed rows keep all three values
fixed at finish.
Completed rows remain available for two days. Project-agent model requests appear with
the task's agent name through Hearth's model relay. No additional setup is required.
Administrators can use **Reset all metrics** to erase saved request history and coding
reviews after two confirmation warnings; subsequent requests are recorded normally.
The **Live logs** page also has **Reset live logs**, which clears completed history and
captured request/response traces after two confirmation warnings. In-flight rows remain
visible and their completed entries are recorded after they finish.
Live logs also include text search across request metadata and opened traces. Admins
can kill active Hearth chat and agent relay requests from their rows. Requests handled
entirely by an external backend cannot be stopped from Hearth.
Rejected agent relay chat requests appear as failed 403 entries labeled with the agent
name and model; Hearth does not read or save their prompt body.
If OpenCode closes a model response before it finishes, Live logs labels the row
**CANCEL** and records that the client closed the stream, separately from an upstream
model error. An agent's first-token timeout can close a stream this way.
For streamed chat and model test requests, Hearth asks Lemonade, FastFlowLM, LM Studio, and KoboldCPP for
token usage and records generated-token counts when the backend supplies them. On local
KoboldCPP, Hearth also samples `/api/extra/perf` before and after each request. It accepts
the last-request counts and decode speed only when KoboldCPP's generation counter advanced
by exactly one, avoiding attribution of another concurrent request's global counters.
Historical rows created without usage counts remain unknown. Tok/s values are decode speeds:
Hearth uses the backend's own measured `decoding_speed_tps` when available (Lemonade and
FastFlowLM), or KoboldCPP's correlated `last_eval_speed`; otherwise it falls back to
completion tokens divided by request elapsed time (which includes prefill). Requests without
a measurable speed show — rather than 0. KoboldCPP does not provide NPU utilization or TOPS
through this endpoint, so those NPU-specific fields remain unavailable for Kobold requests.
Metrics tables scroll horizontally inside their panels on phones, and the header row pins
to the top of each table's scroll area so all columns stay aligned at any width. The model
report uses fixed shared column widths; dashboard cards and filters fit the available screen width.
Agent HTML result previews also work when workspace synchronization stores the entry
file path with a leading `./`.

Model validation reports accept spelled-out test counts, common Markdown emphasis,
and summaries such as “passed all 4 tests.” The suite checks parallel calls
separately from OpenCode's multi-file read continuation scenario, so a model can
report independent results for each behavior. Automatic compatibility repair is
off by default, including when a test run starts from a model card. **Test Model**
tests the model from that card.
The Model tests dropdown also selects Ornith by default when available, and allows
choosing another installed model. Select the repair option on the Model tests
page to enable repairs for a run; the option appears on the full run and on the
core, coding-workflow, and extra-tool tabs, which all support the verified recipe.

Administrators can open **Browse history** to search and page through visits from chat
research and agent tasks, including URLs returned by configured agent research providers
and agent browser navigation. Agent task text and provider credentials are not copied into
history; agent searches use a generic task-search label. Repeated task snapshots do not
create duplicate entries.
Delete individual entries from their rows or clear the entire history with **Delete all
history**; both actions ask for confirmation.

For an application update, install requirements again, rebuild the agent image
when its recipe or OpenCode version changed, and restart the service:

```bash
.venv/bin/python -m pip install -U -r requirements.txt
bash deploy/install-first-time.sh --rebuild-agent-image
systemctl --user restart ai-webui.service
systemctl --user restart lemonade-guard-shim.service flm-tool-shim.service lmstudio-shim.service
```

Use `-U` so security-fixed minimums in `requirements.txt` replace older installed
packages; plain `-r` skips packages that are already installed. The current floors are
`cryptography>=50` and `mcp>=1.30,<2`. The separate vLLM virtualenv is updated on its
own; keep its `setuptools` at the version the installed vLLM release pins, since a forced
upgrade makes vLLM refuse to start. Update model runtimes through their own installers or
package managers (for example `sudo tailscale update` for Tailscale), then restart their
services and Hearth. Lemonade runs in Docker and can be updated with
`docker pull ghcr.io/lemonade-sdk/lemonade-server:latest` followed by
`docker restart lemonade-server`. Re-run the
administrator host security scan after the upgrade.

The following runtime versions were checked for this guide on 2026-11-14. This is a
version snapshot, not a set of pins in the Hearth installer. The optional automatic
backend installer installs LM Studio headless and Lemonade; it does not update existing
installations or install the other runtimes.

| Runtime | Checked version | Update source |
| --- | --- | --- |
| LM Studio headless (`llmster`) | 0.0.27+1; selected Vulkan llama.cpp engine 2.73.0 | [LM Studio installer and runtime manager](https://lmstudio.ai/docs/developer/core/headless); run `lms daemon update` and `lms runtime update --all` |
| Lemonade | 2026.46.2 | [Lemonade releases](https://github.com/lemonade-sdk/lemonade/releases); pull the Docker `latest` image and restart the container |
| vLLM | 0.33.0 CPU build | [vLLM releases](https://github.com/vllm-project/vllm/releases); keep the Hearth vLLM environment on the CPU wheel for CPU hosts |
| KoboldCPP | 1.124.2 | [KoboldCPP releases](https://github.com/LostRuins/koboldcpp/releases); AMD hosts should use the Linux `nocuda` build with Vulkan support |
| FastFlowLM | 1.0.13 | [FastFlowLM releases](https://github.com/ROCm/FastFlowLM/releases); install the package for the host Ubuntu release, then restart `flm.service` |
| ComfyUI | 0.45.0; frontend 1.58.5 | [ComfyUI releases](https://github.com/Comfy-Org/ComfyUI/releases); update its separate Python environment from the matching release requirements |
| q38rocm | No numbered release; current upstream source plus Hearth's local Vulkan build adjustments | [q38rocm source](https://github.com/julianmb/q38rocm); update the checkout, then rebuild with `deploy/build-q38rocm-vulkan.sh` |

LM Studio, Lemonade, ComfyUI, and model-serving services can be running without a model
loaded. vLLM, KoboldCPP, and q38rocm are normally started on demand when a model is
selected. Updating a runtime does not download model weights.

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


### Optional automatic model runtime installation

Both first-time and release installers ask whether to install LM Studio, Lemonade,
both, or skip (the default for an existing or remote backend). For unattended setup:

```bash
bash deploy/install-first-time.sh --install-backends lmstudio lemonade
# Release bundles use the same options:
bash deploy/install-release.sh --backend lmstudio --install-backends lmstudio
# Preserve an existing runtime or use a remote server:
bash deploy/install-first-time.sh --skip-backend-install
```

LM Studio installation uses the [official headless llmster installer](https://lmstudio.ai/docs/developer/core/headless)
on Linux, macOS, and Windows, reuses an existing `lms` executable, and starts its
API at `http://localhost:1234`. This installs the server runtime without the desktop
app. On Linux with systemd, Hearth installs
`~/.config/systemd/user/lmstudio-server.service` and `lmstudio-server.timer`.
The timer checks the API every 30 seconds and starts the daemon or server if
port 1234 stops responding. After updating, run
`bash deploy/install-lmstudio-server.sh` to install the timer. Systems without
systemd still need `lms daemon up` and
`lms server start --port 1234` at each login. Pass `--skip-lmstudio-server` to omit
this bootstrap unit when you start the API server yourself.
Lemonade installation uses Docker and starts the official
`ghcr.io/lemonade-sdk/lemonade-server:latest` image as the `lemonade-server`
container at `http://localhost:13305`. Lemonade stores models in Docker volumes;
when `~/lemonade-hf-cache` exists, the installer mounts that existing cache so
migrated models appear without copying hundreds of gigabytes. Otherwise, it uses
the `lemonade-cache` named volume. The installer does not install a
Snap or host package and does not download model weights. Configure the local URL
in Hearth Settings (the release installer saves its selected endpoint), then
download models through Hearth's Downloads page. On Linux the container uses host
networking so Hugging Face downloads use the host's working DNS and internet route;
its API remains available at `localhost:13305`.
The Models page also checks required model files in the shared Hugging Face cache;
this keeps migrated downloads visible when Lemonade's catalog download flag is stale.
Split GGUF models with missing shards appear as incomplete and cannot be loaded until
the full model download is completed from **Downloads**.
Deleting a Lemonade model removes its Hearth inventory entry even if another variant
still uses shared cache files. Download it again to restore the entry.
On Linux hosts with AMD Ryzen AI NPU support, the installer passes
`/dev/accel/accel0` and its owning group into the container when that device exists.
The host NPU driver must be installed. Only Lemonade models that use the FLM/NPU
recipe run on the NPU; GGUF models continue to use their configured CPU or GPU
recipe. On Linux hosts exposing `/dev/kfd` or `/dev/dri`, Hearth also passes available
GPU device nodes and their owning groups into Docker for Vulkan/ROCm GGUF recipes.
To apply network, NPU, or GPU settings to an existing Lemonade container, run
`bash deploy/install-first-time.sh --install-backends lemonade`; Hearth recreates
the container with its existing model cache, preserving downloaded models.
If an older Lemonade Snap service still owns port 13305, stop that service before
rerunning the installer. The installer reports the port conflict without replacing
the existing container.
For OpenCode, Hearth advertises Lemonade's loaded context and checks it before reusing
a loaded model. If the live context is below the saved Models-page setting, Hearth
reloads the model at that size; otherwise the default Lemonade context is 65,536 tokens.
This keeps OpenCode's advertised limit aligned with the running Lemonade model and
prevents compaction from triggering at a stale smaller context.
An explicitly configured remote URL is preserved even when local runtimes are installed.

Administrators can restart managed inference services from **Settings → Service
controls → Restart all backends**. Hearth restarts LM Studio, Lemonade's gateway,
vLLM, KoboldCPP, q38rocm, and FastFlowLM independently and reports each result.
Use **Restart q38rocm** on its Settings card to restart only that service; it must
already be loaded. A backend
restart interrupts active inference and may require loading the model again. Use
**Restart Hearth** in the same section to restart the web application; the page
disconnects briefly while the service comes back. Active agent tasks may be
interrupted and can be resumed from Agents.


For noninteractive setup, use `--skip-backend-install` or an explicit
`--install-backends` selection; closed input skips the optional runtime prompt.
Release installers accept `--no-api-key` to skip the secret prompt while preserving
an existing key, and `--project-name NAME` with `--postgres-port PORT` to isolate
an additional installation. Blank API key input also preserves an existing key.
Use `--no-service` with the Ubuntu first-time installer when running in a container
without systemd. Docker must already be available to that container.

On clean Ubuntu installations, optional LM Studio setup installs `libatomic1`
when missing and fetches the official installer with `curl`. Current Lemonade
packages are detected by `lemond` as well as the legacy executable. In containers
without systemd, start Lemonade manually with `lemond`; setup reports this instead
of claiming the service started. First-time setup requires Python 3.12+ and reports
an actionable error if service installation is requested without systemd. Installing
missing host packages no longer tries to restart an already accessible Docker daemon.

Linux browser installation includes Playwright's required OS libraries using
`playwright install --with-deps chromium` (sudo may be requested). `--skip-browser`
skips both the browser and those libraries. This prevents a fresh installation from
reporting success with a Chromium executable that cannot launch.

### Containerized Hearth on Linux

To run the complete Hearth app in Docker alongside the normal host installation:

```bash
bash deploy/install-docker.sh
```

Prerequisites: Docker Engine, Compose, and host Python 3. Open
**https://localhost:5444**, accept the generated local certificate, and create the
new administrator account. Setup creates `.env.docker` with a random database
password, selects free HTTPS/database ports starting at 5444/55434, and creates a
persistent runtime directory.
Existing configuration is preserved on reruns. Customize that file before rerunning
if either port is occupied. Keep the file private and retain it with your backups.
This containerized deployment uses its own PostgreSQL volume and app data; it does
not migrate or share the current host installation's accounts or conversations. Each
instance names its session cookie after its listen port, so the container login and
the host login coexist in the same browser without replacing each other.

The app image includes Chromium and its OS libraries. Model runtimes are configured
separately in Settings; Linux host networking lets the container reach existing
LM Studio/Lemonade servers at their localhost URLs. The app receives Docker socket
access for isolated coding agents, so use a trusted Linux host. Its agent runtime
bind mount uses the same absolute path inside and outside the app container; do not
move it while assignments are active. Missing agent images build on first use.

The app container does not include systemd. Hearth cannot natively start or stop host
services for systemd-managed backends (vLLM, KoboldCPP, and FastFlowLM) from the
Docker instance, and their inventory checks fail soft instead of blocking another
backend's load.

An optional local broker bridges that. A fresh install now creates it
automatically: both `deploy/install-first-time.sh` and `deploy/install-docker.sh`
create and enable `~/.config/systemd/user/hearth-backend-broker.service`
(implementation lives in `deploy/install-backend-broker.sh`); pass
`--skip-backend-broker` to either installer to opt out of the broker.
The Docker installer also creates `~/.config/systemd/user/lmstudio-server.service`
through `deploy/install-lmstudio-server.sh`; pass `--skip-lmstudio-server`
to skip that additional bootstrap.

The broker runs `systemctl --user|--system` for a fixed allowlist of Hearth's units
(`vllm.service`, `kobold.service`, `flm.service`) on 127.0.0.1:8765, auth by a random
bearer token stored in `~/.config/hearth/backend-broker/token`. It also proxies a
read-only subset of the FastFlowLM `flm` CLI (`list --filter installed|--filter all`,
`pull <tag>`, `remove`, `--version`, and `pull-progress`) so Docker Hearth can download
and delete NPU models on the host; the host Hearth install still runs everything else.
The Docker app is built so the controls can be used from the browser; the UI reads the same
commands the host Hearth install uses, and `~/.config/vllm`, `~/.config/koboldcpp`,
`~/.config/fastflowlm`, and `~/.config/flm` are mounted at (`Path.home()`/".config") so
load/unload sees the same inventory. Without the broker the previous behavior unchanged:
the UI tells you to control those services from the host install.

The Docker app shares model storage with the host rather than keeping a separate
copy inside the container:

- `~/.lmstudio` is mounted twice inside the container: at `~/.lmstudio` for the
  `lms` CLI, and at the host's absolute `$HOME/.lmstudio` path so a model path
  registered by KoboldCPP or LM Studio resolves identically from both sides.
- `~/.cache/huggingface` is mounted at the same absolute path and `HF_HOME` /
  `HUGGINGFACE_HUB_CACHE` point at it, so vLLM and KoboldCPP downloads use the host
  Hugging Face cache.
- Local GGUF uploads use `data/local-models` at the same absolute path inside and
  outside the container, so the host Lemonade server can open the registered file.

The installer writes these paths to `.env.docker` and adds missing values on reruns
without changing existing ports, passwords, or user edits.

```bash
# Status and logs:
docker compose --env-file .env.docker -f compose.docker.yaml ps
docker compose --env-file .env.docker -f compose.docker.yaml logs -f hearth
# Stop without removing data:
docker compose --env-file .env.docker -f compose.docker.yaml stop
# Rebuild and restart while retaining data:
bash deploy/install-docker.sh
```

Application files, certificates, and PostgreSQL persist in named volumes. Relay
files persist in `data/docker-hearth/agent-runtime` by default. Containers restart
with Docker. Deleting volumes deletes their stored data. Host systemd is not needed
inside the container. Windows/macOS Docker deployment is not covered by this Linux
host-network configuration.

In the Docker instance, background support-model loading starts disabled to avoid
eagerly loading models on shared host runtimes. Enable it in Settings if desired;
that choice persists across container restarts.

When Tailscale is installed and running on the host, the installer also gives the
container the host's Tailscale CLI and daemon socket. Help then shows the detected
tailnet address, DNS name, and Serve route, and startup logs the tailnet URL.
Installing or reconnecting Tailscale later requires rerunning the installer so the
container picks it up. The container keeps the app's own Tailscale settings; it does
not join or reconfigure the tailnet. Omitting `--tailscale-https-port` is normal when
you do not need a persistent Serve route; only the flag attempts to configure one.

On first creation of `.env.docker`, setup selects free ports starting at 5444
for HTTPS and 55434 for PostgreSQL. It prints the actual app URL when ready.
Existing saved port choices are preserved on reruns.


### Local certificates and trusted Docker HTTPS

First-time setup, release setup, Docker startup, and direct app startup create
90-day local certificates. They automatically replace Hearth-generated 825-day
certificates that can trigger `NET::ERR_CERT_VALIDITY_TOO_LONG`, and renew local
certificates when expired or within seven days of expiry at startup. Existing
private keys are reused with private file permissions. Certificates from external
issuers or with custom subjects are preserved. An incomplete certificate/key pair
produces a recovery message instead of overwriting the remaining file.
A continuously running instance should be restarted before its local certificate
expires. Certificate files persist across Docker container recreation.

Local certificates remain self-signed: shortening validity does not make them
browser-trusted. For trusted HTTPS with an existing connected Tailscale host:

```bash
bash deploy/install-docker.sh --tailscale-https-port 8448
```

Choose an unused Tailscale HTTPS port. The installer builds/starts Docker Hearth,
then adds a persistent Tailscale Serve route for that port. Existing routes on
other ports are retained. Open the `https://HOSTNAME.ts.net:8448` URL printed by
Tailscale, using a device connected to the tailnet. Tailscale supplies the trusted
certificate and proxies to the container's local HTTPS endpoint. This is private
tailnet access; the installer does not enable public Funnel access. The option
requires Tailscale already installed and connected, with permission to manage Serve.
The installer also passes the host's Tailscale CLI and daemon socket into the
container, so Help → Connect over Tailscale reports this route automatically.

When the host already has a Caddy gateway configuration, the installer mounts
`/etc/caddy/Caddyfile` into the container read-only. Help then shows the generated
bearer tokens and Continue/OpenCode configurations, and reaches Caddy's admin API
through host networking. Generate or update the host gateway with
`sudo bash deploy/install-host-deps.sh`; without host Caddy, Docker Hearth still runs
but Help cannot generate gateway keys.


### Model gateway keys and certificates

`sudo bash deploy/install-host-deps.sh` installs Caddy and generates Hearth's model
gateway. It creates random `opencode`, `vscode`, and `lan` bearer tokens, five
authenticated HTTPS proxy blocks for LM Studio, Lemonade, vLLM, KoboldCPP, and
FastFlowLM, Caddy's local CA certificates, and an exported CA root at
`~/.local/share/hearth/hearth-ca.crt`. Tokens are written once to
`~/.local/share/hearth/Caddyfile`; reruns preserve them, so OpenCode and Continue
configurations already copied to clients keep working. The installer publishes a
marked block to `/etc/caddy/Caddyfile`, validates it, and reloads Caddy. A
hand-installed block from an older setup is left untouched.

The basic `install-first-time.sh` installation does not install Caddy. When Caddy is
already present, it generates the same tokens and CA and publishes the block through
passwordless sudo (otherwise it prints the command). Gateway files are never generated
when Caddy is absent. To rotate a standard key, change it in
`~/.local/share/hearth/Caddyfile`, rerun `sudo bash deploy/install-host-deps.sh`, and
update the clients that use that key. Per-user keys are managed from **Users**.

The Continue and OpenCode configurations in Help use these keys when the app can read
`/etc/caddy/Caddyfile`; otherwise they show a placeholder. The CA root can also be
downloaded from Help, which is the certificate clients must trust for the gateway.


First-time setup validates database ports and service availability before prompting
for optional runtime installation. Without systemd, the shell installer reports
`--no-service` before installing packages. Both setup paths refuse to create a fresh
database over an unconfigured installation with populated legacy SQLite files;
configure the existing installation's `DATABASE_URL` first. Runtime and agent-image
installation follow this check. Rerunning the normal service installer restarts
`ai-webui.service` so updated code and renewed certificates take effect; active
assignments may be interrupted and can be resumed after restart. Use `--no-service`
when preparing an installation without starting/restarting the service.
### Login and password audit history

The **Users** screen includes an admin-only history of successful and failed sign-ins,
initial password creation, and administrator password resets. Each event includes the
account, acting administrator for resets, local timestamp, and request address; passwords and password hashes are never
stored in or displayed by the history. Existing installations create the audit table
automatically when the Hearth service starts.

# DFlash model loading

`Anbeeld-GLM-5.3-DFlash2-GGUF` cannot run as a standalone chat model. Restart
Hearth after updating. Selecting this draft on **Models** now gives an actionable
error immediately; load a compatible GLM-5.3 target model for chat. DFlash2
requires a backend configured with both target and draft models.

Model pickers in Chat, Settings, model tests, and agent assignments open a
dropdown with search at the top. Type part of a model name to narrow the list;
clear the search to show every model again.
The Reports dropdown shows model names, searches by model, and selects the same
report for viewing, downloading, or deleting. Active reports cannot be deleted.
Model tests count incorrect reasoning answers and failed security probes as
failures instead of blocked checks. Dependent workflow steps remain blocked when
their prerequisite fails. Restart Hearth after updating; saved reports retain
their old results, so rerun a suite to see the corrected classification.
Metrics uses each model's latest comprehensive completed test run (All tests or Complete); narrow suites do not replace it. Skipped checks do not lower pass rate. Recommendation scoring prioritizes pass rate (70%) and the reasoning benchmark (20%), with speed and token economy (5% each) as minor tie-breakers; available dimensions are normalized against the models currently listed. The reasoning benchmark is a model comparison score, not a human IQ estimate.
An active rerun leaves the prior completed score visible; after completion, Metrics
recalculates from the effective result of each check, excluding rolled-back repair
attempts. Restart Hearth after updating to serve the new JavaScript and styles;
refresh Metrics to see the revised score.
