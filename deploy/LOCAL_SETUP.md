# Hearth on example-host

Configured initially on October 13, 2026.

For a new machine, use [FIRST_TIME_INSTALL.md](FIRST_TIME_INSTALL.md). The commands
and model inventory below describe the existing example-host host, not a generic installer.

Open https://machine.example.ts.net:8443 while connected to Tailscale.
Tailscale Serve provides trusted HTTPS and proxies to Hearth's local TLS server
on port 5443. The existing RF Signals route on port 443 is preserved.

## Basic models

| Feature | Model | Verification |
| --- | --- | --- |
| Default chat and vision | Qwen3.5 4B, Q4_K_M, with BF16 vision projector | Chat response and apple recognition |
| Lightweight chat / backend checks | Qwen3 0.6B, GGUF Q8_0 and Hugging Face weights | LM Studio, KoboldCPP and vLLM chat responses |
| NPU chat | Llama 3.2 1B NPU2 | FastFlowLM chat response |
| Memory | Nomic Embed Text v1 GGUF through Lemonade | 768-dimensional embedding |
| Speech recognition | Whisper Tiny | Transcribed synthesized speech correctly |
| Spoken replies | Kokoro v1, af_heart voice | Generated MP3 audio |
| Voice translation | Gemma 4 E4B IT NPU2, including audio and vision weights | English speech translated into Spanish |
| Images | SD-Turbo | Generated and visually checked a 512×512 PNG |
| Video | Wan 2.1 T2V 1.3B, UMT5 FP8 encoder, Wan VAE | Generated and inspected a five-frame MP4 |

Nomic Embed Text v1.5 is also installed for LM Studio. FastFlowLM exposes
Gemma's flash variant from the same downloaded weights.

## PostgreSQL database

Hearth and its three proxies use PostgreSQL 18 in Docker Compose, bound to
`127.0.0.1:55432`. `.env` supplies `DATABASE_URL`; `.env.postgres` holds the Compose
credentials.

```bash
docker compose --env-file .env.postgres ps
.venv/bin/python tools/run_tests.py --browser
```

## Runtime and services

Hearth uses its existing Python 3.14 environment. ComfyUI, vLLM and the generic
FLM converter have separate Python 3.12 environments under
`~/.local/share/hearth`. PyTorch and vLLM use CPU builds on this AMD machine.
LM Studio and KoboldCPP have working GPU inference; FastFlowLM has working NPU
inference with its system service's unlimited memory-lock allowance.

Enabled user services: `ai-webui`, `lmstudio`, `comfyui`,
`lemonade-guard-shim`, `lmstudio-shim`, and `flm-tool-shim`. User lingering is enabled.
Lemonade runs through its installed snap daemon. `kobold`, `vllm` and the
system `flm` service start when selected, rather than loading all chat models
at once. The final selected default is `lmstudio/qwen/qwen3.5-4b` with a
32,768-token context. Restarting Hearth preserves an already-loaded instance.

FFmpeg, Chromium, Caddy, Lynis, Vulkan libraries, development tools and the
FastFlowLM/XRT packages are installed. Browser and persistent Python MCP tools
passed live calls. The narrow Lynis sudoers rule is configured for `HEARTH_USER`.

Authenticated Caddy backend ports:

| Backend | Port |
| --- | --- |
| LM Studio | 9443 |
| Lemonade | 8444 |
| vLLM | 8445 |
| KoboldCPP | 8446 |
| FastFlowLM | 8447 |

The app's Help page provides credentials and client configuration. These
backend ports use Caddy's local CA; the browser app's Tailscale URL uses a
publicly trusted certificate. LM Studio uses 9443 because 8443 belongs to
the browser app on Tailscale.

## Checks and limits

Hearth's authenticated main, Settings, Models, Downloads, chat history and
system statistics routes returned 200. Trusted Tailscale HTTPS returned 200.
All three Python runtime dependency checks passed. The PostgreSQL suite
covers startup, MCP, model handoffs, and first-time setup. Fresh database/admin creation and idempotent installer reruns were checked
in a separate disposable Compose deployment.

Media generation runs on CPU. The 512×512 one-step image check took about
15 seconds; the 256×144 five-frame, twelve-step video check took about
88 seconds. Larger jobs take longer.

The generic FLM converter is installed and its CLI works. The experimental
custom 27B conversion feature remains unavailable: its project-specific
`validate_profile.py` and validated kernel/ABI artifacts are not part of the
upstream converter. Its readiness checks have not been bypassed.

To reinstall the host packages or proxy configuration, run in a local terminal:

```bash
sudo bash /path/to/ai-webui/deploy/install-host-deps.sh
```

For application logs:

```bash
journalctl --user -u ai-webui -f
```
