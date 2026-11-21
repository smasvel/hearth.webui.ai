"""Direct client for upstream llama.cpp's OpenAI-compatible llama-server."""
from __future__ import annotations

import configparser
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

import requests

from hearth.storage import settings_store


_PREFIX = "llamacpp/"
MANAGEMENT_HINT = "Model and context are set by the local llama-server."


class LlamaCppError(RuntimeError):
    pass


def base_url() -> str:
    return normalize_url(settings_store.get("llamacpp_base_url") or os.environ.get("LLAMACPP_BASE_URL", ""))


def normalize_url(url: str) -> str:
    return url.strip().rstrip("/").removesuffix("/v1")


def owns_model(model_id: str | None) -> bool:
    return isinstance(model_id, str) and model_id.startswith(_PREFIX)


def api_key() -> str | None:
    return settings_store.get("llamacpp_api_key") or os.environ.get("LLAMACPP_API_KEY")


def _auth_headers() -> dict:
    key = api_key()
    return {"Authorization": f"Bearer {key}"} if key else {}


def managed_here() -> bool:
    # The public backend setting is migrated out of the environment at startup.
    configured = normalize_url(os.environ.get("HEARTH_LLAMACPP_MANAGED_URL", "http://127.0.0.1:8093"))
    return bool(configured and configured == base_url() and os.environ.get("HEARTH_LLAMACPP_CONTAINER"))


def list_models() -> list[dict]:
    """Return the running server's catalog, or an empty list if it is offline."""
    base = base_url()
    if not base:
        return []
    try:
        response = requests.get(f"{base}/v1/models", headers=_auth_headers(), timeout=2)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        return []
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return []

    context_editable = managed_here() and bool(os.environ.get("HEARTH_LLAMACPP_PRESETS"))
    models = []
    for row in payload["data"]:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            continue
        status = row.get("status")
        managed = isinstance(status, dict) and isinstance(status.get("value"), str)
        runtime_state = status["value"] if managed else "loaded"
        if runtime_state in {"incomplete", "downloading"}:
            continue
        meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
        model = {
            "id": _PREFIX + row["id"],
            "name": row["id"],
            "backend": "llamacpp",
            "type": "llm",
            "state": "loaded" if runtime_state == "loaded" else "not-loaded",
            "externally_managed": not managed,
            "deletable": managed and (row.get("can_remove") is True or "cached" in (row.get("tags") or [])),
            "context_editable": managed and context_editable,
            "management_hint": MANAGEMENT_HINT,

        }
        for source, target in (("n_ctx", "loaded_context_length"), ("n_ctx_train", "max_context_length")):
            if type(meta.get(source)) is int and meta[source] > 0:
                model[target] = meta[source]
        args = (status.get("args") or []) if managed else []
        for flag, value in zip(args, args[1:]):
            if flag == "--ctx-size" and str(value).isdigit() and int(value) > 0:
                model["configured_context_length"] = int(value)
                break
        memory = row.get("memory") or {}
        if runtime_state == "loaded" and "loaded_context_length" not in model:
            effective = (memory.get("effective") or {}).get("n_ctx")
            if type(effective) is int and effective > 0:
                model["loaded_context_length"] = effective
        if type(meta.get("size")) is int and meta["size"] > 0:
            model["size_gb"] = meta["size"] / (1024 ** 3)
        models.append(model)
    return models


def chat_stream(messages: list[dict], model: str, temperature: float = 0.7,
                max_tokens: int | None = None):
    base = base_url()
    if not base:
        raise LlamaCppError("The llama.cpp endpoint is disabled.")
    payload = {
        "model": model.removeprefix(_PREFIX),
        "messages": messages,
        "temperature": temperature,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    try:
        with requests.post(f"{base}/v1/chat/completions", json=payload,
                           headers=_auth_headers(), stream=True, timeout=(8, 600)) as response:
            if response.status_code >= 400:
                try:
                    data = response.json()
                    error = data.get("error") if isinstance(data, dict) else None
                    detail = error.get("message") if isinstance(error, dict) else None
                except ValueError:
                    detail = None
                raise LlamaCppError(detail or f"llama-server returned HTTP {response.status_code}.")
            for line in response.iter_lines(chunk_size=1):
                if line:
                    yield line.decode("utf-8", errors="replace")
    except requests.RequestException as exc:
        raise LlamaCppError(f"Could not reach llama-server: {exc}") from exc


def request_model_id(model_id: str) -> str:
    return model_id.removeprefix(_PREFIX)


def _router_request(method, path, **kwargs):
    base = base_url()
    if not base:
        raise LlamaCppError("Configure the llama.cpp endpoint in Settings.")
    try:
        response = requests.request(method, base + path, headers=_auth_headers(),
                                    timeout=kwargs.pop("timeout", 15), **kwargs)
        data = response.json()
        if response.ok and isinstance(data, dict):
            return data
        detail = (data.get("error") or {}).get("message") if isinstance(data, dict) else None
        raise LlamaCppError(detail or f"llama-server returned HTTP {response.status_code}.")
    except (requests.RequestException, ValueError) as exc:
        raise LlamaCppError(f"llama-server router request failed: {exc}") from exc


def _router_model(model_id, *, refresh=False):
    params = {"reload": 1} if refresh else None
    data = _router_request("GET", "/models", params=params)
    return next((model for model in data.get("data", []) if model.get("id") == model_id), None)


def _write_context_preset(model_id, context_length):
    filename = os.environ.get("HEARTH_LLAMACPP_PRESETS")
    if not filename or not managed_here():
        raise LlamaCppError("This server's context is configured in llama-server's launch options.")
    if not re.fullmatch(r"[A-Za-z0-9._:/-]+", model_id):
        raise LlamaCppError("This model name cannot be used in a context preset.")
    path = Path(filename)
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        parser.read(path, encoding="utf-8")
        if not parser.has_section(model_id):
            parser.add_section(model_id)
        parser.set(model_id, "ctx-size", str(context_length))
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            parser.write(handle)
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    except (OSError, configparser.Error) as exc:
        raise LlamaCppError(f"Could not update the local llama-server context preset: {exc}") from exc


def load_model(model_id, context_length=None):
    served = request_model_id(model_id)
    row = _router_model(served)
    if row is None:
        raise LlamaCppError("The requested model is missing from llama-server.")
    status = row.get("status") or {}
    if not status:
        if context_length:
            raise LlamaCppError(MANAGEMENT_HINT)
        return
    if context_length:
        _write_context_preset(served, context_length)
        if status.get("value") in {"loaded", "loading", "sleeping"}:
            unload_model(model_id)
        _router_model(served, refresh=True)
    elif status.get("value") == "loaded":
        return
    _router_request("POST", "/models/load", json={"model": served}, timeout=120)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        row = _router_model(served)
        status = (row or {}).get("status") or {}
        if status.get("value") == "loaded":
            return
        if status.get("failed"):
            raise LlamaCppError("llama-server could not load the model. Check its service logs.")
        time.sleep(0.4)
    raise LlamaCppError("Timed out waiting for the llama.cpp model to load.")


def unload_model(model_id):
    served = request_model_id(model_id)
    _router_request("POST", "/models/unload", json={"model": served}, timeout=90)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        row = _router_model(served)
        if not row or (row.get("status") or {}).get("value") in {"unloaded", "incomplete"}:
            return
        time.sleep(0.4)
    raise LlamaCppError("Timed out waiting for llama.cpp to unload the model.")


def delete_model(model_id):
    _router_request("DELETE", "/models", params={"model": request_model_id(model_id)}, timeout=60)


def pause_download(model_id):
    row = _router_model(request_model_id(model_id))
    if row and (row.get("status") or {}).get("value") == "downloading":
        unload_model(model_id)


def _local_download_bytes(model_id):
    """Read the managed router's partial-file manifest when it omits API progress."""
    presets = os.environ.get("HEARTH_LLAMACPP_PRESETS")
    if not presets or not managed_here():
        return 0
    cache = (Path(presets).parent / "cache").resolve()
    try:
        manifest = json.loads((cache / "unfinished-downloads.json").read_text())
    except (OSError, ValueError):
        return 0
    paths = set()
    for download in manifest.get("downloads", []):
        if download.get("model") != model_id:
            continue
        for part in download.get("partials", []):
            path = Path(part.get("path", ""))
            if path.is_relative_to("/data/cache") and path.name.endswith(".downloadInProgress"):
                paths.add(cache / path.relative_to("/data/cache"))
    downloaded = 0
    for partial in paths:
        for path in (partial, partial.with_suffix("")):
            try:
                if path.resolve().is_relative_to(cache) and path.is_file():
                    downloaded += path.stat().st_size
                    break
            except OSError:
                continue
    return downloaded


def _download_progress(model_id, status):
    parts = [part for part in (status.get("progress") or {}).values() if isinstance(part, dict)]
    downloaded = sum(part.get("done") or 0 for part in parts)
    total = sum(part.get("total") or 0 for part in parts)
    return {
        "bytes_downloaded": downloaded if parts else _local_download_bytes(model_id),
        "bytes_total": total,
        "percent": min(99, int(downloaded * 100 / total)) if total else None,
        "file": model_id,
    }


def active_downloads():
    if not base_url():
        return []
    try:
        rows = _router_request("GET", "/models", timeout=2).get("data", [])
    except LlamaCppError:
        return []
    return [
        {"backend": "llamacpp", "model_id": row["id"], "kind": "structured",
         "source": "huggingface", "progress": _download_progress(row["id"], row["status"])}
        for row in rows if (row.get("status") or {}).get("value") == "downloading" and row.get("id")
    ]


def download_model(model_id, cancelled=None):
    served = request_model_id(model_id)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)?", served):
        raise LlamaCppError("Enter a Hugging Face GGUF repository with an optional quantization tag.")
    _router_request("POST", "/models", json={"model": served}, timeout=30)
    started = time.monotonic()
    while time.monotonic() - started < 7200:
        row = _router_model(served)
        status = (row or {}).get("status") or {}
        state = status.get("value")
        if cancelled and cancelled():
            if state == "downloading":
                unload_model(model_id)
            return
        if state in {"unloaded", "loaded", "sleeping"} and not status.get("failed"):
            return
        if state == "incomplete" or status.get("failed") or (row is None and time.monotonic() - started > 15):
            raise LlamaCppError("llama-server did not complete the download. You can retry it from Downloads.")
        yield _download_progress(served, status)
        time.sleep(0.6)
    raise LlamaCppError("Timed out waiting for the llama.cpp download.")


def health(url=None, key=None):
    base = normalize_url(url) if url else base_url()
    if not base:
        return False, "Configure the llama.cpp endpoint."
    headers = {"Authorization": f"Bearer {key}"} if key else _auth_headers()
    try:
        response = requests.get(f"{base}/v1/models", headers=headers, timeout=3)
        return (True, "llama-server is reachable.") if response.ok else (False, f"llama-server returned HTTP {response.status_code}.")
    except requests.RequestException:
        return False, "Could not reach llama-server."


def restart_service():
    if not managed_here():
        raise LlamaCppError("This llama-server is managed outside Hearth.")
    loaded = [model["id"] for model in list_models() if model["state"] == "loaded"
              and not model["externally_managed"]]
    try:
        subprocess.run(["docker", "restart", os.environ["HEARTH_LLAMACPP_CONTAINER"]],
                       check=True, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LlamaCppError(f"Could not restart the local llama-server container: {exc}") from exc
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if health()[0]:
            for model_id in loaded:
                load_model(model_id)
            return
        time.sleep(0.5)
    raise LlamaCppError("llama-server restarted but did not become ready.")
