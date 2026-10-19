"""Client for a vLLM server's OpenAI-compatible API, plus model management.

Unlike LM Studio and Lemonade, vLLM itself has no runtime load/unload HTTP
API — a `vllm serve <model>` process serves exactly one model until the
process is restarted with a different one. This module builds
load/unload/download/delete on top of that constraint instead of exposing it
as a limitation: the server runs as a **user-level** systemd unit
(~/.config/systemd/user/vllm.service, controlled via `systemctl --user`, no
sudo — this app already runs as this same user, so no privilege boundary is
crossed) whose model/context/dtype live in an env file
(~/.config/vllm/service.env) this module rewrites before restarting it.
"load" = write new config + `systemctl --user restart vllm` + poll until the
new model reports ready; "unload" = `systemctl --user stop vllm`.

vllm.service's ExecStart also always passes `--language-model-only` and
`--gpu-memory-utilization 0.6` (fixed, not per-model configurable — no model
registered here has ever needed to change either). Real incident behind both
(2026-07-23): a Qwen3.5-family model (hybrid linear-attention/"mamba" layers
plus a multimodal image encoder) hung forever during "Encoder cache... will be
initialized... profiled with 1 image items" — not a crash, an actual deadlock
in the EngineCore/Worker shared-memory handshake, reproduced identically
across --enforce-eager and multiple --gpu-memory-utilization values, so not a
memory problem despite superficially resembling one (a *separate*, real
`--gpu-memory-utilization` OOM ValueError was also hit along the way, which is
what made this confusing to diagnose). `--language-model-only` (disables all
multimodal inputs, i.e. skips exactly that profiling step) got the same model
serving successfully in minutes. Since this app never sends images to vLLM
(text chat only), that flag costs nothing here for any model, multimodal or
not, and closes off the whole failure class in advance rather than needing a
per-model workaround. `--gpu-memory-utilization` at the vLLM CPU default
(0.9) is what raised the separate OOM ValueError encountered along the way
(90% of RAM is rarely actually free once buff/cache and other processes are
counted) — 0.6 leaves real headroom without needing to special-case it per
machine.

Downloads go straight to Hugging Face Hub — vLLM has no catalog of its own —
via the `hf` CLI (ships with huggingface_hub, already a vLLM dependency) for
the download step, and huggingface_hub's cache-scanning API for delete/size.
A small local registry (~/.config/vllm/models.json) tracks which repos this
app has explicitly downloaded/loaded (with their max_model_len/dtype), rather
than trusting the raw shared HF cache wholesale — that cache is a general
machine-wide directory any tool can write to, confirmed live it already held
an unrelated broken repo from something else entirely (see README's vLLM
section), so blindly listing everything in it would misrepresent stray
cache content as vLLM-manageable models.

Runs locally on this machine as of 2026-07-23, CPU-only — this machine's iGPU
(gfx1150, Strix Point) is a mobile RDNA3.5 part and not a supported ROCm
target for vLLM (whose AMD support targets discrete cards: MI-series, RDNA3
desktop), so there's no GPU path here. The CPU PyPI wheel (`vllm==X.Y.Z+cpu`)
needed one manual fix to actually run: its bundled `_C*.abi3.so` native
extensions ship with an executable-stack ELF flag this kernel refuses to mmap
("cannot enable executable stack"), fixed permanently by
`patchelf --clear-execstack` on each `.so` in site-packages/vllm/ right after
install (see README's vLLM section for the full incident) — a fresh
`pip install --upgrade` would need the same patch reapplied.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import psutil
import requests
import model_test_profiles

import settings_store

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\].*?(?:\x07|\x1b\\)")

_DEFAULT_BASE_URL = os.environ.get("VLLM_BASE_URL", "http://localhost:8000").rstrip("/")
_DEFAULT_API_KEY = os.environ.get("VLLM_API_KEY")

VENV_DIR = Path(os.environ.get("VLLM_VENV_DIR", str(Path.home() / ".local/share/hearth/vllm/.venv")))
HF_CLI = VENV_DIR / "bin" / "hf"
SERVICE_NAME = "vllm.service"
_CONFIG_DIR = Path.home() / ".config" / "vllm"
_SERVICE_ENV_FILE = _CONFIG_DIR / "service.env"
_REGISTRY_FILE = _CONFIG_DIR / "models.json"

_DEFAULT_MAX_MODEL_LEN = 4096
_DEFAULT_DTYPE = "bfloat16"


def base_url() -> str:
    """Settings-panel override takes priority over .env, checked fresh on every call
    (not cached at import time) so a change in the UI applies immediately, no restart —
    same pattern as lmstudio_client.base_url()/lemonade_client.base_url()."""
    return (settings_store.get("vllm_base_url") or _DEFAULT_BASE_URL).rstrip("/")


def api_key() -> str | None:
    return settings_store.get("vllm_api_key") or _DEFAULT_API_KEY


def _auth_headers() -> dict:
    key = api_key()
    return {"Authorization": f"Bearer {key}"} if key else {}


class VLLMError(RuntimeError):
    pass


# --- local registry: which repos this app manages for vLLM ---------------

def _load_registry() -> dict:
    try:
        return json.loads(_REGISTRY_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_registry(registry: dict) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _REGISTRY_FILE.write_text(json.dumps(registry, indent=2))


def _register(model_id: str, max_model_len: int, dtype: str) -> None:
    registry = _load_registry()
    registry[model_id] = {"max_model_len": max_model_len, "dtype": dtype}
    _save_registry(registry)


def _deregister(model_id: str) -> None:
    registry = _load_registry()
    if registry.pop(model_id, None) is not None:
        _save_registry(registry)


# --- systemd control (user-level unit, no sudo) ---------------------------

def _systemctl(*args: str, timeout: int = 120) -> str:
    """Default timeout raised from an original 30s after a real failure: systemd's own
    stop timeout for this unit is the 90s default (TimeoutStopSec unset), and a restart
    mid-load of a large model can genuinely take that long to respond to SIGTERM (it's
    busy in a blocking memory-intensive operation) before systemd escalates to SIGKILL —
    confirmed live: a `systemctl --user restart` issued while a 71.9GB model was loading
    hit our old 30s timeout and raised, while systemd's own stop was still in progress
    and didn't actually finish (SIGKILL) until ~90s in. 120s gives real margin above that
    without hanging forever on a truly stuck unit."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", *args, SERVICE_NAME],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise VLLMError(f"'systemctl --user {' '.join(args)} {SERVICE_NAME}' timed out") from exc
    if result.returncode != 0 and "is-active" not in args:
        raise VLLMError((result.stderr or result.stdout or "systemctl command failed").strip())
    return result.stdout


def _write_service_config(model_id: str, max_model_len: int, dtype: str) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _SERVICE_ENV_FILE.write_text(
        f"VLLM_MODEL={model_id}\nVLLM_MAX_MODEL_LEN={max_model_len}\nVLLM_DTYPE={dtype}\n"
    )


def _current_served_model() -> str | None:
    """The model id the running vllm.service actually reports via its own live
    /v1/models — not just what the config file says (the service could still be
    starting up, crashed, or serving a stale config from before a pending restart).
    None on any failure, never raises — same fail-soft contract as list_models()."""
    try:
        resp = requests.get(f"{base_url()}/v1/models", headers=_auth_headers(), timeout=5)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return data[0]["id"] if data else None
    except (requests.RequestException, KeyError, IndexError, ValueError):
        return None


def list_models() -> list[dict]:
    """Every model this app has registered for vLLM (downloaded and/or previously
    loaded), tagged 'loaded' if it's the one vllm.service is actually currently
    serving right now (confirmed live, not assumed from config) and 'not-loaded'
    otherwise — same shape/semantics as lemonade_client.list_models() so the Models
    panel can render both backends identically. Never raises: an unreachable/stopped
    vllm.service just means nothing shows as 'loaded', same fail-soft contract as
    Lemonade not being installed."""
    registry = _load_registry()
    served_id = _current_served_model()
    sizes = _cached_repo_sizes_gb()  # {repo_id: on-disk GB} from the HF cache
    ids = list(registry)
    if served_id and served_id not in registry:
        ids.append(served_id)
    models = []
    for model_id in ids:
        # The registry records the exact --max-model-len each repo was loaded with, so for
        # the model vllm.service is actually serving right now we can report the real context
        # (help_data advertises it in the generated OpenCode config instead of the old fixed
        # 8192, which had drifted from service.env's VLLM_MAX_MODEL_LEN). Unknown for a
        # served-but-unregistered id → None, so the caller falls back to a backend default.
        meta = registry.get(model_id) or {}
        models.append({
            "id": model_id,
            "publisher": "vllm",
            "type": "llm",
            "state": "loaded" if model_id == served_id else "not-loaded",
            # vLLM's model id IS the HF repo id, so this is a direct lookup of the
            # real bytes on disk (None if the cache scan failed or the repo isn't
            # cached — the UI omits size then).
            "size_gb": sizes.get(model_id),
            "loaded_context_length": meta.get("max_model_len") if model_id == served_id else None,
        })
    return models


def _cached_repo_sizes_gb() -> dict[str, float]:
    """{repo_id: size-on-disk in GB} for everything in the local HF cache, via
    huggingface_hub.scan_cache_dir(). Best-effort — any failure yields {} so
    list_models still returns the models, just without sizes."""
    try:
        from huggingface_hub import scan_cache_dir

        return {r.repo_id: r.size_on_disk / 1e9 for r in scan_cache_dir().repos}
    except Exception:
        return {}


def owns_model(model_id: str) -> bool:
    """Membership check so app.py can route /api/chat and the load/unload/delete
    endpoints by model id alone, without hardcoding names — same pattern as
    lemonade_client.owns_model(). Fails soft (False), never raises."""
    return any(m["id"] == model_id for m in list_models())


_SAFE_LOAD_RAM_FRACTION = 0.65


def _hf_repo_size_bytes(repo_id: str, timeout: float = 10.0) -> int | None:
    """Total size of a Hugging Face repo's files via its file-tree API. Simpler than
    lemonade_client's equivalent (_hf_checkpoint_size_bytes): vLLM loads a whole HF repo
    directly, no per-file GGUF quantization picking, so no checkpoint-token fallback is
    needed here — just sum every file in the repo."""
    try:
        resp = requests.get(
            f"https://huggingface.co/api/models/{repo_id}/tree/main",
            params={"recursive": "true"},
            timeout=timeout,
        )
        resp.raise_for_status()
        entries = resp.json()
    except requests.RequestException:
        return None
    return sum(e.get("size", 0) or 0 for e in entries if e.get("type") == "file")


def check_safe_to_load(model_id: str) -> str | None:
    """Pre-flight size check before ever touching vllm.service — see
    lemonade_client.check_safe_to_load()'s docstring for the original incident (a 211GB
    GLM-5.2 quant on a 43.7GB-RAM machine) that motivated this pattern. A second, closely
    related real incident here (2026-07-23): a 71.9GB model was allowed to Load, and it
    didn't just fail once — systemd's stop timeout killed the stuck process mid-load,
    Restart=on-failure immediately tried again, and it kept retrying identically forever
    (the size math never changes), a genuine crash loop rather than a one-time error.
    vLLM CPU has no swap-aware degradation path the way some GPU setups do; it just gets
    SIGKILLed.

    _SAFE_LOAD_RAM_FRACTION was originally 0.8, on the theory that at crash-loop scale
    (71.9GB vs 43.7GB RAM) raw repo size alone was already the dominant, unambiguous
    factor and KV-cache/activation overhead on top of it didn't need separate accounting.
    A third real incident (2026-07-23, same day) disproved that at closer margins: a
    33.3GB model on a 46.9GB-RAM machine passed this check fine under 0.8 (37.5GB limit),
    but actual peak RSS+swap hit ~39.8GB once the KV cache came in, well past even that
    limit — the process didn't crash, it hung indefinitely instead (EngineCore/Worker
    shared-memory handshake never completing under swap thrashing) while pinning the
    whole machine at 95%+ RAM and nearly exhausting swap. Lowered to 0.65 so a repo has
    to leave real headroom for that overhead, not just for its own raw weights, before
    this waves it through. Returns a
    refusal string if the repo's total size exceeds _SAFE_LOAD_RAM_FRACTION of total
    system RAM; None if it looks safe or the size genuinely can't be determined (fails
    open, same as Lemonade's version — a network hiccup checking Hugging Face shouldn't
    block a load that might be fine)."""
    size_bytes = _hf_repo_size_bytes(model_id)
    if size_bytes is None:
        return None
    total_ram = psutil.virtual_memory().total
    limit = total_ram * _SAFE_LOAD_RAM_FRACTION
    if size_bytes > limit:
        return (
            f"'{model_id}' is {size_bytes / 1e9:.1f}GB, too large to safely load on "
            f"this machine ({total_ram / 1e9:.1f}GB RAM total, {limit / 1e9:.1f}GB safe "
            f"limit at {_SAFE_LOAD_RAM_FRACTION:.0%}). vLLM CPU has no way to serve a "
            "model that doesn't fit in memory with headroom for its KV cache — loading "
            "it would either crash-loop (systemd kills it mid-load, Restart=on-failure "
            "tries again, indefinitely) or hang indefinitely under swap thrashing rather "
            "than fail once and stop. Use a smaller model."
        )
    return None


def check_servable(model_id: str) -> str | None:
    """Pre-flight: refuse models vLLM structurally can't serve as an HF repo, before the
    load ever reaches vllm.service. The one that bit us (2026-07-24): a GGUF-only repo
    (`GnLOLot/MiniCPM5-1B-...-GGUF` — only .gguf files, no config.json). vLLM serves
    full Hugging Face repos (safetensors + config.json); given a repo id with no
    config.json it fails ModelConfig validation *instantly* on every start, and
    Restart=on-failure turns that into a fast crash loop that also evicts whatever
    model was working before. GGUF is llama.cpp's format — this app already has two
    backends built for it (LM Studio and Ryzen AI / Lemonade), so the right move is to
    send the user there rather than fight vLLM's limited/experimental GGUF path.
    Returns a refusal string if unservable, None if it looks fine (fails open on any
    detection error — a check glitch shouldn't block a load that might work)."""
    files = _repo_file_names(model_id)
    if files is None:
        return None  # couldn't determine — don't block
    lower = [f.lower() for f in files]
    has_config = any(f == "config.json" or f.endswith("/config.json") for f in lower)
    has_gguf = any(f.endswith(".gguf") for f in lower)
    if has_gguf and not has_config:
        return (
            f"'{model_id}' is a GGUF model (llama.cpp format). vLLM serves full Hugging "
            "Face repos (safetensors + config.json), not GGUF files, so it can't load "
            "this — it would fail instantly and crash-loop. Load GGUF models through the "
            "Ryzen AI (Lemonade) or LM Studio backend instead, which are built for them."
        )
    return None


def _repo_file_names(model_id: str) -> list[str] | None:
    """File names in a repo — from the local HF cache first (the model's already
    downloaded when Load is pressed), falling back to the HF file-tree API. None if
    neither source can answer."""
    try:
        from huggingface_hub import scan_cache_dir

        for repo in scan_cache_dir().repos:
            if repo.repo_id == model_id:
                names = set()
                for rev in repo.revisions:
                    names.update(f.file_name for f in rev.files)
                if names:
                    return list(names)
    except Exception:
        pass
    try:
        resp = requests.get(
            f"https://huggingface.co/api/models/{model_id}/tree/main",
            params={"recursive": "true"},
            timeout=10,
        )
        resp.raise_for_status()
        return [e.get("path", "") for e in resp.json() if e.get("type") == "file"]
    except requests.RequestException:
        return None


def load_model(model_id: str, max_model_len: int | None = None, dtype: str | None = None) -> None:
    """Swaps vLLM's served model — the only way to do this given vLLM has no runtime
    load API (see module docstring): rewrite vllm.service's env file, restart the
    unit, then poll /v1/models until it reports the NEW model specifically (not just
    'the service is up' — during the restart window it briefly reports nothing, and
    right after a crash-loop it could be reporting a stale process still shutting
    down). A generous timeout (10 min) since this covers a first-time download too —
    vLLM downloads a model's weights lazily on first `serve` if not already cached,
    same as `download_model()` below would, just blocking instead of streaming
    progress."""
    existing = _load_registry().get(model_id, {})
    resolved_max_len = max_model_len or existing.get("max_model_len") or _DEFAULT_MAX_MODEL_LEN
    resolved_dtype = dtype or existing.get("dtype") or _DEFAULT_DTYPE
    _register(model_id, resolved_max_len, resolved_dtype)
    _write_service_config(model_id, resolved_max_len, resolved_dtype)
    _systemctl("restart")

    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        if _current_served_model() == model_id:
            return
        active = _systemctl("is-active", timeout=10).strip()
        if active == "failed":
            raise VLLMError(
                f"vllm.service failed to start '{model_id}' — check `journalctl --user -u vllm -e`"
            )
        time.sleep(3)
    raise VLLMError(f"Timed out waiting for vLLM to start serving '{model_id}'")


def unload_model(model_id: str) -> None:
    """Stops vllm.service entirely — there's no concept of unloading just one model
    while leaving the process up, since the process only ever holds one. `model_id`
    is accepted (unused) only so app.py's dispatch can call this uniformly across
    backends without a special case."""
    _systemctl("stop")


def delete_model(model_id: str) -> None:
    """Removes a model from disk (Hugging Face's local cache) and from this app's
    registry. Unloads first if it's the one currently being served — a loaded-but-
    deleted model would be a confusing dangling state, same reasoning as
    lemonade_client.delete_model(). Best-effort on the unload: fine if it was already
    stopped."""
    if _current_served_model() == model_id:
        try:
            unload_model(model_id)
        except VLLMError:
            pass
    _deregister(model_id)
    try:
        from huggingface_hub import scan_cache_dir
        cache_info = scan_cache_dir()
        for repo in cache_info.repos:
            if repo.repo_id == model_id and repo.repo_type == "model":
                revisions = {rev.commit_hash for rev in repo.revisions}
                cache_info.delete_revisions(*revisions).execute()
                return
    except Exception as exc:
        raise VLLMError(f"Removed '{model_id}' from the list, but couldn't delete its cached files: {exc}") from exc


def download_model(repo_id: str):
    """Downloads a Hugging Face repo into the local cache ahead of time (so a later
    `load_model()` doesn't block on a first-time download), via the `hf` CLI (part of
    huggingface_hub, already installed as a vLLM dependency, no extra install needed)
    to actually move the bytes, but NOT for progress reporting — confirmed live
    (2026-07-23) that `hf download`'s stdout is completely silent for the entire
    download, in every configuration tried (plain pipe, pty with a real window size,
    HF_HUB_DISABLE_XET), printing nothing until a single final line at the very end.
    That's what a user report ("downloading status is not correct" / an earlier one,
    "not downloading... not started or hang") actually was: not a stuck download, but
    a real one with literally zero observable progress the whole time it ran.

    Fixed by not depending on the CLI's own output at all: huggingface_hub writes each
    file to a `<hash>.<random>.incomplete` file under its blobs/ dir and grows it in
    real time as bytes arrive (confirmed live: watched one climb from 0 to 807MB over
    ~25s) regardless of whether anything reaches stdout. Polling that directory's total
    size against the repo's known total size (via _hf_repo_size_bytes, the same call
    check_safe_to_load() uses) gives real byte-level progress unconditionally. Yields
    structured dicts ({file, percent, bytes_downloaded, bytes_total}), the same shape
    lemonade_client.pull_huggingface_repo()/pull_catalog_model() yield, so the Downloads
    page can drive one real progress bar for both backends instead of vLLM's old
    indeterminate scrolling log. Registers the repo (with default max_model_len/dtype,
    editable later via Load) on success so it shows up as 'not-loaded' immediately,
    matching Lemonade's downloaded-but-not-loaded state."""
    if not HF_CLI.is_file():
        raise VLLMError(f"'hf' CLI not found at {HF_CLI} — is the vLLM venv set up?")

    from huggingface_hub.constants import HF_HUB_CACHE

    total_bytes = _hf_repo_size_bytes(repo_id)
    repo_dir = Path(HF_HUB_CACHE) / f"models--{repo_id.replace('/', '--')}"

    def _bytes_on_disk() -> int:
        if not repo_dir.is_dir():
            return 0
        blobs_dir = repo_dir / "blobs"
        if not blobs_dir.is_dir():
            return 0
        return sum(f.stat().st_size for f in blobs_dir.iterdir() if f.is_file())

    proc = subprocess.Popen(
        [str(HF_CLI), "download", repo_id],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    output_lines: list[str] = []

    def _drain_output() -> None:
        for line in proc.stdout:
            clean = _ANSI_RE.sub("", line).strip()
            if clean:
                output_lines.append(clean)

    drain_thread = threading.Thread(target=_drain_output, daemon=True)
    drain_thread.start()

    last_yielded = None
    while proc.poll() is None:
        bytes_downloaded = _bytes_on_disk()
        percent = (
            min(100, round(bytes_downloaded / total_bytes * 100))
            if total_bytes
            else None
        )
        progress = {
            "file": repo_id,
            "percent": percent,
            "bytes_downloaded": bytes_downloaded,
            "bytes_total": total_bytes,
        }
        if progress != last_yielded:
            last_yielded = progress
            yield progress
        time.sleep(1)

    drain_thread.join(timeout=5)

    if proc.returncode != 0:
        raise VLLMError(
            (output_lines[-1] if output_lines else None)
            or f"Download failed (exit code {proc.returncode})"
        )

    final_bytes = _bytes_on_disk()
    yield {
        "file": repo_id,
        "percent": 100,
        "bytes_downloaded": final_bytes,
        "bytes_total": total_bytes or final_bytes,
    }

    _register(repo_id, _DEFAULT_MAX_MODEL_LEN, _DEFAULT_DTYPE)


# Curated starting points for vLLM — the counterpart to Lemonade's built-in catalog,
# which vLLM has none of. Every entry is deliberately: (1) a full HF repo with
# safetensors + config.json (NOT GGUF — vLLM can't serve GGUF, see check_servable),
# (2) an architecture vLLM supports (Qwen2, Phi-3, Llama, StableLM), and (3) non-gated
# so a download works without an HF token (this app sends unauthenticated requests).
# Ordered smallest → largest so the fast-on-CPU options (this is a CPU-only box) show
# first. Sizes aren't hardcoded — they're fetched live in suggested_models() so they
# stay honest if a repo changes.
# Only "clean" repos: safetensors weights and little else. Repos that also ship ONNX
# exports or multiple precisions (e.g. SmolLM2, StableLM) were deliberately left out —
# `hf download` pulls the WHOLE repo, so those balloon the download to several times the
# model's actual size (SmolLM2-360M's repo is ~5GB), which is misleading and wasteful
# for a "suggested" pick. Verified each size below matches ~params×2 bytes (bf16), i.e.
# a clean repo. Ordered smallest → largest (fast-on-CPU first).
_SUGGESTED_MODELS = [
    ("Qwen/Qwen2.5-0.5B-Instruct", "Tiny (0.5B) — fastest, light chat / smoke test"),
    ("TinyLlama/TinyLlama-1.1B-Chat-v1.0", "Small (1.1B) — classic lightweight chat"),
    ("Qwen/Qwen2.5-1.5B-Instruct", "Small (1.5B) — quick general chat"),
    ("Qwen/Qwen2.5-Coder-1.5B-Instruct", "Small coding model (1.5B)"),
    ("Qwen/Qwen2.5-3B-Instruct", "Balanced general chat (3B) — a good CPU default"),
    ("microsoft/Phi-3.5-mini-instruct", "Strong for its size (3.8B) — good reasoning"),
    ("Qwen/Qwen2.5-7B-Instruct", "Stronger chat (7B) — noticeably slower on CPU"),
    ("Qwen/Qwen2.5-Coder-7B-Instruct", "Capable coding model (7B) — slower on CPU"),
]


def _fill_sizes(results: list[dict]) -> None:
    """Fill each result's size_gb concurrently via _hf_repo_size_bytes (one file-tree
    fetch per repo). Shared by search_huggingface() and suggested_models(). Mutates in
    place; any per-repo failure just leaves size_gb as-is (None)."""
    if not results:
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(results)) as pool:
        future_to_idx = {pool.submit(_hf_repo_size_bytes, r["id"]): i for i, r in enumerate(results)}
        for fut, idx in future_to_idx.items():
            try:
                size_bytes = fut.result(timeout=8)
            except Exception:
                size_bytes = None
            if size_bytes:
                results[idx]["size_gb"] = size_bytes / 1e9


def suggested_models() -> list[dict]:
    """The curated _SUGGESTED_MODELS as search-result dicts (same shape
    search_huggingface() returns), so the Downloads page renders them with the same
    card code. Shown when the vLLM search box is empty — the 'browse suggestions' case
    Lemonade already had and vLLM didn't. size_gb filled live; 'downloaded' reflects
    the local registry so already-pulled ones are marked."""
    registry = _load_registry()
    results = [
        {
            "id": repo_id,
            "checkpoint": repo_id,
            "source": "huggingface",
            "size_gb": None,
            "labels": ["text-generation"],
            "downloaded": repo_id in registry,
            "suggested": True,
            "description": desc,
            "last_modified": None,
            "likes": None,
            "downloads": None,
            "pipeline_tag": "text-generation",
        }
        for repo_id, desc in _SUGGESTED_MODELS
    ]
    _fill_sizes(results)
    return results


def search_huggingface(query: str, limit: int = 12) -> list[dict]:
    """Live search against Hugging Face's public model API (no key needed) — vLLM has
    no catalog of its own (unlike Lemonade's curated list), so this is the only
    search source. Scoped to filter=text-generation (vLLM serves causal-LM repos
    directly, not GGUF quant files the way llama.cpp-based backends do, so no
    'gguf' filter here — see lemonade_client.search_huggingface() for that variant).
    Same shape as lemonade_client.search_huggingface()'s results where the fields
    overlap, so the Downloads page can render both with one code path.

    Unlike Lemonade's version, size_gb is filled in here (concurrently, one file-tree
    fetch per result via _hf_repo_size_bytes — same call check_safe_to_load() uses) —
    a vLLM repo has one canonical set of weight files, not many alternate GGUF
    quantizations of different sizes the way llama.cpp-based backends do, so a single
    real size is meaningful to show before downloading. Directly relevant after a real
    incident this session (a 71.9GB model downloaded and only then found to be
    too large to load) — seeing the size up front, before committing to a download,
    is the whole point."""
    if not query.strip():
        return []
    try:
        resp = requests.get(
            "https://huggingface.co/api/models",
            params={
                "search": query,
                "filter": "text-generation",
                "limit": limit,
                "sort": "downloads",
                "expand[]": ["lastModified", "likes", "downloads", "pipeline_tag", "tags"],
            },
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise VLLMError(f"Hugging Face search failed: {exc}") from exc

    results = []
    for m in resp.json():
        repo_id = m.get("id")
        if not repo_id:
            continue
        results.append(
            {
                "id": repo_id,
                "checkpoint": repo_id,
                "source": "huggingface",
                "size_gb": None,  # filled in below, concurrently, once all results are built
                "labels": [m.get("pipeline_tag")] if m.get("pipeline_tag") else [],
                "downloaded": repo_id in _load_registry(),
                "suggested": False,
                "last_modified": m.get("lastModified"),
                "likes": m.get("likes"),
                "downloads": m.get("downloads"),
                "pipeline_tag": m.get("pipeline_tag"),
                "tags": m.get("tags") or [],
            }
        )

    _fill_sizes(results)
    return results


def chat_stream(
    messages: list[dict],
    model: str,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    frequency_penalty: float | None = 0.3,
):
    """Yield raw SSE 'data: ...' lines proxied from vLLM's OpenAI-compatible chat
    endpoint. Same shape and defaults as lmstudio_client.chat_stream()/
    lemonade_client.chat_stream() — frequency_penalty defaults on (mild, 0.3) for the
    same repetition-loop reason documented there. Pass None to disable."""
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if frequency_penalty:
        payload["frequency_penalty"] = frequency_penalty

    try:
        with requests.post(
            f"{base_url()}/v1/chat/completions",
            json=model_test_profiles.apply(payload, "vllm"),
            headers=_auth_headers(),
            stream=True,
            # Read timeout is generous (15 min): CPU prefill is slow (~15 tok/s on this
            # box), so a large prompt near the model's full context window can take
            # several minutes just to prefill before the first token streams.
            timeout=(10, 900),
        ) as resp:
            if resp.status_code >= 400:
                # raise_for_status()'s message is just "400 Client Error: Bad Request
                # for url: ..." — the actually-useful reason is in the JSON body, which
                # that helper discards. Read it and surface something a user can act on.
                # The dominant case on this small-context CPU model is a context-length
                # overflow (a long conversation, or a web-search-augmented message whose
                # injected page text blows the window), so that one gets a plain-English
                # rewrite pointing at the real fixes.
                raise VLLMError(_explain_http_error(resp))
            for line in resp.iter_lines():
                if line:
                    yield line.decode("utf-8", errors="replace")
    except requests.RequestException as exc:
        raise VLLMError(f"Chat request failed: {exc}") from exc


def _explain_http_error(resp) -> str:
    """Turn a vLLM 4xx/5xx into an actionable message, pulling the real reason out of
    the response body (which raise_for_status throws away)."""
    detail = ""
    try:
        body = resp.json()
        err = body.get("error")
        detail = (err.get("message") if isinstance(err, dict) else str(err)) or ""
    except (ValueError, AttributeError):
        detail = (resp.text or "").strip()
    if "maximum context length" in detail.lower() or "context length" in detail.lower():
        return (
            "This conversation is too long for the model's context window. "
            "The loaded vLLM model has a limited context, and a long chat (or a "
            "web-search reply that pulled in a lot of page text) has exceeded it. "
            "Start a new chat, edit/shorten an earlier message, or load a model with a "
            "larger context from the Models panel. "
            f"(vLLM: {detail})"
        )
    if detail:
        return f"vLLM rejected the request ({resp.status_code}): {detail}"
    return f"vLLM request failed with HTTP {resp.status_code}."
