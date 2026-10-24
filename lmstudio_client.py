"""Thin client around LM Studio's local REST API and the `lms` CLI.

Chat/model-listing goes through LM Studio's HTTP server (OpenAI-compatible
`/v1` routes plus the richer `/api/v0` routes). Loading/unloading models is
not exposed over HTTP, so we shell out to the `lms` CLI for that instead.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import requests
import model_test_profiles

import settings_store

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\].*?(?:\x07|\x1b\\)")

_DEFAULT_BASE_URL = os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234").rstrip("/")
_DEFAULT_API_KEY = os.environ.get("LMSTUDIO_API_KEY")
LMS_CLI = shutil.which("lms") or os.path.expanduser("~/.lmstudio/bin/lms")
EMBEDDING_MODEL_ID = os.environ.get("LMSTUDIO_EMBEDDING_MODEL_ID", "text-embedding-nomic-embed-text-v1.5")
LMSTUDIO_MODELS_DIR = os.path.realpath(os.path.expanduser("~/.lmstudio/models"))


def base_url() -> str:
    """Settings-panel override takes priority over .env, checked fresh on every call
    (not cached at import time) so a change in the UI applies immediately, no restart."""
    return (settings_store.get("lmstudio_base_url") or _DEFAULT_BASE_URL).rstrip("/")


def api_key() -> str | None:
    return settings_store.get("lmstudio_api_key") or _DEFAULT_API_KEY


def _auth_headers() -> dict:
    key = api_key()
    return {"Authorization": f"Bearer {key}"} if key else {}


class LMStudioError(RuntimeError):
    pass


def _run_lms(args: list[str], timeout: int = 120) -> str:
    if not (os.path.isfile(LMS_CLI) and os.access(LMS_CLI, os.X_OK)):
        raise LMStudioError(
            "The 'lms' CLI was not found. Install LM Studio and make sure "
            "'lms' is on PATH (or reachable at ~/.lmstudio/bin/lms)."
        )
    try:
        result = subprocess.run(
            [LMS_CLI, *args], capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise LMStudioError(f"'lms {' '.join(args)}' timed out") from exc
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "lms command failed").strip()
        raise LMStudioError(message)
    return result.stdout


def list_models() -> list[dict]:
    """All models known to LM Studio (on disk), with load state."""
    try:
        resp = requests.get(
            f"{base_url()}/api/v0/models", headers=_auth_headers(), timeout=10
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise LMStudioError(
            f"Could not reach LM Studio server at {base_url()}: {exc}"
        ) from exc
    models = resp.json().get("data", [])
    for model in models:
        instances = model.get("loaded_instances") or []
        loaded_contexts = [
            config.get("context_length")
            for instance in instances
            if isinstance((config := instance.get("config")), dict)
            and isinstance(config.get("context_length"), int)
            and config["context_length"] > 0
        ]
        if loaded_contexts:
            model["loaded_context_length"] = max(loaded_contexts)
    return models


def owns_model(model_id: str) -> bool:
    """Check the installed catalog before restoring a saved default."""
    try:
        return any(m.get("id") == model_id for m in list_models())
    except LMStudioError:
        return False


def default_gpu_offload(model_key: str) -> str:
    """Reserve runtime headroom when weights approach an AMD UMA GPU's budget."""
    try:
        devices = list(Path('/sys/class/drm').glob('card[0-9]*/device/mem_info_gtt_total'))
        if len(devices) != 1:
            return 'max'
        device = devices[0].parent
        gtt = int(devices[0].read_text())
        vram = int((device / 'mem_info_vram_total').read_text())
        # Only adjust integrated/shared-memory GPUs, not discrete GPU placement.
        if not 0 < vram < gtt:
            return 'max'
        catalog = json.loads(_run_lms(['ls', '--json'], timeout=15))
        model = next((m for m in catalog if m.get('modelKey') == model_key), {})
        size = model.get('sizeBytes')
        budget = (gtt + vram) * 0.75
        if isinstance(size, (int, float)) and size > budget > 0:
            # Round down so the chosen ratio cannot exceed the weight budget.
            ratio = int(100 * budget / size) / 100
            return str(ratio) if ratio > 0 else 'off'
    except (OSError, ValueError, TypeError, LMStudioError):
        pass
    return 'max'


def load_model(
    model_key: str,
    identifier: str | None = None,
    context_length: int | None = None,
    ttl: int | None = None,
    gpu: str | None = None,
) -> None:
    args = ["load", model_key, "-y"]
    if identifier:
        args += ["--identifier", identifier]
    if context_length:
        args += ["-c", str(context_length)]
    if ttl:
        args += ["--ttl", str(ttl)]
    args += ["--gpu", str(gpu) if gpu is not None else default_gpu_offload(model_key)]
    _run_lms(args, timeout=900)


def unload_model(identifier: str) -> None:
    _run_lms(["unload", identifier], timeout=60)


def delete_model(identifier: str) -> None:
    """Deletes a downloaded LM Studio model's actual files on disk.

    `lms` has no delete/rm/uninstall subcommand, and this used to refuse outright: `lms
    ls --json`'s own reported `path` field was confirmed live to sometimes NOT match
    reality (for 'qwen/qwen3.6-35b-a3b' it reported path "qwen/qwen3.6-35b-a3b", which
    doesn't exist on disk, while the real download sits at
    ~/.lmstudio/models/lmstudio-community/Qwen3.6-35B-A3B-GGUF/). That risk doesn't go
    away here - it's handled by verification instead of by refusing: this re-derives the
    path from a fresh `lms ls --json`, resolves it against LMSTUDIO_MODELS_DIR, and
    requires the resulting file to actually exist on disk before deleting anything. A
    path that fails to resolve inside LMSTUDIO_MODELS_DIR, doesn't exist, or sits
    directly in the models root with no containing folder (would mean rmtree-ing the
    entire LM Studio library) raises instead of guessing further - same fail-closed
    posture as before, just narrower.

    Deletes the model's whole containing folder, not just the single file `path` names:
    confirmed live (2026-08-23, manually, before this function existed) that a real
    download's folder often holds companion files a single-file delete would silently
    orphan - `mmproj-*.gguf` for vision models, `config.json` - both present for the
    Cold-Fusion/Fable-Fusion models removed that day."""
    try:
        raw = _run_lms(["ls", "--json"], timeout=15)
        catalog = json.loads(raw)
    except (LMStudioError, json.JSONDecodeError) as exc:
        raise LMStudioError(f"Could not list LM Studio models to resolve a delete path: {exc}") from exc

    entry = next((m for m in catalog if m.get("modelKey") == identifier), None)
    if entry is None:
        raise LMStudioError(f"'{identifier}' is not a known LM Studio model.")

    relative_path = entry.get("path")
    if not relative_path:
        raise LMStudioError(f"LM Studio reported no file path for '{identifier}' — refusing to guess.")

    resolved = os.path.realpath(os.path.join(LMSTUDIO_MODELS_DIR, relative_path))
    folder = os.path.dirname(resolved)
    if os.path.commonpath([resolved, LMSTUDIO_MODELS_DIR]) != LMSTUDIO_MODELS_DIR:
        raise LMStudioError(
            f"Resolved path for '{identifier}' ({resolved}) is outside the LM Studio "
            "models directory — refusing to delete."
        )
    if folder == LMSTUDIO_MODELS_DIR:
        raise LMStudioError(
            f"'{identifier}' resolved directly to the LM Studio models root with no "
            "containing folder — refusing to delete (would remove the entire library)."
        )
    if not os.path.isfile(resolved):
        raise LMStudioError(
            f"Could not verify '{identifier}' on disk (expected {resolved}, not found) — "
            "refusing to delete rather than guess. Delete it from the LM Studio app's "
            "My Models tab instead."
        )

    try:
        unload_model(identifier)
    except LMStudioError:
        pass  # not loaded, or already unloaded — deleting an unloaded model's files is fine either way

    shutil.rmtree(folder)


def search_and_download(query: str):
    """Search LM Studio's catalog for `query` and download the best match.

    `lms get` renders an animated terminal UI (spinners, carriage-return
    redraws), so we strip ANSI escapes and collapse consecutive duplicate
    lines into a readable progress log instead of trying to replicate the
    TUI. `-y` auto-approves any staff-pick / variant selection prompts so
    this never blocks waiting for interactive input.
    """
    if not (os.path.isfile(LMS_CLI) and os.access(LMS_CLI, os.X_OK)):
        raise LMStudioError(
            "The 'lms' CLI was not found. Install LM Studio and make sure "
            "'lms' is on PATH (or reachable at ~/.lmstudio/bin/lms)."
        )

    # Search cards carry exact Hugging Face repo ids (owner/repo). Passing those as a
    # plain name makes `lms get` run its fuzzy LM Studio catalog resolver, which can pick
    # a different result or remain in its animated selection UI indefinitely. LM Studio's
    # own CLI help explicitly requires a *full URL* for direct Hugging Face downloads.
    # Convert exact repo ids here; free-form names without a slash keep fuzzy resolution.
    target = query.strip()
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", target):
        target = f"https://huggingface.co/{target}"

    proc = subprocess.Popen(
        [LMS_CLI, "get", target, "--gguf", "-y"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    buffer = ""
    last_line = None
    try:
        while True:
            chunk = proc.stdout.read(256)
            if not chunk:
                break
            buffer += chunk
            *segments, buffer = re.split(r"[\r\n]+", buffer)
            for segment in segments:
                clean = _ANSI_RE.sub("", segment).strip()
                if clean and clean != last_line:
                    last_line = clean
                    yield clean
        clean = _ANSI_RE.sub("", buffer).strip()
        if clean and clean != last_line:
            yield clean
    finally:
        proc.wait()

    if proc.returncode != 0:
        raise LMStudioError(last_line or f"Download failed (exit code {proc.returncode})")


def embed(texts: list[str]) -> list[list[float]]:
    """Embeddings via LM Studio's OpenAI-compatible /v1/embeddings, for the cross-chat
    memory feature. Independent of whichever chat model is currently loaded — the
    embedding model is exempt from this app's usual "one model at a time" policy."""
    try:
        resp = requests.post(
            f"{base_url()}/v1/embeddings",
            json={"model": EMBEDDING_MODEL_ID, "input": texts},
            headers=_auth_headers(),
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise LMStudioError(f"Embedding request failed: {exc}") from exc
    data = sorted(resp.json()["data"], key=lambda d: d["index"])
    return [d["embedding"] for d in data]


def chat_stream(
    messages: list[dict],
    model: str,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    frequency_penalty: float | None = 0.3,
):
    """Yield raw SSE 'data: ...' lines proxied from LM Studio's chat endpoint.

    frequency_penalty defaults on (mild, 0.3) — see lemonade_client.chat_stream()'s
    docstring for why (a real report of repetition-loop degeneration surfaced that
    neither backend was ever sent any repetition guard at all). Pass None to disable."""
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
            json=model_test_profiles.apply(payload, "lmstudio"),
            headers=_auth_headers(),
            stream=True,
            timeout=(10, 600),
        ) as resp:
            resp.raise_for_status()
            # Requests defaults to 512-byte chunks. Tiny SSE token events then sit
            # buffered until enough bytes accumulate, making Hearth look frozen.
            for line in resp.iter_lines(chunk_size=1):
                if line:
                    yield line.decode("utf-8", errors="replace")
    except requests.RequestException as exc:
        raise LMStudioError(f"Chat request failed: {exc}") from exc
