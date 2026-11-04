"""Standalone FastFlowLM (FLM) backend — the AMD Ryzen AI NPU runtime, run directly rather
than through Lemonade. Serves an OpenAI-compatible API on 127.0.0.1:52625 via `flm serve`.

Important: FastFlowLM and Lemonade both drive the SAME NPU, and only one FLM process can hold
it at a time. So this backend and Lemonade's NPU models are mutually exclusive — app.py frees
the other one before activating this (see the load endpoint's NPU-coordination). Because FLM
model tags (llama3.2:1b, deepseek-r1:8b, …) overlap Lemonade's flm-recipe ids, this app namespaces
them with an "flm/" prefix so routing is unambiguous; the prefix is stripped when talking to FLM.

The `flm serve` process runs as a SYSTEM service (flm.service) because the NPU needs raised
memlock, which a --user unit can't grant. The app controls that system unit via a polkit rule
(50-flm.rules) — start/stop without a password. `flm list`/`flm pull` run the binary directly
as this user (no NPU, no memlock needed)."""

from __future__ import annotations

import json
import base64
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import requests
import backend_broker_client
import model_test_profiles
import flm_context


def _broker_flm(args: list[str], timeout: int = 900) -> str | None:
    if not backend_broker_client.broker_configured():
        return None
    try:
        response = requests.post(
            backend_broker_client.broker_src_url().rstrip("/") + "/flm",
            json={"args": args},
            headers={"Authorization": f"Bearer {backend_broker_client.broker_token()}"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise FastFlowLMError(f"FastFlowLM broker request failed: {exc}") from exc
    if response.status_code >= 400:
        try:
            message = response.json().get("error") or str(response.status_code)
        except ValueError:
            message = str(response.status_code)
        raise FastFlowLMError(f"FastFlowLM broker rejected '{args[0] if args else ''}': {message}")
    try:
        payload = response.json()
        # Read-only calls (pull-progress) return a plain progress payload with no
        # returncode/stdout fields, so return it serialized as the CLI "stdout" so the
        # caller can json.loads it back into a dict.
        if not isinstance(payload, dict) or "returncode" not in payload:
            return json.dumps(payload)
        returncode = int(payload["returncode"])
        stdout = str(payload.get("stdout") or "")
        stderr = str(payload.get("stderr") or "")
    except (ValueError, TypeError, KeyError) as exc:
        raise FastFlowLMError(f"FastFlowLM broker returned a malformed response: {exc}") from exc
    if returncode != 0 and "--version" not in args:
        raise FastFlowLMError((stderr or stdout or "flm command failed").strip())
    return stdout

import flm_backend_profile
import settings_store

FLM_DIR = Path(os.environ.get("FLM_DIR", "/opt/fastflowlm"))
FLM_BIN = os.environ.get("FLM_BIN", str(FLM_DIR / "bin" / "flm"))
_ENV = {**os.environ, "LD_LIBRARY_PATH": str(FLM_DIR / "lib")}
SERVICE_NAME = "flm.service"  # SYSTEM unit (needs raised memlock); controlled via polkit
# The shared FastFlowLM state directory lives on the host (active / manually-unloaded).
# Host Hearth uses Path.home(); Docker Hearth points at the host's absolute path (via
# HEARTH_FASTFLOWLM_STATE_DIR) so the same files are read and written from both sides.
_STATE_DIR = Path(os.environ.get(
    "HEARTH_FASTFLOWLM_STATE_DIR", str(Path.home() / ".config" / "fastflowlm")
))
_ACTIVE_FILE = _STATE_DIR / "active"
_PAUSED_FILE = _STATE_DIR / "manually-unloaded"


def _share_with_ownership(path: Path) -> None:
    """Keep host-writable permissions on a shared FastFlowLM state file.

    The Docker app runs as root while mounting the host's `~/.config/fastflowlm`, so a
    naive `touch`/`write_text` leaves the file root-owned and the host Hearth install
    (running as the actual host user) then gets PermissionError adding or clearing the
    paused/active flag. Mirror the nearest non-root ancestor's owner onto the state file
    we create so both installs can drive the same backend."""
    import os as _os
    owner = _host_shared_owner()
    if owner is None:
        return
    try:
        st = path.stat()
    except FileNotFoundError:
        return
    except OSError:
        return
    try:
        if (st.st_uid, st.st_gid) != owner:
            _os.chown(path, owner[0], owner[1])
            _os.chmod(path, 0o664)
    except OSError:
        pass


def _host_shared_owner() -> tuple[int, int] | None:
    """The uid/gid the host Hearth install runs under.

    Walks up from `~/.config/fastflowlm` to the first ancestor with a non-root owner —
    in practice the host mount `~/.config` — so a state file created by the container's
    root user does not lock the host Hearth install out of its own backend state."""
    import os as _os
    if _os.geteuid() != 0:
        return None
    env_uid = _os.environ.get("HEARTH_HOST_UID")
    env_gid = _os.environ.get("HEARTH_HOST_GID")
    if env_uid and env_uid.isdigit():
        try:
            gid = int(env_gid) if env_gid and env_gid.isdigit() else int(env_uid)
            return int(env_uid), gid
        except ValueError:
            pass
    probe = _STATE_DIR.parent
    for _ in range(6):
        try:
            st = probe.stat()
        except OSError:
            break
        if st.st_uid != 0:
            return st.st_uid, st.st_gid
        if probe == probe.parent:
            break
        probe = probe.parent
    return None


def _reclaim_state_ownership() -> None:
    """Ensure a root-owned state directory/files from a previous Docker write become
    writable by the host Hearth install again."""
    import os as _os
    owner = _host_shared_owner()
    if owner is None:
        return
    try:
        st = _STATE_DIR.stat()
    except FileNotFoundError:
        return
    except OSError:
        return
    try:
        if (st.st_uid, st.st_gid) != owner:
            _os.chown(_STATE_DIR, owner[0], owner[1])
            _os.chmod(_STATE_DIR, 0o775)
    except OSError:
        pass
    for candidate in (_ACTIVE_FILE, _PAUSED_FILE):
        try:
            st = candidate.stat()
        except FileNotFoundError:
            continue
        except OSError:
            continue
        if (st.st_uid, st.st_gid) != owner:
            _os.chown(candidate, owner[0], owner[1])
        _os.chmod(candidate, 0o664)


def requests_paused() -> bool:
    return _PAUSED_FILE.exists()
_CONVERSION_DIR = Path(os.environ.get(
    "FLM_CONVERSION_OUTPUT_ROOT", str(Path.home() / ".config" / "flm" / "conversions")
)).expanduser().resolve()
_XCLBIN_ROOT = Path(os.environ.get("FLM_XCLBIN_ROOT", str(FLM_DIR / "share/flm/xclbins")))

_DEFAULT_BASE_URL = "http://127.0.0.1:52625"
_ID_PREFIX = "flm/"


class FastFlowLMError(Exception):
    pass


def base_url() -> str:
    return (settings_store.get("fastflowlm_base_url") or _DEFAULT_BASE_URL).rstrip("/")


def api_key() -> str | None:
    return settings_store.get("fastflowlm_api_key") or None


def _auth_headers() -> dict:
    key = api_key()
    return {"Authorization": f"Bearer {key}"} if key else {}


def _run(args: list[str], timeout: int = 60) -> str:
    try:
        r = subprocess.run([FLM_BIN, *args], capture_output=True, text=True, env=_ENV, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise FastFlowLMError(f"'flm {' '.join(args)}' timed out") from exc
    except FileNotFoundError:
        if _broker_flm(args, timeout=timeout) is None:
            raise FastFlowLMError("FastFlowLM binary not found at " + FLM_BIN)
        return _broker_flm(args, timeout=timeout)
    return r.stdout


# ---- service control (system unit via polkit) --------------------------------------
def _systemctl(*args: str, timeout: int = 60) -> str:
    if os.name == "nt":
        raise FastFlowLMError("FastFlowLM service management is available on Linux only.")
    try:
        # Docker Hearth has no systemd: route through the host backend broker.
        brokered = backend_broker_client.systemctl_via_broker(
            list(args), SERVICE_NAME, scope="system", timeout=timeout,
        )
        if brokered is not None:
            return brokered
        r = subprocess.run(["systemctl", *args, SERVICE_NAME], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise FastFlowLMError(f"'systemctl {' '.join(args)} {SERVICE_NAME}' timed out") from exc
    except backend_broker_client.BackendBrokerError:
        raise
    except FileNotFoundError as exc:
        # The Docker app container intentionally has no systemd. FastFlowLM's service is a
        # host system unit, so an absent systemctl means "not manageable from here", not a
        # fatal inventory error. Callers such as is_active()/list_models() treat this as an
        # unavailable backend and keep the other backends usable.
        raise FastFlowLMError(
            "FastFlowLM service management is unavailable because systemctl was not found. "
            "The Docker app container has no systemd; manage flm.service on the host."
        ) from exc
    except OSError as exc:
        raise FastFlowLMError(f"Could not run systemctl for {SERVICE_NAME}: {exc}") from exc
    if r.returncode != 0 and "is-active" not in args:
        raise FastFlowLMError((r.stderr or r.stdout or "systemctl failed").strip())
    return r.stdout


def is_active() -> bool:
    try:
        return _systemctl("is-active", timeout=8).strip() == "active"
    except FastFlowLMError:
        return False


def _read_active() -> str | None:
    try:
        return _ACTIVE_FILE.read_text().strip() or None
    except OSError:
        return None


def _mkdir_state_dir() -> None:
    """Create the shared FastFlowLM state dir with the same ownership as its ancestor.

    If Docker Hearth creates `~/.config/fastflowlm/` before the host Hearth install ever
    has, a plain mkdir would leave it root-owned; when the ancestor (the host mount,
    `~/.config`) already has a real user owner, mirror that owner so both installs (one
    on the host, one in the container) can drive the same backend."""
    import os as _os
    ancestors = []
    probe = _STATE_DIR.parent
    for _ in range(4):
        try:
            st = probe.stat()
        except OSError:
            st = None
        if st is not None and st.st_uid != 0:
            _STATE_DIR.mkdir(parents=True, exist_ok=True)
            if _os.geteuid() == 0:
                try:
                    current = _STATE_DIR.stat()
                    if (current.st_uid, current.st_gid) != (st.st_uid, st.st_gid):
                        _os.chown(_STATE_DIR, st.st_uid, st.st_gid)
                        _os.chmod(_STATE_DIR, 0o775)
                except OSError:
                    pass
            return
        if probe == probe.parent:
            break
        probe = probe.parent
    _STATE_DIR.mkdir(parents=True, exist_ok=True)


def _write_active(tag: str | None) -> None:
    _mkdir_state_dir()
    _ACTIVE_FILE.write_text((tag or "").strip())
    _share_with_ownership(_ACTIVE_FILE)


# ---- model registry / listing ------------------------------------------------------
def _installed() -> list[dict]:
    """Downloaded FLM models with metadata, from `flm list --filter installed --json`."""
    try:
        data = json.loads(_run(["list", "--filter", "installed", "--json"], timeout=30) or "{}")
    except (FastFlowLMError, ValueError):
        return []
    return data.get("models", []) if isinstance(data, dict) else []


def _runtime_version() -> str | None:
    try:
        output = _run(["--version"], timeout=10).strip()
    except FastFlowLMError:
        return None
    match = re.search(r"\d+\.\d+\.\d+", output)
    return match.group(0) if match else None


def _installed_tags(installed: list[dict] | None = None) -> set[str]:
    installed = _installed() if installed is None else installed
    return {
        str(model.get("model") or model.get("name"))
        for model in installed if model.get("model") or model.get("name")
    }


def _local_report(model_id: str, installed_tags: set[str] | None = None,
                  runtime_version: str | None = None) -> dict:
    local_id = _tag(model_id)
    if not local_id.startswith("local:"):
        raise FastFlowLMError(f"'{model_id}' is not a local FLM conversion.")
    name = local_id[len("local:"):]
    directory = (_CONVERSION_DIR / name).resolve()
    if not name or not directory.is_relative_to(_CONVERSION_DIR) or not directory.is_dir():
        raise FastFlowLMError(f"Converted model '{model_id}' was not found.")
    if not flm_backend_profile.is_profile_conversion(directory):
        raise FastFlowLMError(f"Converted model '{model_id}' has no valid 27B profile manifest.")
    return flm_backend_profile.assess_conversion(
        directory,
        _XCLBIN_ROOT,
        installed_tags if installed_tags is not None else _installed_tags(),
        runtime_version if runtime_version is not None else _runtime_version(),
    )


def backend_status() -> dict:
    installed = _installed()
    tags = _installed_tags(installed)
    version = _runtime_version()
    conversions = []
    if _CONVERSION_DIR.is_dir():
        for directory in sorted(_CONVERSION_DIR.iterdir()):
            if directory.is_dir() and flm_backend_profile.is_profile_conversion(directory):
                conversions.append(flm_backend_profile.assess_conversion(
                    directory, _XCLBIN_ROOT, tags, version,
                ))
    return {
        "profile": flm_backend_profile.public_profile(),
        "runtime_version": version,
        "conversions": conversions,
    }


def list_models() -> list[dict]:
    """Installed FLM models, id-prefixed with 'flm/'. The app-activated one is 'loaded'
    while flm.service is running; everything else is 'not-loaded'. Fail-soft (never raises):
    if the binary/service is gone the list is just empty."""
    active = _read_active() if is_active() else None
    models = []
    installed = _installed()
    for m in installed:
        tag = m.get("model") or m.get("name")
        if not tag:
            continue
        fp = m.get("footprint")
        labels = m.get("label") or []
        quantization = (m.get("details") or {}).get("quantization_level") or "NPU"
        if m.get("asr") or "audio" in labels:
            quantization = f"{quantization} · audio translation"
        models.append({
            "id": f"{_ID_PREFIX}{tag}",
            "publisher": "fastflowlm",
            "quantization": quantization,
            "type": "llm",
            "state": "loaded" if tag == active else "not-loaded",
            "configured_context_length": flm_context.context_limit(f"{_ID_PREFIX}{tag}"),
            "size_gb": round(float(fp), 2) if isinstance(fp, (int, float)) else None,
        })
    installed_tags = _installed_tags(installed)
    runtime_version = _runtime_version()
    # Completed engine-development conversions are local artifacts rather than entries
    # in FLM's compiled-in download catalog. Surface them in the same inventory, but do
    # not pretend they are loadable: the 27B model needs dimension-specific kernels and
    # a runtime catalog tag before `flm serve` can resolve it safely.
    if _CONVERSION_DIR.is_dir():
        for directory in sorted(_CONVERSION_DIR.iterdir()):
            if not directory.is_dir() or not flm_backend_profile.is_profile_conversion(directory):
                continue
            report = flm_backend_profile.assess_conversion(
                directory, _XCLBIN_ROOT, installed_tags, runtime_version,
            )
            weights = directory / "model.q4nx"
            load_error = flm_backend_profile.blocker_message(report, limit=5) or None
            models.append({
                "id": f"{_ID_PREFIX}local:{directory.name}",
                "publisher": "local conversion",
                "quantization": "Q4NX · mixed Q4_1/Q8_0 · text-only",
                "type": "llm",
                "state": "loaded" if report["ready"] and active == report["runtime_tag"] else "not-loaded",
                "size_gb": round(weights.stat().st_size / (1024 ** 3), 2)
                    if weights.is_file() else None,
                "converted": True,
                "loadable": report["ready"],
                "load_error": load_error,
                "load_blockers": report["blockers"],
                "warnings": report["warnings"],
                "deletable": True,
                "runtime_tag": report["runtime_tag"],
                "backend_profile": report["profile"],
            })
    return models


def check_loadable(model_id: str) -> None:
    """Preflight a model before app.py evicts the currently loaded reasoning model."""
    if shutil.which("systemctl") is None:
        raise FastFlowLMError(
            "FastFlowLM's systemd service cannot be managed from this environment. "
            "The Docker app container has no systemd; use the host Hearth installation "
            "or manage flm.service on the host."
        )
    tag = _tag(model_id)
    if not tag.startswith("local:"):
        if tag not in _installed_tags():
            raise FastFlowLMError(f"'{tag}' isn't downloaded. Pull it first from the Downloads panel.")
        return
    report = _local_report(model_id)
    if not report["ready"]:
        raise FastFlowLMError(
            flm_backend_profile.blocker_message(report, limit=12)
            or "Converted model is not runtime-ready."
        )


def resolve_model(model_id: str) -> str:
    """Resolve a UI model ID to the exact runtime tag after local-artifact preflight."""
    tag = _tag(model_id)
    if tag.startswith("local:"):
        check_loadable(model_id)
        return flm_backend_profile.RUNTIME_TAG
    return tag


def search_catalog(query: str, limit: int = 80) -> list[dict]:
    """FastFlowLM's built-in NPU model catalog (`flm list --filter all --json`) as
    Downloads-page result cards. FLM ships a fixed, curated catalog rather than a live
    Hugging Face search, so this IS the whole universe of pullable NPU tags — filtered
    client-side (tag/family/params/quant/label) against the query. Real size comes from
    each entry's 'footprint' (its on-NPU GB), and 'installed' marks the ones already
    downloaded. Cards download via /api/models/download with backend=fastflowlm (bare
    tag). Fail-soft: empty list if the binary/catalog is unavailable.

    Shape matches lemonade_client.search_huggingface()'s result dicts where the fields
    overlap, so downloads.js renders these with the same card code."""
    try:
        data = json.loads(_run(["list", "--filter", "all", "--json"], timeout=30) or "{}")
    except (FastFlowLMError, ValueError):
        return []
    models = data.get("models", []) if isinstance(data, dict) else []
    # Token-wise matching tolerates the punctuation differences between human queries
    # and FLM tags ("qwen 0.6b" should match "qwen3:0.6b").
    q_parts = re.findall(r"[a-z0-9]+", query.strip().lower())
    results = []
    for m in models:
        tag = m.get("model") or m.get("name")
        if not tag:
            continue
        details = m.get("details") or {}
        labels = [l for l in (m.get("label") or []) if l]
        family = details.get("family") or ""
        params = details.get("parameter_size") or ""
        quant = details.get("quantization_level") or ""
        haystack = " ".join([tag, family, params, quant, " ".join(labels)]).lower()
        if q_parts and not all(part in haystack for part in q_parts):
            continue
        desc_bits = []
        if params:
            desc_bits.append(f"{params} params")
        if quant:
            desc_bits.append(quant)
        if details.get("think"):
            desc_bits.append("reasoning")
        if labels:
            desc_bits.append(", ".join(labels))
        fp = m.get("footprint")
        results.append({
            "id": tag,  # bare FLM tag; the download posts backend=fastflowlm
            "source": "fastflowlm",
            "source_url": m.get("file_url") or m.get("url"),
            "size_gb": round(float(fp), 2) if isinstance(fp, (int, float)) else None,
            "labels": labels or ["text-generation"],
            "pipeline_tag": "text-generation",
            "downloaded": bool(m.get("installed")),
            "suggested": False,
            "description": " · ".join(desc_bits) or None,
            "last_modified": m.get("modified_at"),
            "likes": None,
            "downloads": None,
        })
    # Small catalog: installed first, then smallest-first so the quickest pulls surface.
    results.sort(key=lambda r: (not r["downloaded"], r["size_gb"] if r["size_gb"] is not None else 999))
    return results[:limit]


def owns_model(model: str) -> bool:
    return bool(model) and model.startswith(_ID_PREFIX)


def _tag(model: str) -> str:
    return model[len(_ID_PREFIX):] if model.startswith(_ID_PREFIX) else model


def current_served_model() -> str | None:
    a = _read_active()
    return f"{_ID_PREFIX}{a}" if (a and is_active()) else None


# ---- chat --------------------------------------------------------------------------
def chat_stream(messages, model, temperature=0.7, max_tokens=None, frequency_penalty=0.3,
                tools=None, enable_thinking=True, parallel_tool_calls=None):
    """Yield raw SSE 'data: ...' lines from FLM's OpenAI-compatible endpoint. FLM auto-loads
    the requested model onto the NPU (switching from whatever was active).

    `tools`: OpenAI-format tool definitions (see mcp_tools_client.py). When provided, this
    routes to flm_tool_shim.py (127.0.0.1:52626) instead of the raw engine (base_url(),
    52625) - the shim is what actually hardens FastFlowLM's tool-call output (repairs
    missing/empty arguments, injects the model's own tool-call prompt format, watches for
    degenerate output) for real OpenCode/LAN traffic; the raw engine alone reproduces the
    exact malformed-tool-call problems that shim exists to fix. The shim also does its own
    NPU-lock acquisition and model loading (_ensure_fastflow_ready) - the caller (app.py's
    webui-chat worker) MUST NOT also hold npu_request_lock across this call, since both
    lock on the same machine-wide flock and the shim runs as a separate process: holding
    it here while the shim tries to acquire it too is a real deadlock, not just redundant
    work. See app.py's worker() - it skips its own _claim_selected_npu for this exact case."""
    runtime_tag = resolve_model(model)
    payload = {"model": runtime_tag, "messages": messages, "temperature": temperature,
               "stream": True, "stream_options": {"include_usage": True},
               "enable_thinking": enable_thinking}
    if max_tokens:
        payload["max_tokens"] = min(max_tokens, flm_context.output_limit(runtime_tag)) if runtime_tag.startswith("qwen3.8") else max_tokens
    if tools:
        payload["tools"] = tools
        if parallel_tool_calls is not None:
            payload["parallel_tool_calls"] = parallel_tool_calls
    # A mild repetition guard, same as lemonade_client.chat_stream sends — without it,
    # FastFlowLM has NO repetition penalty at all and long generations on some quantized
    # NPU models degenerate into a token-repetition loop (confirmed live: qwen3.6-moe:35b-a3b
    # produced a coherent answer that then collapsed into ".s worths…"/"d.i.s.t.o.p.i.a.n…"
    # garbage all the way to the 4096-token cap). This param was declared here but never
    # actually sent, so the guard the rest of the app relies on silently didn't apply to
    # this backend. Pass frequency_penalty=None to disable.
    if frequency_penalty:
        payload["frequency_penalty"] = frequency_penalty
    url = "http://127.0.0.1:52626/v1/chat/completions" if tools else f"{base_url()}/v1/chat/completions"
    headers = {"X-Hearth-Client": "webui-chat"} if tools else _auth_headers()
    try:
        with requests.post(url, json=model_test_profiles.apply(payload, "fastflowlm"),
                           headers=headers, stream=True, timeout=(10, 900)) as resp:
            if resp.status_code >= 400:
                detail = ""
                try:
                    err = resp.json().get("error")
                    detail = (err.get("message") if isinstance(err, dict) else str(err)) or ""
                except (ValueError, AttributeError):
                    detail = (resp.text or "").strip()
                raise FastFlowLMError(f"FastFlowLM error {resp.status_code}: {detail or 'request failed'}")
            saw_event = False
            for line in resp.iter_lines(chunk_size=1):
                if line:
                    decoded = line.decode("utf-8", errors="replace")
                    if decoded.startswith("data:") and decoded[5:].strip() != "[DONE]":
                        saw_event = True
                    yield decoded
            if not saw_event:
                raise FastFlowLMError(
                    "FastFlowLM returned no response. The model may have failed to load; "
                    "check flm.service logs and its --ctx-len setting."
                )
    except requests.RequestException as exc:
        raise FastFlowLMError(f"Chat request failed: {exc}") from exc
    # A successful chat means this tag is now the one on the NPU.
    _write_active(runtime_tag)


VOICE_TRANSLATION_MODEL = "gemma4-it:e4b"

# Keep this allow-list shared by the UI-facing route and the model client.  Besides
# preventing prompt injection through target_language, it documents the languages
# this feature has deliberately exposed and tested.  The four major South Indian
# languages are included explicitly (rather than relying on a free-form target).
VOICE_TRANSLATION_LANGUAGES = frozenset({
    "English", "Spanish", "French", "German", "Hindi", "Chinese",
    "Japanese", "Korean", "Arabic", "Portuguese", "Italian", "Polish",
    "Tamil", "Telugu", "Kannada", "Malayalam",
})

_NATIVE_SCRIPT_INSTRUCTIONS = {
    "Tamil": "Use natural Tamil script, not Latin transliteration.",
    "Telugu": "Use natural Telugu script, not phonetic English written in Telugu characters.",
    "Kannada": "Use natural Kannada script, not Latin transliteration.",
    "Malayalam": "Use natural Malayalam script, not Latin transliteration.",
}


def _clean_translation(text: str, target_language: str) -> str:
    """Normalize Gemma's occasional draft/transliteration + final-answer output.

    The audio model sometimes emits an initial phonetic draft followed by the actual
    semantic translation on a second line.  The prompt asks for one line, but choosing
    the final non-empty line makes the API deterministic when the model ignores that.
    """
    lines = [line.strip().strip('"') for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    if target_language in _NATIVE_SCRIPT_INSTRUCTIONS and len(lines) > 1:
        return lines[-1]
    return " ".join(lines)


def transcribe_audio(audio_bytes: bytes) -> str:
    """Speech-to-text with the same Gemma 4 E4B audio model voice translation uses,
    instead of Lemonade's separate Whisper-Tiny model - one fewer NPU model to keep
    loaded for voice input. Reuses lemonade_client's ffmpeg-based WAV conversion (the
    same input-format requirement applies here as it does for Whisper: Chrome's
    MediaRecorder default WebM/Opus isn't accepted directly)."""
    if not audio_bytes:
        raise FastFlowLMError("Audio is empty.")
    import lemonade_client as lemonade
    import npu_request_lock
    wav_bytes = lemonade._convert_to_wav(audio_bytes)
    handle = npu_request_lock.acquire()
    try:
        try:
            for model in lemonade.list_models():
                if model.get("state") == "loaded" and lemonade.is_npu_model(model.get("id") or ""):
                    lemonade.unload_model(model["id"])
        except Exception:
            pass
        ensure_service()
        payload = {
            "model": VOICE_TRANSLATION_MODEL,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": (
                    "Transcribe exactly what is spoken in this audio, verbatim, in its "
                    "original language. Do not translate. Return only the transcript with "
                    "no labels, explanation, or quotation marks. If nothing intelligible is "
                    "spoken, return an empty response."
                )},
                {"type": "input_audio", "input_audio": {
                    "data": base64.b64encode(wav_bytes).decode("ascii"), "format": "wav"
                }},
            ]}],
            "temperature": 0.0,
            "max_tokens": 512,
            "stream": False,
        }
        try:
            response = requests.post(f"{base_url()}/v1/chat/completions", json=payload,
                                     headers=_auth_headers(), timeout=300)
            response.raise_for_status()
            text = response.json()["choices"][0]["message"]["content"].strip().strip('"')
        except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
            detail = getattr(response, "text", "")[:500] if "response" in locals() else str(exc)
            raise FastFlowLMError(f"Gemma transcription failed: {detail or exc}") from exc
        _write_active(VOICE_TRANSLATION_MODEL)
        return text
    finally:
        npu_request_lock.release(handle)


def translate_audio(wav_bytes: bytes, target_language: str) -> str:
    """Translate spoken audio to text with the installed Gemma 4 E4B audio model.

    FastFlowLM's OpenAI-compatible endpoint accepts a base64 WAV `input_audio` block.
    The request owns the NPU lock for the entire inference and records Gemma as active,
    just like normal FLM chat requests.
    """
    if not wav_bytes:
        raise FastFlowLMError("Audio is empty.")
    target_language = target_language.strip()
    if target_language not in VOICE_TRANSLATION_LANGUAGES:
        raise FastFlowLMError("Unsupported target language.")
    import npu_request_lock
    import lemonade_client as lemonade
    handle = npu_request_lock.acquire()
    try:
        try:
            for model in lemonade.list_models():
                if model.get("state") == "loaded" and lemonade.is_npu_model(model.get("id") or ""):
                    lemonade.unload_model(model["id"])
        except Exception:
            pass
        ensure_service()
        payload = {
            "model": VOICE_TRANSLATION_MODEL,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": (
                    "Translate the meaning of the spoken audio into natural, fluent "
                    f"{target_language}. Do not transliterate or spell the source words "
                    f"phonetically. {_NATIVE_SCRIPT_INSTRUCTIONS.get(target_language, '')} "
                    "Return exactly one translation with no transcript, alternatives, labels, "
                    "explanation, or quotation marks."
                )},
                {"type": "input_audio", "input_audio": {
                    "data": base64.b64encode(wav_bytes).decode("ascii"), "format": "wav"
                }},
            ]}],
            "temperature": 0.1,
            "max_tokens": 512,
            "stream": False,
        }
        try:
            response = requests.post(f"{base_url()}/v1/chat/completions", json=payload,
                                     headers=_auth_headers(), timeout=300)
            response.raise_for_status()
            raw_text = response.json()["choices"][0]["message"]["content"].strip()
            text = _clean_translation(raw_text, target_language)
        except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
            detail = getattr(response, "text", "")[:500] if "response" in locals() else str(exc)
            raise FastFlowLMError(f"Gemma voice translation failed: {detail or exc}") from exc
        if not text:
            raise FastFlowLMError("Gemma returned an empty translation.")
        _write_active(VOICE_TRANSLATION_MODEL)
        return text
    finally:
        npu_request_lock.release(handle)


def translate_text(source_text: str, target_language: str) -> str:
    """Translate a verified speech transcript with Gemma.

    Whisper transcription followed by text translation is more reliable than Gemma's
    direct audio translation for longer utterances, especially for Telugu where the
    audio path can emit phonetic transliteration instead of translated meaning.
    """
    source_text = source_text.strip()
    target_language = target_language.strip()
    if not source_text:
        raise FastFlowLMError("Transcript is empty.")
    if target_language not in VOICE_TRANSLATION_LANGUAGES:
        raise FastFlowLMError("Unsupported target language.")

    import npu_request_lock
    import lemonade_client as lemonade
    handle = npu_request_lock.acquire()
    try:
        try:
            for model in lemonade.list_models():
                if model.get("state") == "loaded" and lemonade.is_npu_model(model.get("id") or ""):
                    lemonade.unload_model(model["id"])
        except Exception:
            pass
        ensure_service()
        prompt = (
            f"Translate the following text into natural, fluent {target_language}. "
            "Translate its meaning; never transliterate or preserve source-language words "
            f"phonetically. {_NATIVE_SCRIPT_INSTRUCTIONS.get(target_language, '')} "
            "Return only the translation with no label, explanation, alternatives, or quotes.\n\n"
            f"Source text:\n{source_text}"
        )
        payload = {
            "model": VOICE_TRANSLATION_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "max_tokens": 512,
            "stream": False,
        }
        try:
            response = requests.post(f"{base_url()}/v1/chat/completions", json=payload,
                                     headers=_auth_headers(), timeout=300)
            response.raise_for_status()
            raw_text = response.json()["choices"][0]["message"]["content"].strip()
            text = _clean_translation(raw_text, target_language)
        except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
            detail = getattr(response, "text", "")[:500] if "response" in locals() else str(exc)
            raise FastFlowLMError(f"Gemma text translation failed: {detail or exc}") from exc
        if not text:
            raise FastFlowLMError("Gemma returned an empty translation.")
        _write_active(VOICE_TRANSLATION_MODEL)
        return text
    finally:
        npu_request_lock.release(handle)


def translate_image_text(image_bytes: bytes, mime_type: str, target_language: str) -> str:
    """OCR + translate any text visible in an image, using the same Gemma 4 E4B model as
    voice translation (confirmed live: FastFlowLM's OpenAI-compatible endpoint accepts a
    standard `image_url` data-URI content block the same way it accepts `input_audio`,
    see translate_audio() above).
    """
    if not image_bytes:
        raise FastFlowLMError("Image is empty.")
    target_language = target_language.strip()
    if target_language not in VOICE_TRANSLATION_LANGUAGES:
        raise FastFlowLMError("Unsupported target language.")

    import npu_request_lock
    import lemonade_client as lemonade
    handle = npu_request_lock.acquire()
    try:
        try:
            for model in lemonade.list_models():
                if model.get("state") == "loaded" and lemonade.is_npu_model(model.get("id") or ""):
                    lemonade.unload_model(model["id"])
        except Exception:
            pass
        ensure_service()
        payload = {
            "model": VOICE_TRANSLATION_MODEL,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": (
                    "Read every piece of text visible in this image (signs, labels, captions, "
                    "documents, UI text - anything legible) and translate its meaning into "
                    f"natural, fluent {target_language}. Do not transliterate or spell source "
                    f"words phonetically. {_NATIVE_SCRIPT_INSTRUCTIONS.get(target_language, '')} "
                    "If the image contains no legible text, respond with exactly: No text found "
                    "in image. Otherwise return only the translation, with no transcript, "
                    "labels, explanation, or quotation marks."
                )},
                {"type": "image_url", "image_url": {
                    "url": f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
                }},
            ]}],
            "temperature": 0.1,
            "max_tokens": 512,
            "stream": False,
        }
        try:
            response = requests.post(f"{base_url()}/v1/chat/completions", json=payload,
                                     headers=_auth_headers(), timeout=300)
            response.raise_for_status()
            raw_text = response.json()["choices"][0]["message"]["content"].strip()
            text = _clean_translation(raw_text, target_language)
        except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
            detail = getattr(response, "text", "")[:500] if "response" in locals() else str(exc)
            raise FastFlowLMError(f"Gemma image translation failed: {detail or exc}") from exc
        if not text:
            raise FastFlowLMError("Gemma returned an empty translation.")
        _write_active(VOICE_TRANSLATION_MODEL)
        return text
    finally:
        npu_request_lock.release(handle)


# ---- load / unload (NPU coordination with Lemonade is done in app.py) ---------------
def load_model(model_id: str) -> None:
    """Make an installed FLM model the active NPU model: ensure flm.service is running and
    record it active. app.py must have already freed the NPU from Lemonade before calling.
    (FLM auto-switches on request, so no restart is needed to change models.)"""
    tag = resolve_model(model_id)
    if not any((m.get("model") or m.get("name")) == tag for m in _installed()):
        raise FastFlowLMError(f"'{tag}' isn't downloaded. Pull it first from the Downloads panel.")
    previous = _read_active()
    # The service launcher reads this tag to select the model-specific context.
    _write_active(tag)
    try:
        ensure_service()
    except Exception:
        _write_active(previous)
        raise
    _PAUSED_FILE.unlink(missing_ok=True)


def ensure_service() -> None:
    """Start the lightweight FLM API process if an NPU handoff previously stopped it."""
    if not is_active():
        _systemctl("start")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                requests.get(f"{base_url()}/v1/models", timeout=3).raise_for_status()
                break
            except requests.RequestException:
                time.sleep(1)
        else:
            raise FastFlowLMError("flm.service didn't come up on :52625.")


def unload_model(model_id: str | None = None, *, npu_lock_held: bool = False) -> None:
    """Stop flm.service, releasing the NPU (so Lemonade can use it again)."""
    lock_handle = None
    if not npu_lock_held:
        # Manual unload must persist against OpenCode's automatic retries. Internal
        # NPU/model handoffs own the lock and must remain automatically recoverable.
        _reclaim_state_ownership()
        _mkdir_state_dir()
        try:
            _PAUSED_FILE.touch(mode=0o600)
        except PermissionError as exc:
            # The shared state was written by the other Hearth install as root and our
            # chown attempt failed; surface a clear error instead of a raw stack trace.
            raise FastFlowLMError(
                "FastFlowLM's paused-state file is not writable from this installation: "
                f"{_PAUSED_FILE} ({exc}). Clear it on the host, then retry."
            ) from exc
        # The shared state must stay writable by the host Hearth install too.
        _share_with_ownership(_PAUSED_FILE)
        import npu_request_lock
        try:
            lock_handle = npu_request_lock.acquire(timeout=15)
        except TimeoutError as exc:
            raise FastFlowLMError(
                "FastFlowLM is still serving an active NPU request. Unload was not "
                "performed; wait for the request to finish or cancel it, then retry."
            ) from exc
    try:
        _systemctl("stop")
        _write_active(None)
    finally:
        if lock_handle is not None:
            npu_request_lock.release(lock_handle)


# ---- download (flm pull) -----------------------------------------------------------
def download_model(query: str):
    """Pull an FLM model by tag via `flm pull <tag>`, yielding structured progress dicts
    (shape app.py's structured download worker relays). Accepts 'flm/<tag>' or a bare tag.

    From the Docker app the pull runs on the Hearth host through the backend broker, so
    this function relays the broker's pull-progress instead of the CLI's stdout; the
    shape is identical either way."""
    import re
    tag = _tag(query.strip())
    if not tag:
        raise FastFlowLMError("A FastFlowLM model tag is required (e.g. gpt-oss:20b).")
    if backend_broker_client.broker_configured() and not os.path.isfile(FLM_BIN):
        yield from _download_model_via_broker(tag)
        return
    try:
        proc = subprocess.Popen([FLM_BIN, "pull", tag], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, env=_ENV)
    except FileNotFoundError as exc:
        raise FastFlowLMError("FastFlowLM binary not found.") from exc
    last_pct = -1
    file_no, total = 1, 1
    for line in proc.stdout:
        fm = re.search(r"Downloading (\d+)/(\d+)", line)
        if fm:
            file_no, total = int(fm.group(1)), int(fm.group(2))
        pm = re.search(r"(\d+(?:\.\d+)?)%", line)
        if pm:
            pct = int(float(pm.group(1)))
            if pct != last_pct:
                last_pct = pct
                yield {"file": f"file {file_no}/{total}", "file_index": file_no, "total_files": total, "percent": pct}
    code = proc.wait()
    if code != 0:
        raise FastFlowLMError(f"'flm pull {tag}' failed (exit {code}). Check the tag exists in FastFlowLM's catalog.")
    yield {"file": tag, "file_index": total, "total_files": total, "percent": 100}


def _download_model_via_broker(tag: str):
    """Run the host's `flm pull <tag>` and yield the same progress dicts as the CLI path

    The broker runs the pull in the background and reports progress, so the caller can
    drive the same UI bar without the backend's stdout being streamed into the process
    that made the request."""
    _broker_flm(["pull", tag], timeout=5)
    last_pct = -1
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        snapshot = _flm_pull_progress()
        if not snapshot.get("running"):
            break
        pct = int(snapshot.get("percent") or 0)
        if pct != last_pct:
            last_pct = pct
            yield {
                "file": "file 1/1", "file_index": 1, "total_files": 1,
                "percent": pct,
                "bytes_downloaded": snapshot.get("bytes_downloaded"),
                "bytes_total": snapshot.get("bytes_total"),
            }
        time.sleep(2)
    # The broker reports the pull as finished either when it exits or when a concurrent
    # CLI pull ends; verify the target actually landed before telling the UI "done".
    installed = _installed_tags()
    if last_pct in (-1, 100) or tag in installed:
        yield {"file": tag, "file_index": 1, "total_files": 1, "percent": 100}
        return
    raise FastFlowLMError(f"'flm pull {tag}' did not finish within the expected window.")


def _flm_pull_progress() -> dict:
    """Pull-progress snapshot, same keys the broker's /flm pull-progress returns."""
    if backend_broker_client.broker_configured() and not os.path.isfile(FLM_BIN):
        try:
            out = _broker_flm(["pull-progress"], timeout=10) or ""
            value = json.loads(out or "null")
            return value if isinstance(value, dict) else {"running": False}
        except FastFlowLMError:
            return {"running": False}
    import re
    # Local pull in progress (host Hearth process)
    try:
        out = subprocess.run(["pgrep", "-af", "flm pull"], capture_output=True, text=True, timeout=5).stdout
    except (subprocess.SubprocessError, OSError):
        return {"running": False}
    tag = None
    for line in out.splitlines():
        m = re.search(r"\bflm\s+pull\s+(\S+)", line)
        if m and not m.group(1).startswith("-"):
            tag = m.group(1)
            break
    if not tag:
        return {"running": False}
    return _local_pull_progress(tag)


def _local_pull_progress(tag: str) -> dict:
    """The same on-disk progress math the broker uses, but for a local (host) FLM pull
    Hearth started inside its own process tree."""
    import re as _re
    dirname, total = _pull_catalog_map().get(tag, (None, None))
    if not dirname:
        return {"running": False}
    try:
        directory = _STATE_DIR.parent / "flm" / "models" / dirname
        cur = sum(f.stat().st_size for f in directory.rglob("*") if f.is_file()) if directory.is_dir() else 0
    except OSError:
        cur = 0
    return {
        "running": True,
        "percent": int(min(99, cur * 100 / total)) if total else 0,
        "bytes_downloaded": cur,
        "bytes_total": total,
    }


_pull_catalog_cache: dict | None = None


def _pull_catalog_map() -> dict:
    """tag -> (on-disk dir name, expected total bytes), from the FLM catalog — cached since
    it's static. Lets a running `flm pull` be turned into a percentage from on-disk size
    without parsing the pull's stdout (which this app never sees for a CLI-started pull)."""
    global _pull_catalog_cache
    if _pull_catalog_cache is None:
        import re
        result: dict = {}
        try:
            data = json.loads(_run(["list", "--filter", "all", "--json"], timeout=15) or "{}")
        except (FastFlowLMError, ValueError):
            data = {}
        for e in (data.get("models") or []):
            tag = e.get("model") or e.get("name")
            if not tag:
                continue
            # On-disk dir = the HF repo name. The catalog URLs are inconsistent — some carry
            # a /tree|/resolve suffix, some are the bare repo root, some are the /api/models/
            # form — so match "<org>/<repo>" and take the repo half, tolerating all three.
            url = e.get("file_url") or e.get("url") or ""
            dm = re.search(r"huggingface\.co/(?:api/models/)?[^/]+/([^/]+?)(?:/(?:resolve|tree|blob)\b|/?$)", url)
            fp = e.get("footprint")
            total = int(float(fp) * 1e9) if isinstance(fp, (int, float)) and fp else None
            if dm:
                result[tag] = (dm.group(1), total)
        _pull_catalog_cache = result
    return _pull_catalog_cache


def active_pull_progress() -> dict | None:
    """If an `flm pull <tag>` is running right now — including one started OUTSIDE this app
    (e.g. a CLI `flm pull --force`) — report its progress by measuring the model's on-disk
    dir against the catalog's expected footprint, so the Downloads page can show it. None if
    no pull is running or the tag can't be resolved. Structured-progress shape, same as
    download_model yields, so the Downloads page renders it with its existing snapshot code."""
    try:
        snapshot = _flm_pull_progress()
    except Exception:
        return None
    if not snapshot.get("running"):
        return None
    return {
        "model_id": f"{_ID_PREFIX}{snapshot.get('tag') if isinstance(snapshot.get('tag'), str) else snapshot}",
        "backend": "fastflowlm",
        "source": "fastflowlm",
        "kind": "structured",
        "progress": {
            "percent": snapshot.get("percent") or 0,
            "bytes_downloaded": snapshot.get("bytes_downloaded") or 0,
            "bytes_total": snapshot.get("bytes_total"),
            "file": "model.q4nx",
            "file_index": 1,
            "total_files": 1,
        },
    }


def delete_model(model_id: str) -> None:
    tag = _tag(model_id)
    if tag.startswith("local:"):
        _delete_local_conversion(tag)
        return
    if _read_active() == tag:
        _write_active(None)
    out = _run(["remove", tag], timeout=60)
    if "error" in out.lower() and "removed" not in out.lower():
        raise FastFlowLMError(out.strip()[:200] or f"Couldn't remove '{tag}'.")


def _delete_local_conversion(tag: str) -> None:
    """Deletes a local engine-development conversion's files on disk.

    These aren't in FLM's own catalog (`flm remove` has no concept of them - they're just
    directories this app's conversion tooling wrote under _CONVERSION_DIR), so this can't
    reuse the CLI-based delete_model() path above. Re-derives and verifies the path the same
    way _local_report() does before ever deleting anything: the resolved directory must
    actually sit inside _CONVERSION_DIR (not escape it via '..') and must actually exist -
    same fail-closed posture as lmstudio_client.delete_model()."""
    name = tag[len("local:"):]
    directory = (_CONVERSION_DIR / name).resolve()
    if not name or not directory.is_relative_to(_CONVERSION_DIR) or not directory.is_dir():
        raise FastFlowLMError(f"Converted model '{tag}' was not found.")
    if directory == _CONVERSION_DIR:
        raise FastFlowLMError(
            f"'{tag}' resolved to the conversions root itself - refusing to delete "
            "(would remove every local conversion)."
        )
    if _read_active() == flm_backend_profile.RUNTIME_TAG:
        try:
            unload_model()
        except FastFlowLMError:
            pass
    shutil.rmtree(directory)


def check_servable(model_id: str) -> str | None:
    return None
