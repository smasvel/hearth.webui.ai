"""KoboldCPP backend — an OpenAI-compatible GGUF server this app manages like vLLM.

KoboldCPP (a llama.cpp server) serves ONE GGUF model at a time and has no runtime
load/unload API of its own, so — exactly like vllm_client — this app builds those on top:
a user-level systemd unit (~/.config/systemd/user/kobold.service) whose model path lives in
an EnvironmentFile (~/.config/koboldcpp/service.env) this module rewrites before restarting.

  "load"   = write the new GGUF path to service.env + `systemctl --user restart kobold` +
             poll /v1/models until it reports the model loaded
  "unload" = `systemctl --user stop kobold`
  "download" = fetch a GGUF from Hugging Face into the local cache and register it

The client talks DIRECTLY to KoboldCPP on 127.0.0.1:5001 (Caddy's :8446 proxy is only for
LAN tools like Continue/OpenCode). Model ids are shaped "koboldcpp/<gguf-basename>", the same
id KoboldCPP itself reports on /v1/models — so owns_model() can route by that prefix.

Never raises from list_models(): a stopped/unreachable kobold.service just means nothing shows
as loaded, the same fail-soft contract as the other optional backends.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

import psutil
import requests
import model_test_profiles
import model_metadata_store

import settings_store

SERVICE_NAME = "kobold.service"
_CONFIG_DIR = Path.home() / ".config" / "koboldcpp"
_SERVICE_ENV_FILE = _CONFIG_DIR / "service.env"
_LEGACY_REGISTRY_FILE = _CONFIG_DIR / "models.json"
_registry_migrated = False
_registry_migration_lock = threading.Lock()

_DEFAULT_BASE_URL = "http://127.0.0.1:5001"
_DEFAULT_CONTEXT = 8192
_ID_PREFIX = "koboldcpp/"


class KoboldError(Exception):
    pass


def base_url() -> str:
    """Settings-panel override wins over the default, read fresh each call (no import-time
    cache) so a UI change applies immediately — same pattern as the other clients."""
    return (settings_store.get("kobold_base_url") or _DEFAULT_BASE_URL).rstrip("/")


def api_key() -> str | None:
    # KoboldCPP is unauthenticated on localhost; Caddy enforces the bearer token for LAN.
    return settings_store.get("kobold_api_key") or None


def _auth_headers() -> dict:
    key = api_key()
    return {"Authorization": f"Bearer {key}"} if key else {}


# ---- model id <-> gguf path registry ----------------------------------------------
def _load_registry() -> dict:
    global _registry_migrated
    if not _registry_migrated:
        with _registry_migration_lock:
            if not _registry_migrated:
                model_metadata_store.migrate_registry_file("kobold", _LEGACY_REGISTRY_FILE)
                _registry_migrated = True
    return model_metadata_store.registry("kobold")


def initialize_registry() -> None:
    _load_registry()


def _save_registry(reg: dict) -> None:
    model_metadata_store.save_registry("kobold", reg)


def _id_for_path(gguf_path: str) -> str:
    base = os.path.basename(gguf_path)
    if base.lower().endswith(".gguf"):
        base = base[:-5]
    return f"{_ID_PREFIX}{base}"


def _register(gguf_path: str) -> str:
    """Add a GGUF file to the registry, returning its model id."""
    reg = _load_registry()
    model_id = _id_for_path(gguf_path)
    reg[model_id] = {"path": gguf_path, "size_gb": _file_size_gb(gguf_path)}
    _save_registry(reg)
    return model_id


def _file_size_gb(path: str) -> float | None:
    try:
        return round(os.path.getsize(path) / 1e9, 2)
    except OSError:
        return None


# ---- service control ---------------------------------------------------------------
def _systemctl(*args: str, timeout: int = 120) -> str:
    try:
        result = subprocess.run(
            ["systemctl", "--user", *args, SERVICE_NAME],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise KoboldError(f"'systemctl --user {' '.join(args)} {SERVICE_NAME}' timed out") from exc
    if result.returncode != 0 and "is-active" not in args:
        raise KoboldError((result.stderr or result.stdout or "systemctl command failed").strip())
    return result.stdout


def _resolve_gguf_path(model_id_or_path: str) -> str | None:
    """A registered model id -> its GGUF path, or the value itself if it's already a path
    that exists on disk (same fallback load_model() already uses for the main model)."""
    meta = _load_registry().get(model_id_or_path)
    if meta:
        return meta.get("path")
    return model_id_or_path if os.path.exists(model_id_or_path) else None


def _write_service_env(gguf_path: str, model_name: str | None = None, context: int | None = None) -> None:
    """Speculative decoding + GPU-offload tuning (2026-07-27): three optional global settings
    (kobold_gpu_layers, kobold_draft_model, kobold_draft_amount) read fresh here, same pattern
    as base_url()/api_key() above, so a Settings-page change takes effect on the next Load
    without a code change. Global rather than per-model-id, since only one KoboldCPP model is
    ever served at a time anyway (see this module's docstring) - whatever draft model is
    configured must share the served model's tokenizer/family (e.g. Qwen2.5-Coder-7B paired
    with Qwen2.5-Coder-0.5B) for speculative decoding to actually work; picking a mismatched
    pair just wastes GPU time on a draft KoboldCPP will keep rejecting, or fails to start
    outright, not a subtly wrong answer.

    Kept as an EnvironmentFile (not baked directly into the systemd unit's ExecStart args)
    so the unit file itself never needs editing - the unit's `${VAR:+...}` bash expansions
    (see kobold.service) just omit each flag entirely when its variable is unset/empty."""
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    ctx = context or _DEFAULT_CONTEXT
    name = model_name or _id_for_path(gguf_path)[len(_ID_PREFIX):]
    lines = [f"KOBOLD_MODEL={gguf_path}", f"KOBOLD_MODELNAME={name}", f"KOBOLD_CONTEXT={ctx}"]

    gpu_layers = (settings_store.get("kobold_gpu_layers") or "").strip()
    if gpu_layers:
        lines.append(f"KOBOLD_GPU_LAYERS={gpu_layers}")

    draft_setting = (settings_store.get("kobold_draft_model") or "").strip()
    if draft_setting:
        draft_path = _resolve_gguf_path(draft_setting)
        if draft_path:
            lines.append(f"KOBOLD_DRAFT_MODEL={draft_path}")
            draft_amount = (settings_store.get("kobold_draft_amount") or "").strip() or "16"
            lines.append(f"KOBOLD_DRAFT_AMOUNT={draft_amount}")
        # A configured-but-unresolvable draft model (deleted file, typo'd id) is silently
        # skipped rather than failing the whole Load - speculative decoding is a speed
        # optimization, never something that should block serving the main model at all.

    _SERVICE_ENV_FILE.write_text("\n".join(lines) + "\n")


# ---- live queries ------------------------------------------------------------------
def _served() -> dict | None:
    """The model KoboldCPP is actually serving right now (from /v1/models), or None if the
    service is down/unreachable."""
    try:
        resp = requests.get(f"{base_url()}/v1/models", headers=_auth_headers(), timeout=4)
        resp.raise_for_status()
        data = resp.json().get("data") or []
        return data[0] if data else None
    except (requests.RequestException, ValueError):
        return None


def current_served_model() -> str | None:
    entry = _served()
    return entry.get("id") if entry else None


def list_models() -> list[dict]:
    """Every GGUF registered for KoboldCPP, tagged 'loaded' for the one actually being
    served now — same shape as lemonade/vllm list_models() so the Models panel renders it
    identically. Fail-soft: a stopped service just yields nothing loaded."""
    reg = _load_registry()
    served_id = current_served_model()
    # Make sure whatever's currently served is represented even if not yet registered
    # (e.g. the model the service booted with before this app ever wrote the registry) —
    # and PERSIST it, so a later load/delete can resolve its GGUF path.
    if served_id and served_id not in reg:
        env_path = _read_env_path()
        reg[served_id] = {"path": env_path, "size_gb": _file_size_gb(env_path) if env_path else None}
        if env_path:
            _save_registry(reg)
    models = []
    for model_id, meta in reg.items():
        models.append({
            "id": model_id,
            "publisher": "koboldcpp",
            "type": "llm",
            "state": "loaded" if model_id == served_id else "not-loaded",
            "size_gb": meta.get("size_gb"),
            "configured_context_length": (
                _read_env_context()
                if meta.get("path") and _read_env_path()
                and os.path.realpath(meta["path"]) == os.path.realpath(_read_env_path())
                else None
            ),
            # Real --contextsize of the running unit, only for the served model (koboldcpp
            # serves one at a time) — see _read_env_context.
            "loaded_context_length": _read_env_context() if model_id == served_id else None,
        })
    return models


def _read_env_path() -> str | None:
    try:
        for line in _SERVICE_ENV_FILE.read_text().splitlines():
            if line.startswith("KOBOLD_MODEL="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def _read_env_context() -> int | None:
    """KOBOLD_CONTEXT from the service env — the --contextsize the running unit was started
    with (see kobold.service's ExecStart). Surfaced on the served model so help_data's
    generated OpenCode config advertises the real context instead of a fixed guess. None if
    unset/unparsable, so the caller falls back to a backend default."""
    try:
        for line in _SERVICE_ENV_FILE.read_text().splitlines():
            if line.startswith("KOBOLD_CONTEXT="):
                value = line.split("=", 1)[1].strip()
                return int(value) if value.isdigit() else None
    except OSError:
        pass
    return None


def owns_model(model: str) -> bool:
    """Route chat here for any KoboldCPP-shaped id."""
    if not model:
        return False
    return model.startswith(_ID_PREFIX) or model in _load_registry()


# ---- chat --------------------------------------------------------------------------
def chat_stream(messages, model, temperature=0.7, max_tokens=None, frequency_penalty=0.3, tools=None):
    """Yield raw SSE 'data: ...' lines from KoboldCPP's OpenAI-compatible chat endpoint.
    Same shape/defaults as the other clients' chat_stream() so app.py's worker needs no
    changes. KoboldCPP serves one model, so `model` is informational — the loaded GGUF
    answers regardless.

    `tools`: OpenAI-format tool definitions (see mcp_tools_client.py) - passed through
    verbatim. Relies on kobold.service's own --jinja --jinja_tools flags (see that unit's
    ExecStart) to actually honor them; without --jinja_tools specifically, KoboldCPP
    formats tool calls with a generic heuristic template instead of the model's own,
    which is what caused the malformed/empty-argument tool calls this app hit before
    that fix (see the OpenCode tool-calling investigation in this project's history)."""
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": True,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if frequency_penalty:
        payload["frequency_penalty"] = frequency_penalty
    if tools:
        payload["tools"] = tools
    try:
        with requests.post(
            f"{base_url()}/v1/chat/completions",
            json=model_test_profiles.apply(payload, "kobold"), headers=_auth_headers(), stream=True, timeout=(10, 900),
        ) as resp:
            if resp.status_code >= 400:
                detail = ""
                try:
                    body = resp.json()
                    err = body.get("error")
                    detail = (err.get("message") if isinstance(err, dict) else str(err)) or ""
                except (ValueError, AttributeError):
                    detail = (resp.text or "").strip()
                raise KoboldError(f"KoboldCPP error {resp.status_code}: {detail or 'request failed'}")
            for line in resp.iter_lines(chunk_size=1):
                if line:
                    yield line.decode("utf-8", errors="replace")
    except requests.RequestException as exc:
        raise KoboldError(f"Chat request failed: {exc}") from exc


# ---- load / unload -----------------------------------------------------------------
_SAFE_LOAD_RAM_FRACTION = 0.8


def check_safe_to_load(model_id: str) -> str | None:
    """Pre-flight sanity check, same reasoning and threshold as
    lemonade_client.check_safe_to_load() (see its docstring for the real incident that
    motivated it) - refuses when a model's file size vastly exceeds this machine's total
    RAM, instead of letting KoboldCPP crash on load. Simpler than Lemonade's version: every
    KoboldCPP model is a GGUF this app itself downloaded (or was told the local path of), so
    its real size is already sitting in the registry (`_file_size_gb`, straight from
    `os.path.getsize`) - no Hugging Face lookup, and no "size unknown" case to fail open on."""
    size_gb = (_load_registry().get(model_id) or {}).get("size_gb")
    if not size_gb:
        return None
    ram_gb = psutil.virtual_memory().total / 1e9
    if size_gb <= ram_gb * _SAFE_LOAD_RAM_FRACTION:
        return None
    return (
        f"Refusing to load '{model_id}': this model is {size_gb:.0f}GB, but this machine only "
        f"has {ram_gb:.1f}GB of RAM total. It cannot fit in memory - pick a smaller quantization "
        f"or a smaller model."
    )


def is_active() -> bool:
    return _systemctl("is-active", timeout=10).strip() == "active"


def load_model(model_id: str) -> None:
    """Swap the served GGUF: resolve the model id to its file path, rewrite service.env,
    restart the service, and wait until KoboldCPP reports it loaded."""
    import time
    reg = _load_registry()
    meta = reg.get(model_id)
    path = meta.get("path") if meta else (model_id if os.path.exists(model_id) else None)
    if not path or not os.path.exists(path):
        raise KoboldError(f"No GGUF on disk for '{model_id}'. Download it first.")
    _write_service_env(path, model_name=model_id[len(_ID_PREFIX):] if model_id.startswith(_ID_PREFIX) else None)
    if model_id not in reg:
        _register(path)
    _systemctl("restart")
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if current_served_model():  # OpenAI API up => model finished loading
            return
        time.sleep(2)
    raise KoboldError(f"KoboldCPP didn't become ready after loading '{model_id}' (timed out).")


def unload_model(model_id: str | None = None) -> None:
    """Stop the service (frees RAM). KoboldCPP has no partial-unload; stopping is unload."""
    _systemctl("stop")


# ---- download (Hugging Face GGUF) --------------------------------------------------
def download_model(query: str):
    """Download a GGUF from Hugging Face and register it, yielding structured progress dicts
    (same shape vLLM's download_model yields, so app.py's structured worker relays it).
    `query` is an HF repo id, optionally 'repo:quant' (e.g. 'bartowski/Qwen2.5-Coder-7B-Instruct-GGUF:Q4_K_M').
    Picks the smallest GGUF matching the quant hint, or the smallest overall."""
    from huggingface_hub import HfApi, hf_hub_download

    repo, _, quant = query.partition(":")
    repo = repo.strip()
    quant = quant.strip().lower()
    if not repo:
        raise KoboldError("A Hugging Face GGUF repo id is required.")

    api = HfApi()
    try:
        files = [f for f in api.list_repo_files(repo) if f.lower().endswith(".gguf")]
    except Exception as exc:
        raise KoboldError(f"Couldn't list '{repo}': {exc}") from exc
    if not files:
        raise KoboldError(f"No .gguf files found in '{repo}'.")
    candidates = [f for f in files if quant in f.lower()] if quant else files
    if not candidates:
        raise KoboldError(f"No GGUF matching quant '{quant}' in '{repo}'. Available: {', '.join(sorted(files))[:300]}")
    # Prefer a common balanced quant when no hint given; else the first match.
    if not quant:
        for pref in ("q4_k_m", "q4_k_s", "q5_k_m", "q4_0"):
            m = [f for f in candidates if pref in f.lower()]
            if m:
                candidates = m
                break
    target = sorted(candidates, key=len)[0]

    yield {"file": target, "file_index": 1, "total_files": 1, "percent": 0}
    try:
        local_path = hf_hub_download(repo_id=repo, filename=target)
    except Exception as exc:
        raise KoboldError(f"Download of '{repo}/{target}' failed: {exc}") from exc
    _register(local_path)
    yield {"file": target, "file_index": 1, "total_files": 1, "percent": 100, "path": local_path}


def delete_model(model_id: str) -> None:
    """Remove a GGUF from disk and the registry. If it's the currently-served model, stop
    the service first so we're not deleting a file out from under a running process."""
    reg = _load_registry()
    meta = reg.get(model_id)
    path = meta.get("path") if meta else None
    if current_served_model() == model_id:
        try:
            _systemctl("stop")
        except KoboldError:
            pass
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError as exc:
            raise KoboldError(f"Couldn't delete GGUF file: {exc}") from exc
    if model_id in reg:
        del reg[model_id]
        _save_registry(reg)


def check_servable(model_id: str) -> str | None:
    """No-op guard for interface parity with vllm.check_servable — KoboldCPP runs any GGUF."""
    return None
