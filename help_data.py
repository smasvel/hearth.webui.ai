"""Runtime generation of the Help panel's machine-specific data.

Everything the Help panel needs that is specific to *this* machine — the LAN IP, the Caddy
bearer token and proxy ports, the Caddy CA root certificate, and the Continue / OpenCode
config blocks — is derived here from live sources at request time, so the help never goes
stale by hand:

  * LAN IP        — a UDP socket's local address (no packets actually sent)
  * token + ports — parsed from /etc/caddy/Caddyfile (world-readable)
  * CA root cert  — Caddy's admin API (GET :2019/pki/ca/local -> root_certificate);
                    the root isn't in the served TLS chain, but the admin API has it
  * models        — the same live LM Studio / Lemonade / vLLM lists the Models panel uses

The two config blocks (Continue YAML, OpenCode JSON) are then generated from those values +
the current model inventory, so a freshly downloaded model appears automatically.
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
from urllib.parse import urlsplit

import requests

import lmstudio_client as lms
import lemonade_client as lemonade
import vllm_client as vllm
import kobold_client as kobold
import fastflowlm_client as fastflowlm
import settings_store
import flm_context

CADDYFILE = "/etc/caddy/Caddyfile"
CADDY_ADMIN = "http://localhost:2019"
CERT_FILENAME = "hearth-ca.crt"

# Sensible fallbacks if a source is briefly unreachable, so the help still renders.
_DEFAULT_PORTS = {"lmstudio": 8443, "lemonade": 8444, "vllm": 8445, "kobold": 8446, "fastflowlm": 8447}
_UPSTREAM_BACKEND = {
    "1234": "lmstudio",
    "13307": "lmstudio",
    # 13305 = Lemonade's own server; 13306 = lemonade_guard_shim.py, which Caddy's lemonade
    # block now proxies to instead so a LAN request can't trigger the NPU-collision OOM that
    # used to hit flm.service (see that module's docstring) — either upstream port means
    # "this Caddy block is the lemonade one."
    "13305": "lemonade", "13306": "lemonade",
    "8000": "vllm", "5001": "kobold",
    # 52625 = FastFlowLM's own server; 52626 = flm_tool_shim.py, which Caddy's fastflowlm block
    # now proxies to instead so LAN clients get real tool_calls (see that module's docstring) —
    # either upstream port means "this Caddy block is the fastflowlm one."
    "52625": "fastflowlm", "52626": "fastflowlm",
}
_LABELS = {"lmstudio": "LM Studio", "lemonade": "Ryzen AI NPU", "vllm": "vLLM", "kobold": "KoboldCPP", "fastflowlm": "FastFlowLM"}
# FastFlowLM matches flm.service and stays below the NPU kernel limit, including
# runtime padding. Lemonade uses its own context configuration.
_CTX = {"lmstudio": 32768, "lemonade": 65536, "vllm": 8192, "kobold": 8192, "fastflowlm": 8192}
_OUT = {"lmstudio": 8192, "lemonade": 8192, "vllm": 4096, "kobold": 4096, "fastflowlm": 4096}
_BACKENDS = ("lmstudio", "lemonade", "vllm", "kobold", "fastflowlm")
_NON_CHAT = ("embeddings", "transcription", "tts")

# Compaction settings for the generated OpenCode config. OpenCode's own default keep.tokens is
# 15000, which on a small-context model (e.g. the 32768-context LM Studio default) leaves too
# little room for the summarization request itself - the session fails with "The compaction
# request cannot be reduced further without losing the latest exchange or checkpoint". An admin
# can lower keep.tokens / raise buffer from the Settings panel; keep.tokens follows OpenCode's
# 15000 default, buffer defaults to unset (the generated config omits it, letting OpenCode use
# its own 10%-of-limit default). See build_opencode_config().
DEFAULT_COMPACTION_KEEP_TOKENS = 15000


def compaction_settings() -> dict:
    """Effective compaction settings, re-read on each config build so an admin's Settings change
    is picked up the next time the Help page regenerates the OpenCode config."""
    raw_keep = settings_store.get("opencode_compaction_keep_tokens")
    try:
        keep = int(raw_keep) if raw_keep not in (None, "") else DEFAULT_COMPACTION_KEEP_TOKENS
    except (TypeError, ValueError):
        keep = DEFAULT_COMPACTION_KEEP_TOKENS
    buffer: int | None = None
    raw_buffer = settings_store.get("opencode_compaction_buffer")
    if raw_buffer not in (None, ""):
        try:
            buffer = int(raw_buffer)
        except (TypeError, ValueError):
            buffer = None
    return {"keep_tokens": max(0, keep), "buffer": buffer}


def opencode_context_settings() -> dict:
    """Admin controls for how much context the generated OpenCode config advertises.

    advertise_loaded (default true): use each model's *real* loaded context when the backend
    reports one (LM Studio's loaded_context_length, Lemonade's recipe_options.ctx_size, vLLM's
    registry max_model_len, KoboldCPP's KOBOLD_CONTEXT). This is what fixed a real report:
    LM Studio had `qwen3.8-2b-distill` loaded at 8192, but the config hardcoded 32768 for every
    LM Studio model, so OpenCode packed ~30k tokens into a request the server rejected with
    "request (29899 tokens) exceeds the available context size (8192 tokens)".

    fallback: optional single override for models whose real context can't be determined
    (unloaded, or a backend that doesn't report one). Blank keeps each backend's tuned _CTX
    default, which is deliberate — the backends genuinely differ (FastFlowLM/Lemonade serve
    65536, vLLM serves a small CPU context), so one global number would be wrong for most."""
    advertise = (settings_store.get("opencode_advertise_loaded_context") or "true") == "true"
    fallback: int | None = None
    raw = settings_store.get("opencode_context_fallback")
    if raw not in (None, ""):
        try:
            fallback = int(raw)
        except (TypeError, ValueError):
            fallback = None
        if fallback is not None and fallback <= 0:
            fallback = None
    return {"advertise_loaded": advertise, "fallback": fallback}


def _advertised_context(be: str, model_id: str, ctx_by_model: dict, fallback: int | None) -> int:
    """Context to advertise for one model: the backend's reported real context when known
    (ctx_by_model), else the admin's global fallback, else this backend's tuned default."""
    if be == "fastflowlm":
        return flm_context.context_limit(model_id)
    detected = ctx_by_model.get((be, model_id))
    if isinstance(detected, int) and detected > 0:
        return detected
    if fallback:
        return fallback
    return _CTX[be]


def _advertised_output(be: str, context: int, model_id: str | None = None) -> int:
    """Output cap, never more than half the (possibly tiny) context. Without this a model
    whose real context is 8192 would keep the old fixed output of 8192 — an output budget
    equal to the entire window, leaving no room for the prompt (and making compaction
    impossible). Half is a safe reserve; unchanged for every backend that was already
    narrower (e.g. 32768 context / 8192 output)."""
    if be == "fastflowlm":
        return flm_context.output_limit(model_id, context)
    return max(1, min(_OUT[be], context // 2))


def flm_small_agent(model):
    """A primary OpenCode agent with a small fixed prompt/tool footprint."""
    return {
        "description": "Coding with a compact prompt and tool set for FLM",
        "mode": "primary", "model": model, "temperature": 0,
        "prompt": (
            "Complete the user's coding task in the current project. First read project "
            "instructions (AGENTS.md), README, and the relevant package/build configuration. "
            "Use project-relative paths such as src/module.py; never prepend a slash to a "
            "relative path from README. Read README before searching. Use glob and grep to locate the entry points, implementation, and existing tests; "
            "read the relevant code before drawing conclusions. Follow imports and callers "
            "when needed. A test importing a function is not its implementation: follow the "
            "import to the module defining that function. Fix the implementation, not the "
            "test expectations, unless the user specifically asks to change tests. Preserve "
            "the documented behavior and existing passing cases. Use the exact argument names from the offered tool schemas "
            "(for example path versus filePath). Edit existing files with focused exact "
            "replacements or apply_patch; use write to create new files. After an edit "
            "mismatch, re-read the affected lines and retry a smaller replacement. Do not "
            "repeat the same failed call. Preserve unrelated changes. Run the project's "
            "relevant checks and inspect the diff. Continue until the requested work is "
            "complete or report a concrete blocker. Do not claim checks passed unless "
            "they ran. Once the requested change and verification are complete, give a "
            "brief final answer and stop. Do not create unsolicited documentation or extra files."

        ),
        "permission": {"*": "deny", "read": "allow", "glob": "allow", "grep": "allow",
                       "list": "allow", "edit": "allow", "bash": "ask", "shell": "ask",
                       "question": "allow", "skill": "allow", "lsp": "allow",
                       "todowrite": "allow", "doom_loop": "ask"},
    }


def lan_ip() -> str:
    """This machine's LAN address, as another PC on the network would reach it. Opening a
    UDP socket 'to' a public IP just makes the OS pick the right outbound interface — no
    traffic is sent."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"


def tailscale_ip() -> str | None:
    """This machine's Tailscale IP (100.x.x.x), if the `tailscale` CLI is installed and the
    node is logged in — None otherwise (not installed, logged out, daemon down), so the Help
    panel can just omit the Tailscale section rather than show a broken one. Deliberately a
    plain CLI call, not a new Caddy site parse: added 2026-07-26 as a second reachability path
    alongside the LAN IP (same ports, same tokens, same CA — only the address differs), after a
    real "Connect Timeout Error" report from a LAN client turned out to be a network-path issue
    between the two machines rather than anything wrong on this one; the client (a laptop) was
    already on the same tailnet, so exposing the existing Caddy ports there too gave it a second
    path that doesn't depend on the LAN/Wi-Fi being cooperative. See parse_caddyfile()'s ports —
    Tailscale reuses them unchanged, it's the same Caddy site blocks with one more address each."""
    try:
        out = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=3
        )
        ip = out.stdout.strip().splitlines()[0].strip() if out.returncode == 0 else ""
        return ip or None
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def tailscale_dns_name() -> str | None:
    try:
        result = subprocess.run(["tailscale", "status", "--json"], capture_output=True,
                                text=True, timeout=3)
        if result.returncode:
            return None
        name = json.loads(result.stdout).get("Self", {}).get("DNSName", "").rstrip(".")
        return name if re.fullmatch(r"[a-zA-Z0-9.-]+\.ts\.net", name) else None
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return None


def tailscale_app_url(dns_name: str | None, app_port: int) -> str | None:
    """Find Hearth's configured HTTPS Serve route without guessing the port."""
    if not dns_name:
        return None
    try:
        result = subprocess.run(["tailscale", "serve", "status", "--json"], capture_output=True,
                                text=True, timeout=3)
        if result.returncode:
            return None
        config = json.loads(result.stdout)
        for authority, site in config.get("Web", {}).items():
            address = urlsplit("https://" + authority)
            target = urlsplit(site.get("Handlers", {}).get("/", {}).get("Proxy", ""))
            if (address.hostname == dns_name and target.port == app_port
                    and target.hostname in {"localhost", "127.0.0.1", "::1"}
                    and config.get("TCP", {}).get(str(address.port or 443), {}).get("HTTPS")):
                port = address.port or 443
                return f"https://{dns_name}" + (f":{port}" if port != 443 else "")
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        pass
    return None


def parse_caddyfile(path: str = CADDYFILE) -> dict:
    """Pull the per-client bearer tokens and the external→backend port mapping out of the
    Caddyfile. Three distinct tokens (opencode/vscode/lan), not one shared secret (see the
    Caddyfile's `client_auth` snippet) - keyed by the Caddy matcher label attached to each
    ("@opencode header Authorization ...") so a client is identified by the credential it
    actually presents, not a spoofable/absent User-Agent. caddy_metrics.py parses this same
    file the same way to attribute LAN metrics rows by token."""
    result = {"tokens": {}, "ports": {}}
    try:
        text = open(path).read()
    except OSError:
        return result
    for label, token in re.findall(r'@(\w+)\s+header\s+Authorization\s+"Bearer\s+([A-Za-z0-9]+)"', text):
        result["tokens"][label] = token
    current_ext = None
    for raw in text.splitlines():
        line = raw.strip()
        header = re.search(r":(\d+)", line) if "{" in line and "reverse_proxy" not in line else None
        if header:
            current_ext = header.group(1)
        rp = re.search(r"reverse_proxy.*?:(\d+)", line)
        if rp and current_ext:
            backend = _UPSTREAM_BACKEND.get(rp.group(1))
            if backend:
                result["ports"][backend] = int(current_ext)
    return result


def ca_certificate() -> str | None:
    """Caddy's local CA root certificate (the one that must be trusted on the client),
    fetched from Caddy's admin API — it isn't sent in the served TLS chain."""
    try:
        r = requests.get(f"{CADDY_ADMIN}/pki/ca/local", timeout=4)
        r.raise_for_status()
        return (r.json().get("root_certificate") or "").strip() or None
    except (requests.RequestException, ValueError):
        return None


def ca_trust_commands(ip: str, app_port: int, *, base_url: str | None = None) -> dict[str, str]:
    """One-line copy/paste commands that fetch + trust the CA cert in a single step, per OS.

    A public CA (Let's Encrypt/ZeroSSL) can't issue certs for a private IP or LAN-only
    hostname like this machine's — that's a hard protocol constraint (domain validation needs
    public DNS/HTTP reachability), not a config choice. Caddy's `tls internal` already *is*
    its automatic-HTTPS behavior for exactly this case: certs are auto-issued and auto-renewed
    from Caddy's own local CA with zero server-side cert management. The one irreducible manual
    step is trusting that CA on each client once - these commands collapse what used to be
    "download this file, then follow several OS-specific GUI steps" into one line, they don't
    (and can't) remove the trust step itself.

    Fetches from THIS app's own /ca-cert route (deliberately unauthenticated - see its
    docstring) rather than Caddy directly, so one URL works before a client has any other
    reason to reach this machine yet."""
    url = f"{base_url}/ca-cert" if base_url else f"https://{ip}:{app_port}/ca-cert"
    curl = "curl -fsS" if base_url else "curl -sk"
    windows_tls = "" if base_url else " -SkipCertificateCheck"
    # Saved to the home dir, not /tmp - it needs to still be there later when a tool's config
    # (e.g. Continue's caBundlePath) points at it, and /tmp can get cleared on reboot.
    return {
        "linux": (
            f"{curl} {url} -o ~/{CERT_FILENAME} && "
            f"sudo cp ~/{CERT_FILENAME} /usr/local/share/ca-certificates/{CERT_FILENAME} && "
            "sudo update-ca-certificates"
        ),
        "macos": (
            f"{curl} {url} -o ~/{CERT_FILENAME} && "
            "sudo security add-trusted-cert -d -r trustRoot "
            f"-k /Library/Keychains/System.keychain ~/{CERT_FILENAME}"
        ),
        "windows": (
            f"Invoke-WebRequest -Uri {url} -OutFile $env:USERPROFILE\\{CERT_FILENAME}{windows_tls}; "
            f"Import-Certificate -FilePath $env:USERPROFILE\\{CERT_FILENAME} -CertStoreLocation Cert:\\LocalMachine\\Root"
        ),
    }


def _all_models() -> list[dict]:
    """Live model inventory across backends, each tagged with its backend — fail-soft, so a
    backend being down just omits it (same contract as /api/models)."""
    out: list[dict] = []
    # Broad excepts on purpose: this feeds a page render, so a backend being down (or a
    # client raising something unexpected) must degrade to "that backend's models are
    # absent", never a 500 on the whole page.
    for client, name in ((lms, "lmstudio"), (lemonade, "lemonade"), (vllm, "vllm"), (kobold, "kobold"), (fastflowlm, "fastflowlm")):
        try:
            for m in client.list_models():
                m["backend"] = name
                out.append(m)
        except Exception:
            pass
    return out


def _pick_default(chat_by_be: dict, loaded_by_be: dict, override: str | None = None) -> str | None:
    """Default model for a LAN tool. An explicit `override` (Settings panel's "Default LAN
    model", stored as settings_store's "lan_default_model") always wins, as long as it's
    still actually available - a stale override pointing at a deleted/renamed model falls
    straight through to the auto-pick below rather than sending a LAN tool a default that no
    longer exists. Auto-pick otherwise prefers a model that's ALREADY LOADED so the tool's
    first request is warm — Lemonade/vLLM serve one chat model at a time, and requesting an
    unloaded one triggers an evict+load that can take ~a minute on the NPU and blow past the
    tool's request timeout (the exact "OpenCode times out on LAN" symptom). Falls back to a
    capable curated model, then to the first available, when nothing is loaded yet."""
    if override:
        be, _, model_id = override.partition("/")
        if model_id in chat_by_be.get(be, []):
            return override
    for be in ("lemonade", "vllm", "kobold", "fastflowlm", "lmstudio"):
        if loaded_by_be.get(be):
            return f"{be}/{loaded_by_be[be][0]}"
    if "qwen3.6-moe-35b-a3b" in chat_by_be.get("lemonade", []):
        return "lemonade/qwen3.6-moe-35b-a3b"
    for be in ("lemonade", "vllm", "kobold", "fastflowlm", "lmstudio"):
        if chat_by_be.get(be):
            return f"{be}/{chat_by_be[be][0]}"
    return None


def build_continue_yaml(ip, token, ports, chat_by_be, embed_models, network_label="LAN") -> str:
    # network_label is purely cosmetic (model display names + the config's own title) — lets
    # the same builder produce a LAN-flavored or Tailscale-flavored config from the same live
    # model list without duplicating this whole function; the actual connectivity is entirely
    # determined by `ip`.
    lines = [
        f"name: {network_label} Assistant (via Caddy)",
        "version: 1.0.0",
        "schema: v1",
        "",
        f"# Auto-generated from the live model list, Caddy config, and this machine's {network_label} IP.",
        "# Re-copy after downloading/deleting a model or if the IP changes.",
        "# Set caBundlePath to wherever you saved the CA cert (Step 1).",
        "models:",
    ]

    def entry(name, model, port, ctx, roles, timeout):
        b = [f"  - name: {name}", "    provider: openai", f"    model: {model}",
             f"    apiBase: https://{ip}:{port}/v1", f"    apiKey: {token}"]
        if ctx:
            b += ["    defaultCompletionOptions:", f"      contextLength: {ctx}"]
        b += [f"    roles: [{', '.join(roles)}]", "    requestOptions:",
              f"      caBundlePath: /home/YOUR_USERNAME/{CERT_FILENAME}", f"      timeout: {timeout}",
              # Caddy redacts Authorization from its own access log (a deliberate secret-
              # scrubbing default), so the token itself can't be used to attribute metrics rows
              # to "vscode_continue" after the fact - this header isn't a secret, just a label, and
              # Caddy logs client-sent headers it doesn't recognize as sensitive verbatim (see
              # caddy_metrics.py's _source_from_headers).
              "      headers:", "        X-Hearth-Client: vscode_continue"]
        return b

    any_chat = False
    for be in ("lemonade", "vllm", "kobold", "fastflowlm", "lmstudio"):
        for mid in chat_by_be.get(be, []):
            any_chat = True
            lines += entry(f"{mid} ({_LABELS[be]}, {network_label})", mid, ports[be],
                           _advertised_context(be, mid, {}, None),
                           ["chat", "edit", "apply"], 120000)
    for em in embed_models:
        be = em["backend"]
        lines += entry(f"{em['id']} ({_LABELS.get(be, be)}, {network_label}) — embeddings for @codebase",
                       em["id"], ports.get(be, next(iter(ports.values()), 8443)), None,
                       ["embed"], 30000)
    if not any_chat:
        lines.append("  # (no chat models downloaded yet — get one from the Downloads panel)")
    lines += ["", "context:", "  - provider: code", "  - provider: docs", "  - provider: diff",
              "  - provider: terminal", "  - provider: problems", "  - provider: folder",
              "  - provider: codebase"]
    return "\n".join(lines)


def build_opencode_config(ip, token, ports, chat_by_be, loaded_by_be, default_override=None, network_label="LAN", ctx_by_model=None) -> str:
    # network_label: see build_continue_yaml's docstring comment — same reasoning, cosmetic only.
    # ctx_by_model: {(backend, model_id): real_loaded_context} from build()'s live inventory.
    # Used (unless the admin turned auto-detect off) so each entry advertises what its server
    # is actually running, instead of a fixed per-backend guess that can over-advertise.
    ctx_by_model = dict(ctx_by_model or {})
    context_settings = opencode_context_settings()
    if not context_settings["advertise_loaded"]:
        ctx_by_model = {}
    provider = {}
    for be in ("lmstudio", "lemonade", "vllm", "kobold", "fastflowlm"):
        models = {}
        for mid in chat_by_be.get(be, []):
            ctx = _advertised_context(be, mid, ctx_by_model, context_settings["fallback"])
            models[mid] = {
                "name": f"{mid} ({_LABELS[be]}, {network_label})",
                "limit": {"context": ctx, "output": _advertised_output(be, ctx, mid)},
            }
        options = {
            "baseURL": f"https://{ip}:{ports[be]}/v1", "apiKey": token,
            "headers": {"X-Hearth-Client": "opencode"},
        }
        if be == "lemonade":
            # Admin-controlled via Settings ("lemonade_enable_thinking", default on) rather
            # than hardcoded - disabling it didn't meaningfully speed up the Qwen3.8-27B
            # NVFP4-MTP build (the real bottleneck is prefill time on a large/growing
            # context, confirmed live via Lemonade's own timing logs), but it does trade
            # away visibility into the model's reasoning, so this is a real tradeoff worth
            # leaving switchable rather than deciding for every admin. OpenCode's bundled
            # @ai-sdk/openai-compatible provider generically converts any
            # `delta.reasoning_content` in the stream into a first-class "reasoning" stream
            # part (confirmed by inspecting its compiled bundle - not gated behind a
            # known-model allowlist), so turning this on lets OpenCode actually display the
            # model's thinking instead of showing nothing during generation.
            #
            # options.extraBody is still how this app reaches OpenCode's direct-to-Lemonade
            # traffic at all (confirmed via the same bundle inspection: it merges
            # `this.config.extraBody` into every request) - app.py's own chat code never
            # sees these requests, they go straight through Caddy. Harmless for any other
            # Lemonade model: a chat template that doesn't read this kwarg just ignores it.
            thinking = (settings_store.get("lemonade_enable_thinking") or "true") == "true"
            options["extraBody"] = {"chat_template_kwargs": {"enable_thinking": thinking}}
        provider[be] = {
            "npm": "@ai-sdk/openai-compatible",
            "name": f"{_LABELS[be]} ({network_label} via Caddy)",
            # headers.X-Hearth-Client isn't a secret, just a label - Caddy redacts Authorization
            # from its own access log (deliberate secret-scrubbing default), so the actual token
            # can't be used to attribute metrics rows to "opencode" after the fact. Caddy logs
            # client-sent headers it doesn't recognize as sensitive verbatim - see
            # caddy_metrics.py's _source_from_headers.
            "options": options,
            "models": models,
        }
    # Emit an explicit compaction block so a small-context model doesn't fail the way
    # OpenCode's bare defaults do (see compaction_settings' docstring) - an admin tunes
    # keep.tokens/buffer from Settings, and an unset buffer is omitted so OpenCode falls back
    # to its own 10%-of-limit default rather than being pinned to an explicit value here.
    compaction = compaction_settings()
    compaction_block: dict = {"auto": True, "keep": {"tokens": compaction["keep_tokens"]}}
    if compaction["buffer"] is not None:
        compaction_block["buffer"] = compaction["buffer"]
    cfg = {
        "$schema": "https://opencode.ai/config.json",
        "model": _pick_default(chat_by_be, loaded_by_be, default_override) or "",
        "provider": provider,
        "compaction": compaction_block,
    }
    flm_models = list(provider.get("fastflowlm", {}).get("models", {}))
    if flm_models:
        model = cfg["model"] if cfg["model"].startswith("fastflowlm/") else "fastflowlm/" + flm_models[0]
        cfg["agent"] = {"flm-small": flm_small_agent(model)}
        if cfg["model"].startswith("fastflowlm/"):
            cfg["default_agent"] = "flm-small"
        # Compaction is global in OpenCode: a large running model's buffer must
        # not make a smaller configured model overflow before its first turn.
        # Fixed-window Flash models are single-turn, not coding-session targets.
        windows = [item['limit']['context'] for p in provider.values()
                   for mid, item in p['models'].items() if '-flash' not in mid]
        limit = min(windows) if windows else flm_context.DEFAULT_CONTEXT
        compaction_block["keep"]["tokens"] = min(compaction_block["keep"]["tokens"], limit // 8)
        compaction_block["buffer"] = min(max(compaction_block.get("buffer", 0), limit // 8), limit // 4)
    return json.dumps(cfg, indent=2)


def build(app_port: int = 5443) -> dict:
    """Assemble all runtime help data for the template / the /api/help endpoint."""
    ip = lan_ip()
    caddy = parse_caddyfile()
    # Each client tool gets its own dedicated token embedded in its generated config, rather
    # than all three sharing one secret - see parse_caddyfile()'s docstring. "lan" is the
    # general-purpose one shown for manual curl testing / the Settings panel's connection test.
    fallback = "SET_A_BEARER_TOKEN_IN_CADDYFILE"
    opencode_token = caddy["tokens"].get("opencode") or fallback
    vscode_token = caddy["tokens"].get("vscode") or fallback
    lan_token = caddy["tokens"].get("lan") or fallback
    ports = {**_DEFAULT_PORTS, **caddy["ports"]}
    cert = ca_certificate()

    models = _all_models()
    chat_by_be = {
        be: [m["id"] for m in models
             if m.get("backend") == be and m.get("type") not in _NON_CHAT
             and m.get("loadable") is not False]
        for be in ("lmstudio", "lemonade", "vllm", "kobold", "fastflowlm")
    }
    # Which chat models are already loaded, per backend — used to default a LAN tool to a
    # warm model so its first request doesn't pay the cold-load (evict+load) cost.
    loaded_by_be = {
        be: [m["id"] for m in models
             if m.get("backend") == be and m.get("type") not in _NON_CHAT
             and m.get("loadable") is not False and m.get("state") == "loaded"]
        for be in ("lmstudio", "lemonade", "vllm", "kobold", "fastflowlm")
    }
    # Real loaded context per (backend, model), for the generated OpenCode config — only
    # meaningful for models a backend is actually serving right now (see
    # _advertised_context); unloaded models report none and fall back to a backend default.
    ctx_by_model = {
        (m.get("backend"), m["id"]): m["loaded_context_length"]
        for m in models
        if isinstance(m.get("loaded_context_length"), int) and m["loaded_context_length"] > 0
    }
    embed_models = [
        {"id": m["id"], "backend": m.get("backend")}
        for m in models if m.get("type") == "embeddings"
    ]
    # Settings panel's "Default LAN model" (admin-set, "backend/model_id" shaped, empty =
    # auto-pick). Real incident (2026-07-26): the NPU (Lemonade/qwen3.6-moe-35b-a3b) auto-pick
    # was too slow for a large OpenCode coding turn and its client gave up waiting - this lets
    # an admin point OpenCode at a faster backend without needing a code change every time the
    # right default changes, while still auto-picking (NPU-first) when nothing is set.
    default_override = settings_store.get("lan_default_model")

    # Tailscale: "enabled" gates whether the Help panel shows the Tailscale section at all —
    # an admin who doesn't use Tailscale can turn it off outright rather than just ignoring
    # it. The manual override exists because tailscale_ip() shells out to the `tailscale` CLI
    # as this app's own process, which can fail in a container/restricted-service setup even
    # though Tailscale itself is reachable; an admin who already knows the IP can just type it
    # in instead of debugging CLI access.
    #
    # Deliberately NOT a "prefer Tailscale over LAN" switch (an earlier version of this had
    # one, swapping Step 2's config to the Tailscale IP) — real feedback was that some users
    # want the LAN config even with Tailscale enabled (e.g. a laptop that's usually on the
    # same LAN and only needs Tailscale occasionally, off-network). So Step 2 below always
    # stays LAN-only regardless of any Tailscale setting, and Tailscale gets its own always-
    # present, independently-generated set of configs instead of replacing anything — both
    # are shown side by side, pick whichever fits the device being configured right now.
    ts_enabled = (settings_store.get("tailscale_enabled") or "true") == "true"
    ts_ip = (settings_store.get("tailscale_ip_override") or tailscale_ip()) if ts_enabled else None
    ts_dns = tailscale_dns_name() if ts_enabled else None
    ts_app = tailscale_app_url(ts_dns, app_port)
    ts_links = {name: ts_app + path for name, path in {
        "chat": "/", "models": "/models", "downloads": "/downloads",
        "settings": "/settings", "ca_cert": "/ca-cert",
    }.items()} if ts_app else {}

    # None when no Tailscale IP is available, same fail-soft contract as everything else here.
    ts_continue_yaml = None
    ts_opencode_config = None
    ts_ca_trust_commands = None
    if ts_ip:
        ts_continue_yaml = build_continue_yaml(
            ts_ip, vscode_token, ports, chat_by_be, embed_models, network_label="Tailscale"
        )
        ts_opencode_config = build_opencode_config(
            ts_ip, opencode_token, ports, chat_by_be, loaded_by_be, default_override,
            network_label="Tailscale", ctx_by_model=ctx_by_model,
        )
        # A device reachable only over Tailscale can't necessarily reach the LAN IP either —
        # that's the whole point of needing Tailscale — so Step 1's CA-trust command (which
        # fetches from this app's own LAN-addressed /ca-cert route) may not even be reachable
        # for it. This app listens on 0.0.0.0, so the exact same route answers on the
        # Tailscale IP too (confirmed live) — just needs its own copy of the command.
        ts_ca_trust_commands = ca_trust_commands(ts_ip, app_port, base_url=ts_app)

    flm_loaded = loaded_by_be.get("fastflowlm", [])
    flm_status = (
        "Loaded FLM model: " + ", ".join(
            f"{mid} ({flm_context.context_limit(mid):,} context tokens)" for mid in flm_loaded
        ) + ". " if flm_loaded else "No FLM model is currently loaded. "
    )
    support_warmup = settings_store.get("support_models_keep_loaded", "true") != "false"
    flm_status += (
        "Embedding and voice model warm-up is enabled."
        if support_warmup else
        "Embedding and voice model warm-up is disabled; those models load when their features are used."
    )

    return {
        "flm_status": flm_status,
        "lan_ip": ip,
        "tailscale_ip": ts_ip,
        "tailscale_dns_name": ts_dns,
        "tailscale_app_url": ts_app,
        "tailscale_links": ts_links,
        "tailscale_continue_yaml": ts_continue_yaml,
        "tailscale_opencode_config": ts_opencode_config,
        "tailscale_ca_trust_commands": ts_ca_trust_commands,
        "lan_token": lan_token,
        "ports": ports,
        "cert_filename": CERT_FILENAME,
        "ca_cert": cert or "(could not read the CA certificate — is Caddy running? "
                           "check the admin API at localhost:2019)",
        "ca_cert_ok": bool(cert),
        "ca_trust_commands": ca_trust_commands(ip, app_port),
        "continue_yaml": build_continue_yaml(ip, vscode_token, ports, chat_by_be, embed_models),
        "opencode_config": build_opencode_config(
            ip, opencode_token, ports, chat_by_be, loaded_by_be, default_override,
            ctx_by_model=ctx_by_model,
        ),
    }
