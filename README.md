# Hearth

**A self-hosted workspace for local AI chat, model evaluation, and coding agents.**

Hearth brings supported model runtimes into one web app. Chat with local or hosted
models, manage model files, evaluate model behavior, and run OpenCode coding tasks
with live progress. PostgreSQL stores conversations, users, settings, reports, and
agent records; model weights and generated media remain in configured local storage.

[Install Hearth](#installation) · [Features](#features) ·
[Supported backends](#supported-model-backends) ·
[Development](#development)

## Features

| Area | Capabilities |
|---|---|
| Chat | Streamed responses and supported reasoning, saved conversations, model selection, file attachments, Markdown, and context-aware history handling |
| Model management | Search supported catalogs and Hugging Face, download and upload supported models, load or unload local models, and configure per-model context and reasoning preferences |
| Model evaluation | Compatibility and tool-use checks, reasoning and safety evaluations, saved reports, and user-managed test scenarios |
| Coding agents | OpenCode project tasks, live activity, project checkpoints, verification commands, schedules, and reusable agent skills |
| Research and memory | Web search and browser-based page retrieval, searchable browse history, and cross-chat recall using local embeddings |
| Voice and translation | Microphone transcription, spoken replies, and supported local text, image, and video translation workflows |
| Image and video | Local image generation through Lemonade and Anima-Turbo; text-to-video with Wan 2.2 through Lemonade's bundled sd.cpp runtime |
| Hosted models | Optional opencode.ai catalog access for chat and agent assignments |
| Coding clients | Authenticated OpenAI-compatible endpoints and generated configurations for OpenCode, Continue, and compatible clients |
| Monitoring and learning | Request metrics, live logs, system statistics, quizzes, and DMV practice tests |
| Administration | User accounts and roles, backend settings, service controls, and host security reports |

Feature availability depends on the configured runtime, installed models, and host
hardware. See the [installation guide](deploy/FIRST_TIME_INSTALL.md) for prerequisites
and operational details.

## Architecture

Hearth provides the web interface and coordinates supported inference backends. It
stores application data in PostgreSQL and keeps model execution in the selected local
runtime or optional hosted provider.

<p align="center">
  <img src="docs/architecture.svg" alt="Hearth web app connected to local and hosted model backends, coding agents, and PostgreSQL" width="100%">
</p>

## Supported model backends

| Backend | Typical use | Notes |
|---|---|---|
| [LM Studio](https://lmstudio.ai) | Local chat and GGUF models | CPU or GPU execution supported by the installed runtime |
| [Lemonade](https://github.com/lemonade-sdk/lemonade) | Local chat, speech, image, and video models | Supports compatible CPU, GPU, and Ryzen AI NPU recipes |
| [vLLM](https://github.com/vllm-project/vllm) | Local model serving | Connect Hearth to a configured vLLM OpenAI-compatible endpoint |
| [KoboldCPP](https://github.com/LostRuins/koboldcpp) | Local GGUF chat models | CPU or GPU execution supported by the installed build |
| [FastFlowLM](https://github.com/ROCm/FastFlowLM) | Ryzen AI NPU chat models | Runs as a separate host service |
| q38rocm | Qwen 3.8 local inference | Optional Radeon Vulkan backend; see the setup guide |
| [opencode.ai](https://opencode.ai) | Optional hosted chat and coding models | Account and plan access are managed by the provider |

The local backends are optional except for components required by a chosen feature.
Hearth does not bundle model weights. Check each model's license and hardware needs
before downloading it.

## Screenshots

[![Hearth feature demo](docs/screenshots/hearth-feature-demo.gif)](docs/screenshots/hearth-feature-demo.mp4)

| Chat | Models |
|---|---|
| ![Hearth chat workspace](docs/screenshots/chat.png) | ![Hearth model management](docs/screenshots/models.png) |

| Model tests | Coding agents |
|---|---|
| ![Hearth model tests and saved results](docs/screenshots/model-tests.png) | ![Hearth agent task editor](docs/screenshots/agents.png) |

## Installation

Choose the setup path that matches your host:

- **Ubuntu:** From the project checkout, run `bash deploy/install-first-time.sh`. This
  installs and configures the app, PostgreSQL, and required host components.
- **Windows, macOS, or Linux release bundle:** Download a matching bundle from GitHub
  Releases and follow its included setup instructions.
- **Linux Docker deployment:** Run `bash deploy/install-docker.sh` for the containerized
  app deployment.

The first visit prompts you to create the administrator account. Configure at least
one model backend in **Settings**, then download and load a model from **Downloads** or
**Models**. See [deploy/FIRST_TIME_INSTALL.md](deploy/FIRST_TIME_INSTALL.md) for the
complete installation, backend, networking, and maintenance instructions.

## Local model evaluation

The **Model tests** page evaluates supported chat models with compatibility, reasoning,
safety, and tool-use checks. Results include per-check details and response previews.
Administrators can also create reusable test scenarios with expected answers. These
checks help compare configurations; they do not certify model quality or host security.

## Coding agents and client access

Hearth can run project tasks through OpenCode in isolated agent environments. Tasks can
include project files, requirements, verification commands, and reusable skills. The
Agents pages show progress and results, and support scheduled work where configured.

For desktop or LAN coding clients, Hearth can generate OpenCode and Continue
configurations for supported model endpoints. The gateway uses authentication and TLS;
remote access can use Tailscale when the host and client devices are configured for it.
Hearth does not configure tailnet access policies for you.

## Context handling

Hearth fits chat history and output budgets to the model's active context when available.
It reserves space for response generation, chat formatting, and tool definitions. For
unloaded Lemonade models, Hearth uses the saved context setting or the runtime's
4,096-token auto-load default rather than assuming the model's advertised maximum.

OpenCode compaction budgets scale with the selected model's loaded context. When Keep
and Buffer are blank, Hearth calculates them as follows:

```text
buffer         = max(1, context // 12)
prompt_reserve = max(1, context // 64)
keep           = max(0, context - buffer - prompt_reserve)
```

For a 65,536-token context, this yields a 5,461-token buffer, a 1,024-token prompt
reserve, and 59,051 tokens to keep. Explicit settings are bounded so the combined
budget fits the loaded context.

## Data, access, and safety

- PostgreSQL stores accounts, conversations, settings, model-test reports, metrics, and
  agent task records. Back up PostgreSQL and any local files you want to retain.
- Model files and generated media are stored in configured local directories or backend
  caches. Their storage requirements depend on the models you choose.
- User accounts and roles protect Hearth pages. The coding gateway uses separate
  authenticated endpoints; configure TLS and network access for your environment.
- Chat requests go to the selected local backend or optional hosted provider. Web
  research and other integrations may contact the providers you configure.
- Review model licenses and provider terms. Use Hearth and model outputs at your own
  risk; Hearth does not provide professional financial or medical advice, verify every
  model response, or guarantee that generated content is accurate or suitable.

## Development

Application code is organized under `hearth/` by domain. `app.py` is the web entry
point, `deploy/` contains installation and service configuration, and `tests/` contains
the regression suite.

Run the project checks from the repository root:

```bash
.venv/bin/python tools/run_tests.py
```

To include browser coverage:

```bash
.venv/bin/python tools/run_tests.py --browser
```

For environment setup and deployment options, see the
[installation guide](deploy/FIRST_TIME_INSTALL.md).
