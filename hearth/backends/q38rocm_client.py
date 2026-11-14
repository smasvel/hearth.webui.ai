"""Hearth integration for the locally managed q38rocm OpenAI-compatible server."""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

import requests

from hearth.backends import backend_broker_client
from hearth.storage import settings_store

SERVICE_NAME = "q38rocm.service"
_DEFAULT_BASE_URL = "http://127.0.0.1:8008"
_MODEL_ID = "q38rocm/Qwen3.8-27B-Q3_K_M"
_MODEL_NAME = "Qwen3.8-27B-Q3_K_M"
_CATALOG_ID = "julianmb-Qwen-3.8-27B-ROCmFP4-FAST-GGUF"
_HF_REPO = "julianmb/Qwen-3.8-27B-ROCmFP4-FAST-GGUF"
_HF_FILE = "Qwen3.8-27B-Q3_K_M.gguf"
_HF_URL = f"https://huggingface.co/{_HF_REPO}/resolve/main/{_HF_FILE}"
_MODEL_PATH = Path(os.environ.get(
    "Q38ROCM_MODEL_PATH",
    Path(__file__).resolve().parents[2] / "data/q38rocm/models" / _HF_FILE,
))
_CONTEXT = 131072


def configured_context() -> int:
    """Prefer an admin-saved ctx-size, then the installed service window."""
    try:
        from hearth.storage import model_metadata_store
        saved = model_metadata_store.get_model_configured_context("q38rocm", _MODEL_ID)
        if isinstance(saved, int) and not isinstance(saved, bool) and saved > 0:
            return saved
    except Exception:
        pass
    unit = Path.home() / ".config/systemd/user/q38rocm.service"
    try:
        match = re.search(r"(?m)^Environment=CTX=(\d+)$", unit.read_text())
    except OSError:
        match = None
    return int(match.group(1)) if match else _CONTEXT


class Q38RocmError(RuntimeError):
    pass


def search_catalog(query: str = "") -> list[dict]:
    """Expose the one GGUF variant this local Vulkan build is configured to serve."""
    haystack = f"{_CATALOG_ID} {_HF_REPO} {_HF_FILE} qwen 3.8 27b gguf q3 k m rocm fp4 fast".lower()
    terms = re.findall(r"[a-z0-9]+", (query or "").lower())
    if terms and not all(term in haystack for term in terms):
        return []
    size_gb = (_MODEL_PATH.stat().st_size / 1_000_000_000
               if _MODEL_PATH.is_file() else 13.49)
    return [{
        # Use the real Hugging Face repo ID as the visible/searchable result name.
        # A synthetic slug made the catalog entry look unrelated to its source.
        "id": _HF_REPO,
        "checkpoint": f"{_HF_REPO}:{_HF_FILE}",
        "filename": _HF_FILE,
        "repo_id": _HF_REPO,
        "size_gb": round(size_gb, 2),
        "labels": ["chat", "tool-calling", "gguf", "q3_k_m"],
        "parameter_size": "27B",
        "max_context_length": 262144,
        "supports_tool_calling": True,
        "format": "GGUF (Q3_K_M)",
        "description": "Downloads the Q3_K_M GGUF variant supported by this desktop's RADV Vulkan q38rocm build.",
        "downloaded": _MODEL_PATH.is_file(),
        "suggested": True,
        "source": "huggingface",
    }]


def _cached_model_file() -> Path | None:
    repo_folder = "models--" + _HF_REPO.replace("/", "--")
    roots = (
        Path.home() / ".cache/huggingface/hub",
        Path("/var/lib/lemonade/.cache/huggingface/hub"),
    )
    for root in roots:
        snapshots = root / repo_folder / "snapshots"
        if snapshots.is_dir():
            matches = sorted(snapshots.glob(f"*/{_HF_FILE}"), key=lambda path: path.stat().st_mtime, reverse=True)
            if matches:
                return matches[0]
    return None


def _copy_cached_model(source: Path, part_path: Path, cancelled=None):
    total_bytes = source.stat().st_size
    downloaded = 0
    with source.open("rb") as src, part_path.open("wb") as output:
        while True:
            chunk = src.read(4 * 1024 * 1024)
            if not chunk:
                break
            if cancelled and cancelled():
                part_path.unlink(missing_ok=True)
                return
            output.write(chunk)
            downloaded += len(chunk)
            yield {"file": _HF_FILE, "percent": round(downloaded * 100 / total_bytes, 1),
                   "bytes_downloaded": downloaded, "bytes_total": total_bytes,
                   "detail": "Copying the existing Hugging Face cache file"}
        output.flush()
        os.fsync(output.fileno())
    with part_path.open("rb") as model_file:
        if model_file.read(4) != b"GGUF":
            part_path.unlink(missing_ok=True)
            raise Q38RocmError("The cached file does not have a valid GGUF header.")
    yield {"file": _HF_FILE, "percent": 100, "bytes_downloaded": total_bytes,
           "bytes_total": total_bytes, "detail": "Copied the existing Hugging Face cache file"}


def download_model(model_id: str, cancelled=None):
    """Stream the supported GGUF from Hugging Face into q38rocm's model directory."""
    if model_id not in (_CATALOG_ID, _HF_REPO, f"{_HF_REPO}:{_HF_FILE}"):
        raise Q38RocmError(f"Unsupported q38rocm download: {model_id}")
    _MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _MODEL_PATH.is_file():
        with _MODEL_PATH.open("rb") as model_file:
            if model_file.read(4) != b"GGUF":
                raise Q38RocmError(f"Existing model file is not a valid GGUF: {_MODEL_PATH}")
        yield {"file": _HF_FILE, "percent": 100, "bytes_downloaded": _MODEL_PATH.stat().st_size,
               "bytes_total": _MODEL_PATH.stat().st_size}
        return

    part_path = Path(str(_MODEL_PATH) + ".part")
    etag_path = Path(str(part_path) + ".etag")
    cached_source = _cached_model_file()
    if cached_source:
        for progress in _copy_cached_model(cached_source, part_path, cancelled=cancelled):
            if cancelled and cancelled():
                part_path.unlink(missing_ok=True)
                etag_path.unlink(missing_ok=True)
                return
            yield progress
        if part_path.is_file():
            os.replace(part_path, _MODEL_PATH)
        etag_path.unlink(missing_ok=True)
        return

    try:
        head = requests.head(_HF_URL, allow_redirects=True, timeout=(10, 60))
        head.raise_for_status()
    except requests.RequestException as exc:
        raise Q38RocmError(f"Could not inspect Hugging Face file {_HF_REPO}:{_HF_FILE}: {exc}") from exc
    remote_etag = head.headers.get("ETag", "").strip('"')
    try:
        total_bytes = int(head.headers.get("Content-Length", "0")) or None
    except ValueError:
        total_bytes = None
    old_etag = etag_path.read_text().strip() if etag_path.exists() else ""
    offset = (part_path.stat().st_size
              if remote_etag and part_path.is_file() and old_etag == remote_etag else 0)
    if offset and total_bytes and offset >= total_bytes:
        part_path.unlink(missing_ok=True)
        offset = 0
    if offset == 0:
        part_path.unlink(missing_ok=True)
        etag_path.write_text(remote_etag)

    headers = {"Range": f"bytes={offset}-"} if offset else {}
    try:
        response = requests.get(_HF_URL, headers=headers, stream=True, allow_redirects=True,
                                timeout=(10, 120))
        response.raise_for_status()
    except requests.RequestException as exc:
        raise Q38RocmError(f"Could not download {_HF_REPO}:{_HF_FILE}: {exc}") from exc

    with response:
        append = offset > 0 and response.status_code == 206
        if offset and not append:
            offset = 0
        content_range = response.headers.get("Content-Range", "")
        range_match = re.search(r"/([0-9]+)$", content_range)
        if range_match:
            total_bytes = int(range_match.group(1))
        elif not total_bytes:
            try:
                response_bytes = int(response.headers.get("Content-Length", "0")) or None
            except ValueError:
                response_bytes = None
            total_bytes = offset + response_bytes if response_bytes else None
        mode = "ab" if append else "wb"
        downloaded = offset
        try:
            with part_path.open(mode) as output:
                for chunk in response.iter_content(chunk_size=4 * 1024 * 1024):
                    if not chunk:
                        continue
                    if cancelled and cancelled():
                        part_path.unlink(missing_ok=True)
                        etag_path.unlink(missing_ok=True)
                        return
                    output.write(chunk)
                    downloaded += len(chunk)
                    percent = min(99.9, downloaded * 100 / total_bytes) if total_bytes else 0
                    yield {"file": _HF_FILE, "percent": round(percent, 1),
                           "bytes_downloaded": downloaded, "bytes_total": total_bytes}
                output.flush()
                os.fsync(output.fileno())
        except OSError as exc:
            raise Q38RocmError(f"Could not save q38rocm model at {_MODEL_PATH}: {exc}") from exc

    final_size = part_path.stat().st_size
    if total_bytes and final_size != total_bytes:
        raise Q38RocmError(f"Incomplete download: received {final_size} of {total_bytes} bytes.")
    with part_path.open("rb") as model_file:
        if model_file.read(4) != b"GGUF":
            raise Q38RocmError("The downloaded file does not have a valid GGUF header.")
    os.replace(part_path, _MODEL_PATH)
    etag_path.unlink(missing_ok=True)
    yield {"file": _HF_FILE, "percent": 100, "bytes_downloaded": final_size,
           "bytes_total": total_bytes or final_size}


def base_url() -> str:
    return (settings_store.get("q38rocm_base_url") or _DEFAULT_BASE_URL).rstrip("/")


def api_key() -> str | None:
    return settings_store.get("q38rocm_api_key") or None


def _headers() -> dict[str, str]:
    key = api_key()
    return {"Authorization": f"Bearer {key}"} if key else {}


def _server_model() -> dict | None:
    try:
        response = requests.get(f"{base_url()}/v1/models", headers=_headers(), timeout=4)
        response.raise_for_status()
        entries = response.json().get("data", [])
        return entries[0] if entries else None
    except (requests.RequestException, ValueError, TypeError):
        return None


def request_model_id() -> str:
    served = _server_model()
    return str((served or {}).get("id") or _MODEL_NAME)


def list_models() -> list[dict]:
    """Expose the installed model even while stopped; mark it loaded only when API is up."""
    if not _MODEL_PATH.is_file():
        return []
    served = _server_model()
    context = (served or {}).get("meta", {}).get("n_ctx")
    if not isinstance(context, int) or isinstance(context, bool) or context <= 0:
        context = configured_context()
    return [{
        "id": _MODEL_ID,
        "type": "llm",
        "publisher": "julianmb",
        "state": "loaded" if served else "not-loaded",
        "size_gb": round(_MODEL_PATH.stat().st_size / 1e9, 2),
        "loaded_context_length": context if served else None,
        "configured_context_length": context,
        "max_context_length": 262144,
        "server_model_id": (served or {}).get("id", _MODEL_NAME),
        "compatibility_type": "gguf",
        "quantization": "Q3_K_M",
        "deletable": False,
        "lan_compatible": False,
    }]


def owns_model(model_id: str) -> bool:
    return model_id == _MODEL_ID


def _systemctl(*args: str, timeout: int = 120) -> str:
    try:
        brokered = backend_broker_client.systemctl_via_broker(
            list(args), SERVICE_NAME, timeout=timeout,
        )
        if brokered is not None:
            return brokered
        result = subprocess.run(
            ["systemctl", "--user", *args, SERVICE_NAME],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError, backend_broker_client.BackendBrokerError) as exc:
        raise Q38RocmError(f"Could not control {SERVICE_NAME}: {exc}") from exc
    if result.returncode and "is-active" not in args:
        raise Q38RocmError((result.stderr or result.stdout or "systemctl failed").strip())
    return result.stdout


def load_model(model_id: str) -> None:
    if not owns_model(model_id):
        raise Q38RocmError(f"Unknown q38rocm model: {model_id}")
    _systemctl("start", timeout=30)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if _server_model():
            return
        time.sleep(2)
    raise Q38RocmError("q38rocm did not become ready; inspect q38rocm.service logs.")


def unload_model(model_id: str | None = None) -> None:
    _systemctl("stop", timeout=60)


def prepare_request(body):
    """Adapt OpenAI requests to the installed q38rocm llama-server schema.

    Shared by chat and model tests; callers' messages and schemas stay unchanged.
    """
    payload = dict(body)
    template = dict(payload.get("chat_template_kwargs") or {})
    if "enable_thinking" in payload:
        thinking = payload.pop("enable_thinking")
        if not isinstance(thinking, bool):
            raise Q38RocmError("enable_thinking must be a boolean")
        template["enable_thinking"] = thinking
    if template:
        payload["chat_template_kwargs"] = template
    if payload.get("stream"):
        payload["stream_options"] = {**(payload.get("stream_options") or {}), "include_usage": True}
    choice = payload.get("tool_choice")
    if isinstance(choice, dict):
        function = choice.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        selected = [tool for tool in payload.get("tools") or []
                    if (tool.get("function") or {}).get("name") == name]
        if choice.get("type") != "function" or not name or len(selected) != 1:
            raise Q38RocmError("Named tool_choice must select one offered function")
        # This engine has no native named choice. Restricting the offered schema
        # enforces the name with its supported required mode.
        payload["tools"] = selected
        payload["tool_choice"] = "required"
    return payload


def chat_stream(messages, model, temperature=0.7, max_tokens=None, frequency_penalty=None,
                tools=None, **options):
    payload = {
        "model": request_model_id(),
        "messages": messages,
        "temperature": temperature,
        "stream": True,
        # Request the terminal usage chunk so Hearth can capture prompt/output
        # token counts in the Metrics page for this streaming backend.
        "stream_options": {"include_usage": True},
    }
    # Let the service choose sampling defaults unless the caller overrides them.
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if frequency_penalty is not None:
        payload["frequency_penalty"] = frequency_penalty
    if tools:
        payload["tools"] = tools
    # Keep the OpenAI-compatible controls Hearth supplies. In particular, dropping
    # these silently ignores Qwen's per-request reasoning setting and tool-choice
    # constraints, which can make tool turns spend their whole reply budget reasoning.
    for key in (
        "tool_choice", "parallel_tool_calls", "enable_thinking", "chat_template_kwargs",
        "response_format", "stop", "top_p", "presence_penalty", "seed",
        "reasoning_effort", "reasoning_budget_tokens", "repeat_penalty",
    ):
        if key in options and options[key] is not None:
            payload[key] = options[key]
    # Verified test repairs also apply to chat, without the test suite's default
    # reasoning-off setting. Both paths key recipes by the live served ID.
    from hearth.models import model_test_profiles
    payload = model_test_profiles.apply_recipe(
        payload, model_test_profiles.get('q38rocm', payload['model']))
    payload = prepare_request(payload)
    try:
        with requests.post(
            f"{base_url()}/v1/chat/completions", json=payload,
            headers=_headers(), stream=True, timeout=(10, 900),
        ) as response:
            if response.status_code >= 400:
                detail = (response.text or "").strip()
                raise Q38RocmError(
                    f"q38rocm returned HTTP {response.status_code}: {detail[:500]}"
                )
            for line in response.iter_lines(chunk_size=1):
                if line:
                    yield line.decode("utf-8", errors="replace")
    except requests.RequestException as exc:
        raise Q38RocmError(f"q38rocm chat request failed: {exc}") from exc
