# Hearth — Self-Hosted OpenAI-Compatible Web UI for Local AI and Agent Workflows

*Local AI, kept at home, but can be accessed anywhere in the world.*

**Hearth is a free, self-hosted AI WebUI for personal AI chat, local LLM testing,
and agentic coding workflows.** Use one browser interface to manage local models,
evaluate tool calling, supervise OpenCode agents, and keep a searchable conversation
history with cross-chat memory. Run your own AI assistant on your hardware, with
CPU, GPU, or AMD Ryzen AI NPU inference through supported model backends.

Looking for the **best OpenAI-compatible web UI** or a self-hosted ChatGPT-style
interface for local models? Hearth combines browser chat for local inference with
an **OpenAI-compatible API gateway** for coding clients, plus model management,
web research, persistent coding agents, and PostgreSQL-backed chat history. It is
designed for people who want an OpenAI-style workflow while choosing their own
supported local inference backends and hardware.

Hearth brings together a **local AI chat interface**, **LLM tool-calling test suite**,
**agent task dashboard**, and **personal AI workspace**. It is useful for trying a
new model, checking whether a small language model follows a coding workflow, or
using local AI for everyday questions, research, voice conversations, and learning.

[Get started](#first-time-setup) · [Explore features](#what-you-can-do-with-hearth) ·
[Test models](#local-llm-testing-and-tool-calling-evaluation) ·
[Use coding agents](#agentic-ai-and-local-coding-agents) ·
[Personal AI](#a-personal-ai-assistant-on-your-own-hardware) ·
[Search and download models](#search-download-and-manage-local-ai-models) ·
[Secure coding gateway](#secure-model-gateway-for-opencode-continue-and-vs-code) ·
[Tailscale remote access](#tailscale-remote-access-for-chat-and-coding-models) ·
[All features](#current-feature-directory)

> Hearth uses PostgreSQL 18 through Docker Compose. The first-time installer
> starts a clean database, loads the application DDL, creates the initial admin,
> and configures the app to start automatically after reboot. See the
> [installation guide](deploy/FIRST_TIME_INSTALL.md) for setup details.

A small Flask web UI for chatting with local models, with sidebar controls to load/unload
models and switch which loaded model you're chatting with. Hearth started as an LM Studio
front-end and now drives **five** local inference backends behind one chat UI — [LM Studio](https://lmstudio.ai), [Lemonade / Ryzen AI NPU](https://github.com/lemonade-sdk/lemonade),
[vLLM](https://github.com/vllm-project/vllm), [KoboldCPP](https://github.com/LostRuins/koboldcpp),
and [FastFlowLM](https://github.com/FastFlowLM/FastFlowLM) (the Ryzen AI NPU runtime run
standalone) — plus ComfyUI for image generation. Each backend is exposed to LAN coding tools
(Continue, OpenCode) over its own authenticated Caddy reverse-proxy port (see **Help**), and
every one has the same Search / Download / Load / Unload / Delete lifecycle from the Models and
Downloads pages. Beyond chat it also does cross-chat memory (RAG), auto-detected web search,
voice in/out, a performance-metrics board, and an in-chat practice-test/quiz platform —
all documented in their own sections below.

## What you can do with Hearth

| Use case | Included capabilities |
|---|---|
| Local AI chat | Streamed answer and reasoning updates during generation, saved conversations, response copy-to-clipboard, model switching, and supported reasoning displays |
| Model management | Search, download, load, unload, and delete models through supported backends; see each model's supported context limit on the Models page |
| LLM evaluation | Per-model tool-calling checks, typed argument validation, streamed and ordinary responses, and saved test reports |
| Agentic coding | OpenCode project tasks, progress monitoring, user-input handling, and configured verification commands |
| Personal AI memory | Cross-chat memory retrieval with local embeddings and stored conversation history |
| Web research | Web search and page retrieval, with optional Chromium scraping |
| Voice assistant | Microphone input, speech transcription, and spoken responses with configured voice components |
| Creative tools | Image and video generation through separately configured ComfyUI workflows |
| Learning | In-chat practice tests and quizzes |
| Performance monitoring | Request metrics, token usage, generation speed, and live request activity |

## Search, download, and manage local AI models

Hearth combines **local model discovery, Hugging Face model search, model downloads,
and model management** in one browser interface. Open **Downloads** as an
administrator to find models, then use **Models** to load and evaluate them.

1. Use **Search all backends** to search LM Studio, Lemonade, vLLM, KoboldCPP,
   and FastFlowLM together, or search a single backend. Leave the search field
   empty to browse each backend's available catalog or popular Hugging Face models.
2. Compare result cards by backend, model format, execution compatibility, download
   size, task type, update date, popularity, download status, and source link. Each
   card labels tool-calling support as **yes**, **no**, or **unknown**, based on
   catalog metadata.
3. Use the independent **Tool calling filters** to restrict results for selected
   backends to models whose catalog metadata explicitly declares support. Models
   with unknown support are excluded for a filtered backend. A positive label does
   not guarantee compatibility with every tool or runtime, so use **Test tools**
   before relying on it.
4. Filter by parameter count with **Small** (up to 4B), **Medium** (over 4B through
   13B), or **Large** (over 13B). These filters use reported parameter counts or
   recognizable sizes in model names/descriptions; models without a detectable
   count are hidden while a size filter is active. Clear the size checkboxes to
   show all sizes again.
5. Download a compatible model. Follow progress and logs, inspect active downloads,
   or cancel a download. Progress detail depends on the runtime.
6. Open **Models** to load, unload, or delete installed models. Select a loaded
   chat model in the conversation model picker.
7. Check the maximum context-token label shown on a **Downloads** result when
   metadata reports it. In **Models**, check the context limit shown on each label when choosing
   how much chat history to send. The displayed limit is the model/backend
   capability Hearth knows about; runtime settings can impose a lower active
   context size.
8. Run **Test tools** before using a model for coding or tool-driven workflows.

| Backend | Model discovery and download sources | Execution |
|---|---|---|
| LM Studio | Hugging Face GGUF search; direct model name or repository URL | CPU/GPU as supported by LM Studio |
| Lemonade | Curated catalog and Hugging Face search, including GGUF models | CPU/GPU/NPU according to model recipe and hardware |
| vLLM | Catalog and Hugging Face causal language model repositories | Depends on the installed vLLM runtime; the documented host uses CPU |
| KoboldCPP | Hugging Face GGUF search and downloads | CPU/GPU according to the installed build |
| FastFlowLM | FastFlowLM NPU catalog and exact model tags | Supported AMD Ryzen AI NPU hardware |

A model appearing in search does not establish that it fits your hardware or
supports every feature. Hearth includes resource checks and load feedback; runtime
compatibility, available memory, context size, and model capabilities still apply.
Embedding, speech, image, and video models have separate purposes from chat models.

On **Models**, each model label includes its maximum supported context-token count
when the backend reports or Hearth can determine it. This helps compare model
limits before loading or chatting; the actual usable context may be lower when a
runtime or backend setting applies a smaller value.

### Model picks for faster replies

For a smaller desktop that can fit a quantized 9B model, my recommended
all-around pick is [Ornith-1.5-9B-GGUF](https://huggingface.co/ornith-ai/Ornith-1.5-9B-GGUF),
starting with its **Q4_K_M** quantization. It is a dense 9B model distributed in
GGUF for local inference, giving a stronger quality and capability balance than
the tiny models below while remaining practical on a desktop. Actual memory use
and speed depend on quantization, context length, and how much work can run on the
GPU.

For quick everyday chat on a supported Ryzen AI NPU, start with FastFlowLM's
`qwen3:0.6b`. On the documented host it produced about **57 generated tokens per
second** in a live chat request. Its small size helps response speed, while larger
models are generally a better fit for complex reasoning and coding tasks.

For a compact model that can also handle tool calling, **Qwen3.8-2B-Distill** is
another fast option. It has worked for tool-driven tasks on this setup and is a good
pick when a larger coding model feels slow.

For coding on that host, the measured KoboldCPP setup with Qwen3 8B and Qwen3
0.6B speculative decoding produced about **5.1 tokens per second**, compared with
**4.3 tokens per second** without speculative decoding. Long coding histories can
still make prompt processing slow; use a GPU-backed model that fits your hardware
and keep context focused when turnaround matters. These are measurements from one
machine and model configuration, not speed guarantees. Compare your own backend and
model in **Metrics**, since hardware, quantization, context length, and workload all
affect response time.

## Test and compare models before coding

On **Models**, select **Test tools** on a loadable chat model. You can queue installed
chat models, follow the current check and elapsed time, cancel after the current
request, reopen recent runs, and download JSON reports.

The local LLM test suite covers chat and streaming, tool selection, typed JSON
arguments, parallel tool calls, file reads and edits, exact file content, error
recovery, large inputs, and context overflow. OpenCode scenarios also exercise
planning, multi-file workflows, questions and answers, permission decisions, and
continuation after context compaction. Reports distinguish failed checks from
steps blocked by an earlier failure.

Use these results to compare model/backend configurations for your coding workflow.
The tests supply simulated tool results; real project execution takes place in
**Agents** or your connected coding client. See
[the evaluation overview](#local-llm-testing-and-tool-calling-evaluation) and
[saved test evidence](reports/saved-failure-review-2026-10-19.md).

## Streaming chat and copying responses

Hearth streams generated response text into the conversation as the backend sends
it, including supported reasoning/thinking updates. The assistant message grows
during generation so you can see progress before the model finishes. Streaming
depends on the selected backend and model exposing incremental output; some models
may send reasoning separately or not expose it at all.

Use the small **Copy** icon on an assistant response to copy its text to the
clipboard. The control appears with the response after it is available.

## Secure model gateway for OpenCode, Continue, and VS Code

Use local models from a terminal coding agent or an editor on another machine.
Hearth integrates with **OpenCode** and **Continue for VS Code** through
backend-specific **OpenAI-compatible API endpoints**, with **Caddy HTTPS reverse
proxies and bearer-token authentication** when the gateway is configured.

1. Configure the desired inference backend and load a compatible coding model in
   **Models**. Check its tool-test results and loaded context size.
2. Configure the host's Caddy gateway and backend proxy services. These are separate
   from the basic first-time app/database installation.
3. Open Hearth's **Help** panel for the detected gateway addresses, CA certificate
   trust instructions, and generated Continue YAML or OpenCode JSON configuration.
   Configurations use the current model inventory and backend settings.
4. Trust the gateway's local CA on the client when using Caddy's internal
   certificates, and apply any client-specific CA settings shown in Help.
5. Copy the configuration for your **LAN** or configured **Tailscale** address into
   your coding client, then select the corresponding model.

Use the addresses displayed by your installation: backend ports can differ, and
hosted services may already occupy a port. The gateway requires its configured
bearer token. Administrators can retrieve or regenerate per-user gateway tokens
from **Users** and apply the supplied Caddy configuration. Token changes require
updating and reloading Caddy to take effect at the gateway.

Hearth's compatibility layers handle supported tool-call formats and request
policies. Generated coding-client configurations account for model context limits;
Settings also exposes context and OpenCode compaction controls. For compatible
coding requests, Hearth preserves explicit reasoning preferences, applies its Qwen
repetition control, and shortens oversized historical tool output while retaining
its beginning and end. It honors a clear text-only completion instruction without
disabling tools when the same turn requests concrete work. These policies help avoid
unnecessary tool calls and repeated long prefill. Request attribution helps
distinguish browser chat, OpenCode, and Continue traffic in **Metrics**.
Continue can additionally use a configured embedding model for codebase indexing.

See [codebase RAG](#codebase-retrieval-for-coding-clients),
[client attribution](#metrics-and-diagnostics), and
[Tailscale access](#tailscale-remote-access-for-chat-and-coding-models).

## Tailscale remote access for chat and coding models

Hearth includes **Tailscale access inside the app**: detected connection details,
clickable remote browser links, backend connection tests, and ready-to-copy
OpenCode and Continue configurations. Use the app to find the correct connection
for your phone, laptop, or remote coding workstation.

### Find your remote access links in Hearth

1. Open **Help** from the chat interface and expand **Connect over Tailscale**.
2. When the host has a detected Tailscale Serve route, this section shows a
   **Chat** link using the host's trusted HTTPS DNS address, including its configured
   port. Connect your other device to the same tailnet, open that link, and sign in
   with your Hearth account.
3. Administrators also get direct **Models**, **Downloads**, and **Settings** links
   in the same section. Use these to search/download models, manage loaded models,
   and change settings remotely with your normal account permissions.
4. Use **Download the backend CA certificate** when configuring a remote coding
   client. The section also provides copyable trust commands for Linux, macOS,
   and Windows.

Browser links appear when Hearth detects a matching Serve route to its local web
service. Backend configurations appear when a Tailscale IP is available. A missing
browser link does not establish that the backend API is unavailable.

### Connect OpenCode or Continue through the app

In **Help → Connect over Tailscale**, find **Tailscale configs (Continue & OpenCode)**.
Copy the generated Continue YAML or OpenCode JSON into your client, then select a
compatible loaded model. Hearth generates these from the current model inventory,
backend ports, credentials, and context settings. LAN configurations stay available
alongside the Tailscale versions.

The browser link uses the host's Tailscale Serve DNS address. Coding clients use
the numeric Tailscale IP and the configured Caddy backend ports, with bearer-token
checks and the Caddy CA certificate. Follow the app's certificate instructions for
those API connections; the browser's trusted HTTPS link has its own certificate.

### Configure and check Tailscale in Settings

Administrators can open **Settings** to:

- See the host's automatically detected Tailscale IP.
- Enable **Show the Tailscale section (and configs) on the Help page** and save.
- Set **Manual IP override** if automatic detection fails.
- Run the Tailscale backend tests for LM Studio, Lemonade, vLLM, KoboldCPP, or
  FastFlowLM to check the configured gateway address and authentication from the host.

These host-side tests help diagnose gateway configuration. Also connect from your
remote device to check its network access and certificate setup.

Install and connect [Tailscale](https://tailscale.com/download) on the host and
client devices first. Configure the host's
[Tailscale Serve route](https://tailscale.com/docs/features/tailscale-serve) for
private browser HTTPS and Caddy gateways for model API access. Hearth's Settings
controls expose the integration; host networking and tailnet access policy are
configured separately. The first-time Hearth installer does not perform that setup.

## Current feature directory

| Feature | What Hearth supports | Where to start |
|---|---|---|
| Local AI chat | Live answer and reasoning updates, saved conversations, model selection, Markdown and comparison tables, supported reasoning displays, and background replies while navigating | Chat |
| Model search and downloads | Cross-backend search, source links, model metadata, tool-capability filtering, download progress and cancellation | Downloads |
| Model lifecycle | Installed model inventory, load/unload/delete, loading feedback, memory checks, and backend-specific context controls | Models |
| Model evaluation | Tool and coding-workflow checks, queued runs, saved results, JSON reports, and repair-profile reset | Models → Test tools |
| Coding API gateway | Authenticated HTTPS access to backend APIs, generated OpenCode/Continue configurations, certificate instructions, LAN and Tailscale addresses | Help; host gateway setup required |
| Tailscale remote access | Private browser access through configured Serve, address detection, backend tests, and separate LAN/Tailscale coding configurations | [Tailscale setup](#tailscale-remote-access-for-chat-and-coding-models) |
| Persistent coding agents | Guided task creation, separate assignment and live execution pages, real-time agent Activity with timestamps, private workspaces, searchable read-only sharing, recurring schedules with local run times, admin-controlled access, and optional result checks | Users; Agents; OpenCode required |
| Personal memory | Per-user conversation history and cross-chat retrieval using local embeddings | [Cross-chat memory](#cross-chat-memory) |
| Codebase retrieval | Continue embedding configuration for codebase indexing; OpenCode uses its own project file/search tools | [Codebase RAG](#codebase-retrieval-for-coding-clients) |
| Web research | Automatic search detection, page retrieval, optional headless Chromium scraping, and browsing history | [Web search](#web-search-and-browsing); Browse history |
| Voice | Microphone transcription, spoken replies, and hands-free conversation with configured speech models | [Voice](#voice-translation-images-and-video) |
| Translation | Local voice, image, and video translation with configured compatible models | Chat translation controls |
| Image and video creation | Image generation and short video workflows through ComfyUI | [Image generation](#voice-translation-images-and-video); [Video generation](#voice-translation-images-and-video) |
| Study and practice | Built-in and user-created quiz decks, generated questions, and state DMV practice | Quizzes; DMV |
| Performance and diagnostics | Live requests, recent request filters, token usage, throughput, latency, backend/client attribution, and supported NPU statistics | Metrics; system statistics |
| User administration | Accounts, roles, password resets, per-user agent workflow access, per-user data, and gateway-token controls | Users |
| Runtime settings | Backend URLs and credentials, connection tests, context settings, and configured Tailscale controls | Settings |
| Host security checks | Administrator-triggered security scans and reports using the configured host tools | [Security scanner](#metrics-and-diagnostics) |
| FLM conversion tools | Conversion profile inspection and validation; job execution requires the matching converter and validated artifacts | FLM converter; experimental |
| Self-hosted operation | Clean PostgreSQL provisioning, application DDL initialization, first admin setup, and systemd boot startup | [Installation](deploy/FIRST_TIME_INSTALL.md) |

Feature availability follows the components you install and configure. Speech,
translation, embeddings, ComfyUI media generation, coding agents, gateway access,
and NPU inference each require their corresponding runtime, models, or host setup.
The experimental custom FLM conversion path remains unavailable without its required
validation and kernel/ABI artifacts.

## Local LLM testing and tool-calling evaluation

Use Hearth to evaluate whether a local language model can follow the tool contracts
needed by an AI coding agent. The **Models** page includes model tests with saved
results, so you can inspect failures by model and backend rather than relying only
on how convincing a chat response sounds.

The evaluation workflows cover:

- Automatic, required, named, and disabled tool choices.
- JSON argument types, required fields, exact file content, and multiple tool calls.
- Streaming and non-streaming OpenAI-compatible responses.
- Multi-step coding scenarios: reading files, editing code, handling reported
  failures, and responding to verification results.
- Interactive agent behavior: single-choice and multiple-choice questions, custom
  answers, dismissed questions, permission denials, and task continuations.
- Compatibility diagnostics for model runtimes, response parsers, and tool schemas.

These checks evaluate responses against simulated tool results; they do not execute
model-generated commands. They are a practical tool-use evaluation suite, not a
general intelligence benchmark. A passing check establishes only the behavior tested
for that model, backend configuration, and run. See
[model tests in the Models page](#test-and-compare-models-before-coding) for details and
[saved failure reviews](reports/saved-failure-review-2026-10-19.md) for known limitations.

## Agentic AI and local coding agents

Hearth's **Agents** workspace supervises real OpenCode sessions against a local
project directory. Choose a configured local model, describe a task, follow its
progress, and respond when the agent needs input. Agent task supervision and
model testing serve different purposes: agent sessions work on real projects;
the evaluation suite checks model responses using simulated workflows.

For editor and terminal workflows, Hearth also provides integration with
**OpenCode** and **Continue** through backend-specific OpenAI-compatible proxy
endpoints. Compatibility layers address supported tool-call formats, argument
handling, and request metrics. Actual coding reliability depends on the model,
available context, runtime, and task; the saved reports document remaining failures.

## A personal AI assistant on your own hardware

Use Hearth as a self-hosted personal AI workspace for everyday chat, research,
coding assistance, voice interaction, and study. Saved conversations and cross-chat
memory let you return to earlier topics. Model controls let you try different
local LLMs without moving between separate chat interfaces.

Local inference keeps model generation on the configured hardware. Web search,
model downloads, and any external services you configure can use the network.
Chat history, application state, and Settings values use PostgreSQL; local model
runtimes provide inference separately from the Flask application.

## Frequently asked questions

### Is Hearth a free AI WebUI?

Hearth can be self-hosted without a Hearth subscription. You supply the hardware,
model weights, and inference runtime. Model licenses and any separately configured
services have their own terms and costs.

### Can I test local models before using them as coding agents?

Yes. Use the Models page to inspect tool-calling and workflow test results for a
selected model. Review both failed checks and dependent steps that could not run.
See the saved reports for evidence about previously evaluated configurations.

### Which local AI backends does Hearth support?

The main integrations are LM Studio, Lemonade, vLLM, KoboldCPP, and FastFlowLM.
Hardware acceleration and model support depend on the runtime you install.
AMD Ryzen AI NPU workflows use the relevant Lemonade or FastFlowLM configuration;
not every model or backend supports every device.

### Can I use Hearth as a local ChatGPT-style interface?

Yes. Hearth provides browser-based assistant chat with local models, saved history,
memory retrieval, and optional search, voice, and generation integrations. Feature
availability depends on which supporting components you configure.

## Requirements

- Python 3.10+ and venv support.
- Docker Engine with Docker Compose, for PostgreSQL 18.
- At least one supported local model runtime. For LM Studio, run its headless
  daemon at `http://localhost:1234` with the `lms` CLI available at
  `~/.lmstudio/bin/lms`. Other backends are configured from Settings.

## First-time setup

Follow [the first-time installation guide](deploy/FIRST_TIME_INSTALL.md). On Ubuntu,
from the project directory:

```bash
bash deploy/install-first-time.sh
```

This single installer adds missing Ubuntu packages with sudo, prepares the Docker
agent image and Python environment, installs requirements and Chromium, starts
PostgreSQL in Docker Compose, loads the application DDL (including agent schedule
tables), and generates private database credentials. On the first page
visit, Hearth asks you to choose the administrator username and password, then
stores the account in PostgreSQL. The username can be changed on the setup form.
The installer enables the systemd user service and lingering, so Hearth and its recurring schedule worker
start after a reboot, even before login. Schedules are saved in PostgreSQL and resume on service startup;
if a run became due while Hearth was stopped, it runs once after startup and skips
missed intervals. With `--no-service`, scheduled runs start only while you run
Hearth manually. See the [installation guide](deploy/FIRST_TIME_INSTALL.md) for
first-run account setup. Model backends and weights are installed separately.

For a manual app process, use `bash deploy/install-first-time.sh --no-service`.
Chromium is optional (`--skip-browser`); web search falls back to snippets and plain
HTTP when browser scraping is unavailable. Voice input uses the system FFmpeg binary,
which the installer adds when missing.

## Run

The default installer starts the user service. Use the following command only when
you installed with `--no-service` or stopped the service first:

```bash
source .venv/bin/activate
python app.py
```

On first run this generates a self-signed TLS cert/key in `certs/`. The server then listens on
`https://0.0.0.0:5443` — open `https://localhost:5443` in a browser and accept the
self-signed certificate warning (one-time per browser).

The UI is protected by a login page backed by a `users` table. Login uses a browser-session
cookie (closing the browser requires a fresh login) and expires after 30 minutes without a
request.

Chat responses continue when you switch tabs, change conversations, navigate to another
page, or close the browser. Returning to the chat restores its current progress or saved
reply. The Stop button explicitly cancels a text response and keeps its partial answer.
Answer text and supported separate reasoning content appear as the backend streams them;
models that emit no separate reasoning events show the answer as it arrives.
Active jobs run in the web service; completed replies remain in PostgreSQL chat history.
Restarting the web service interrupts active jobs, so wait for responses to finish before
restarting it.

User accounts share the PostgreSQL database with chat history. After a fresh install,
the first visit to Hearth displays a one-time administrator setup form. Choose the
username and enter and confirm a password; the first account is created as an admin
and you are signed in. The database serializes this operation, so only one first account
can be created even if multiple browsers open setup at once. Later visits show the
normal sign-in form. Existing users are preserved on installer reruns.

Account profile fields—username, role, creation time, gateway token, and agent access
setting—and salted, one-way Werkzeug password hashes are stored only in the PostgreSQL
`users` table. Plaintext passwords are not stored or returned by user APIs. Hashing is
intentional: passwords should be verified, not reversibly encrypted. The installer
removes legacy `ADMIN_USERNAME` and `ADMIN_PASSWORD_HASH` entries from `.env` on reruns.

Environment variables:

- `DATABASE_URL` — required PostgreSQL connection URL, generated during setup
- `.env.postgres` — Compose database credentials and host port (default `55432`)
- `HOST` — bind address (default `0.0.0.0`)
- `PORT` — port (default `5443`)

Backend URLs, API keys, model defaults, and other **Settings** values are stored in
PostgreSQL and managed from the admin Settings page. On upgrade, legacy backend URL
and API-key variables are imported once and removed from `.env`; API keys are encrypted
at rest. Keep `.env` for the database connection and deployment-level runtime values.

Setup generates or imports Hearth's Flask session-signing key and stores it encrypted
in PostgreSQL before service start; direct app starts do this on first launch.
Encryption derives from the PostgreSQL connection password;
the app migrates an older `.env` session key into the database and removes it from the
file. Changing the database password makes Hearth rotate the session key, so users
will need to sign in again.

## Users and settings

Administrators manage accounts and roles in **Users**, and backend URLs, credentials,
connection tests, and context controls in **Settings**. Backend URLs and API keys are
stored in PostgreSQL; API keys are encrypted at rest. Legacy backend entries in `.env`
are migrated at startup and removed from that file. Regular users can chat and
select already-loaded models. Model management, downloads, evaluation, metrics,
and agent supervision require an administrator.

Use Users to reset passwords and manage gateway tokens. Apply the supplied Caddy
configuration when adding or rotating gateway credentials. Hearth preserves the
last administrator account and prevents administrators from deleting themselves.

## Personal memory and web research

### Cross-chat memory

Cross-chat recall embeds messages in PostgreSQL, scopes retrieval by user, and
excludes the current conversation. It uses Lemonade embeddings, deduplicates
near-identical chunks, and retrieves up to eight relevant chunks with a minimum
similarity of 0.35. Short conversational facts may score below repeated questions,
so both the similarity threshold and retrieval window matter. Deleting a
conversation or account also deletes its associated memory chunks.

## Voice, translation, images, and video

- **Voice:** microphone transcription, spoken replies, and hands-free conversation
  with voice activity detection, using configured speech models. FFmpeg is required
  for audio processing.
- **Translation:** voice, image, and video translation through configured compatible
  models, including the supported local Gemma workflow.
- **Images:** generation through a configured ComfyUI image workflow, with images
  displayed in chat and saved locally.
- **Video:** short video generation through a configured ComfyUI video workflow.
  The documented workflows include SD-Turbo for images and Wan 2.1 for video.

Install the corresponding runtimes and model weights separately. Generation speed,
resolution, and supported inputs depend on the selected workflow and hardware.

## Practice tests and quizzes

Open **Quizzes** for built-in and user-created decks, generated questions, and
practice sessions. **DMV** provides state driving-test practice. Quiz content and
supported study workflows are available from the app's navigation.

## Persistent project agents

Open **Agents** and provide a goal, acceptance criteria, and configured OpenCode
model. Administrators can grant or revoke Agent access separately for each user on
the **Users** page; new regular users start with it disabled, while admins start
with it enabled. Disabling access blocks the page and API and requests stops for
that user's active assignments. PostgreSQL stores each account's private project
workspace. Hearth bundles the OpenCode worker in its agent container. The selected
local model requires at least 16K context; the model picker excludes configurations
that cannot fit its tools and prompt.

PostgreSQL stores assignment records, activity and message history, sharing rules,
runtime metadata, and private project files. Existing file-backed agent tasks and
workspaces are imported into the database on upgrade and their migrated host
copies are removed. A running isolated container has a temporary working copy so
OpenCode can use normal project tools; Hearth synchronizes that copy to PostgreSQL,
which remains the durable workspace source.
The live task stream includes each latest agent response, completion note, and
verification result, so the result panel updates when the task finishes. Interrupted
tasks keep their latest response visible alongside the error and can be resumed.

Agent assignments and project files are private to their owner by default,
including from other administrators. An owner can explicitly share assignment
information with registered users who have Agent access enabled. Shared users can
view task details and activity and download the completed project, but cannot
change instructions, answer prompts, control, or delete the task. The owner can
revoke access at any time from the assignment's **Share assignment information**
panel. Sharing does not grant direct access to the owner's private workspace.

Create an agent from **New agent** by giving the task a name and describing the result
you want in everyday language. Optional model and control settings are grouped under
**More options**. The **My agents** page lists tasks and their plain-language status;
open one to see its separate execution page. There, the **Activity** feed streams the
agent progress, container tool inputs and outputs, and user-facing model responses
from the active OpenCode session over a task-scoped live event stream, with correctly
formatted timestamps and a live indicator while it is working. Private reasoning
traces are not displayed. The stream is limited to tasks the signed-in user owns or
has been explicitly allowed to view; the global chat metrics stream is not used for
agent content. Answer questions, approve or reject permission
requests, add instructions, and pause, continue, or stop work from the same page. Detailed activity
The activity feed and full activity history always put the newest timestamp at the top,
including when live events arrive out of order. When an assignment is
paused, blocked, or interrupted, its owner can choose another configured model before
continuing. The model picker defaults to the currently loaded model when it is available;
the owner can still select another configured model. Loaded support models such as
embeddings do not hide the loaded coding model from that default, including LM Studio
models reported through their loaded-instance list. A model change continues in a fresh OpenCode session from the latest saved
workspace checkpoint and carries forward the previous agent summary. Tasks persist when you close the browser, and Hearth
reconnects to existing sessions after restarting. Assignments interrupted by a Hearth
restart can be resumed from the task page.
After reviewing a running, waiting, paused, blocked, or interrupted assignment, its owner can use
**Reset cycles** to set the work-cycle count back to zero without changing the saved
workspace or model, then choose **Continue** when ready.
Startup cleanup preserves containers with matching PostgreSQL task and runtime records;
if a container is missing, resuming rebuilds it from the saved project files.

If OpenCode reports an idle session before a new assistant result is available, Hearth
keeps polling for up to 30 minutes. A session that remains idle after that is marked
**Needs review** once, rather than accumulating repeated worker errors; the selected
model remains attached to the assignment when it is resumed.
Provider transport resets such as `ECONNRESET` are retried automatically in the same
assignment session; a failed assistant response is retried as a new work cycle.

Use **Recurring agent schedules** to save a task goal, reusable agent comment,
model, and repeat interval from 5 minutes up to 365 days. Daily or weekly schedules
can specify a local run time using the browser timezone shown in the form. Without a
run time, the first run starts after one interval; timed schedules start at the next
matching local time and keep that wall-clock time across daylight-saving changes.
Later runs create separate assignments. Hearth skips a scheduled
occurrence until that schedule's previous assignment is completed or stopped, so
runs do not overlap unfinished work. You can pause, resume, or delete a schedule
without deleting its past assignments. Schedules run only while the Hearth service
is running, and disabling the owner's Agent access prevents scheduled runs from
starting.

Each assignment runs its own Docker container with OpenCode, Python 3, Java (JDK and
Maven), Node.js, npm, git, and common build tools. The agent can install additional
OS and language packages inside the container with `apt`, `pip`, `npm`, and other
package managers. Package installation and shell commands run inside that
container. Docker applies memory, CPU, and process limits, drops unnecessary Linux
capabilities, and disables privilege escalation. The container receives neither a
host project bind mount nor the Docker socket. Hearth copies safe regular project
files into and out of the account's private agent workspace; symlinks and common
generated dependency/cache directories are excluded from synchronization. Outbound
networking supports package downloads and model calls, but Docker egress is not
restricted to an allowlist.

The default container limit is 8 GiB RAM, 4 CPUs, and 512 processes. Administrators
can tune these in `.env` with `HEARTH_AGENT_MEMORY_LIMIT` (512m–256g),
`HEARTH_AGENT_CPU_LIMIT` (0.5–64), and `HEARTH_AGENT_PID_LIMIT` (64–4096) for
larger builds.

At assignment time, choose to ask before each shell command or auto-accept shell
commands. When a prompt appears, allow it once, always for the proposed command
pattern, or reject it. Completed projects can be downloaded as a `.tar.gz` archive.
Stopped assignments can be deleted; active assignments must first be stopped or
paused.

Provide a verification command when you want independently checked completion.
Hearth runs it and requires a successful exit before marking the assignment complete.
Without one, completion is labelled **agent-reported**. Questions, blockers, and
budget limits remain visible for review.

## Metrics and diagnostics

Open **Metrics** to inspect request activity, latency, generated tokens, throughput,
backend/client breakdowns, and supported NPU statistics. Recent-request filters and
live activity help diagnose slow responses or identify which client is generating.
Available measurements depend on the backend and configured proxy instrumentation.

For coding clients, send a stable attribution header such as:

```http
X-Hearth-Client: my-coding-project
```

Administrators can also run the configured host security scanner from chat. Host
tools and any required privileges must be installed separately; reports help inspect
the host's current configuration.

## Running as a service

The default installer creates the `ai-webui` systemd user service:

```bash
systemctl --user status ai-webui
systemctl --user restart ai-webui
systemctl --user stop ai-webui
journalctl --user -u ai-webui -f
```

To keep it running without an active login, enable lingering:

```bash
sudo loginctl enable-linger "$(id -un)"
```

The PostgreSQL Compose container uses an `unless-stopped` restart policy; Docker
must be enabled at boot. To check the database:

```bash
docker compose --env-file .env.postgres ps
```

For updates, reinstall requirements and restart after active work has finished:

```bash
.venv/bin/python -m pip install -r requirements.txt
bash deploy/install-first-time.sh --rebuild-agent-image
systemctl --user restart ai-webui
```

The first-time installer prepares the isolated agent image automatically. Rebuild
it after updating the container recipe or OpenCode version. The image includes
Python, Java/Maven, Node.js/npm, and build tools; task package installation stays
inside each task container.

Keep `.env`, `.env.postgres`, and TLS private keys private. Preserve the database
volume across application updates; `docker compose down -v` deletes it.

## Disclaimer

Use Hearth at your own discretion and risk. AI models can produce inaccurate,
misleading, offensive, insecure, or otherwise harmful output. Model responses are
not professional advice and should not be treated as verified facts. Check important
claims and review generated code before relying on, running, or sharing it.

Agent features can read, create, modify, or delete files and may run shell commands
or install packages according to the permissions and settings you choose. Agents can
make mistakes, misunderstand instructions, or cause unintended changes. Review
proposed commands and file changes, restrict access to sensitive data, and keep
independent backups. You are responsible for actions you approve or configure agents
to run automatically.

You are also responsible for model downloads, model licenses, third-party services,
and any costs, security, privacy, or legal requirements associated with their use.
The authors and maintainers provide this software without guarantees and are not
responsible for damages, data loss, security incidents, or other consequences arising
from its use, to the extent permitted by applicable law.

## Development and troubleshooting

Run the application checks through the isolated PostgreSQL test runner:

```bash
.venv/bin/python tools/run_tests.py --browser
```

With `--browser`, the unit suite and all six Playwright suites run concurrently. Each
browser suite receives its own temporary PostgreSQL schema, and all schemas are removed
when the run finishes.

For installation details, see [the setup guide](deploy/FIRST_TIME_INSTALL.md).
For model-specific test evidence, see [saved failure reviews](reports/saved-failure-review-2026-10-19.md).
Repair history, debugging investigations, and implementation notes are preserved in
[FIXLOGS.md](FIXLOGS.md). Those notes describe changes over time and may include
behavior superseded by later fixes; use this README for current setup and usage.
