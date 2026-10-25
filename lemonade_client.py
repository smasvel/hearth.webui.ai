"""Thin client around AMD's Lemonade Server (lemonade-sdk) — a second local LLM
backend alongside LM Studio, run the same way Colibri used to be: merged into the
same /api/models list, routed to per-request by model id.

Why this exists rather than just using LM Studio for everything: Lemonade adds
NPU-accelerated inference (FastFlowLM/XDNA2, ONNX Runtime GenAI "ryzenai-llm") on
top of the GGUF/llama.cpp path LM Studio already covers, using models LM Studio's
catalog doesn't carry. It's fully OpenAI-compatible for chat, and — unlike LM
Studio, which has no HTTP model-management API and needs the `lms` CLI for that —
Lemonade exposes model list/load/unload/pull over plain REST, so no CLI shelling
out is needed here at all.

Installed and verified end-to-end (see README) via
`sudo add-apt-repository ppa:lemonade-team/stable && sudo apt install lemonade-server`.
Every function below is still best-effort / fails soft exactly like colibri_client.py
used to, though — if the server isn't installed or running (or gets uninstalled later),
list_models() returns [] and this backend just doesn't appear, rather than breaking
the rest of the app.
"""

from __future__ import annotations

import concurrent.futures
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

import psutil
import requests
import model_test_profiles

import settings_store
import lemonade_templates

_DEFAULT_BASE_URL = "http://localhost:13305"
EMBEDDING_MODEL_ID = os.environ.get("LEMONADE_EMBEDDING_MODEL_ID", "nomic-embed-text-v1-GGUF")


def base_url() -> str:
    """Read the database setting on every call
    (not cached at import time) so a change in the UI applies immediately, no restart."""
    return (settings_store.get("lemonade_base_url") or _DEFAULT_BASE_URL).rstrip("/")


def api_key() -> str | None:
    return settings_store.get("lemonade_api_key")


def _auth_headers() -> dict:
    key = api_key()
    return {"Authorization": f"Bearer {key}"} if key else {}


class LemonadeError(RuntimeError):
    pass


def loaded_image_model() -> str | None:
    """Return the image-generation model currently loaded by Lemonade, if any."""
    try:
        resp = requests.get(f"{base_url()}/v1/health", headers=_auth_headers(), timeout=5)
        resp.raise_for_status()
    except requests.RequestException:
        return None
    for entry in resp.json().get("all_models_loaded", []):
        if entry.get("type") == "image" and entry.get("loaded"):
            return entry.get("model_name")
    return None


def generate_image(
    prompt: str,
    *,
    model: str,
    width: int = 512,
    height: int = 512,
    steps: int = 4,
) -> dict:
    """Generate an image with Lemonade's OpenAI-compatible image endpoint."""
    try:
        resp = requests.post(
            f"{base_url()}/v1/images/generations",
            headers=_auth_headers(),
            json={
                "model": model,
                "prompt": prompt,
                "size": f"{width}x{height}",
                "n": 1,
                "steps": steps,
                "response_format": "b64_json",
            },
            timeout=(10, 900),
        )
        resp.raise_for_status()
        item = (resp.json().get("data") or [{}])[0]
        encoded = item.get("b64_json")
        if encoded:
            image_bytes = base64.b64decode(encoded)
        elif item.get("url"):
            image_resp = requests.get(item["url"], timeout=60)
            image_resp.raise_for_status()
            image_bytes = image_resp.content
        else:
            raise LemonadeError("Image backend returned neither b64_json nor an image URL.")
    except (requests.RequestException, ValueError, TypeError) as exc:
        raise LemonadeError(f"Image generation with '{model}' failed: {exc}") from exc
    return {"image_bytes": image_bytes, "model": model}


def _infer_type_from_labels(labels: list[str] | None) -> str:
    """Maps a catalog entry's `labels` list to this app's model "type" convention -
    "embeddings"/"transcription"/"tts" all need to be excluded from the chat model
    picker (see api.py's /api/models and app.js's renderModelSelect()), same reasoning
    for each: none of them can serve a normal chat completion, picking one would just
    produce a broken response. Everything else defaults to "llm" (the original,
    pre-voice-feature behavior for every non-embedding model)."""
    labels = labels or []
    if "embeddings" in labels:
        return "embeddings"
    if "tts" in labels:
        return "tts"
    if "transcription" in labels or "realtime-transcription" in labels:
        return "transcription"
    if "image" in labels or "text-to-image" in labels or "image-to-image" in labels:
        return "image"
    return "llm"


def _device_label(loaded_entry: dict | None, recipe: str) -> str:
    """The Models panel's existing "quantization" column is repurposed here to show
    which compute device a model actually runs on (NPU vs GPU vs CPU) — more useful
    for this backend than a quant string, since *which chip it runs on* is the whole
    point of having Lemonade alongside LM Studio at all."""
    device = (loaded_entry or {}).get("device") or ""
    if "npu" in device:
        return "NPU + GPU" if "gpu" in device else "NPU"
    if "gpu" in device:
        return "GPU"
    if device == "cpu":
        return "CPU"
    return recipe or ""


_flm_footprint_cache: dict[str, float] | None = None


def _flm_footprint_map() -> dict[str, float]:
    """tag -> on-NPU size (GB) from FastFlowLM's own catalog, cached. Lemonade reports no
    `size` for flm/NPU entries, but FLM's `flm list` does (as `footprint`), and Lemonade's
    flm checkpoints are exactly those tags — so this recovers a real size for them. Empty
    (all sizes stay None) if FastFlowLM isn't available; never raises."""
    global _flm_footprint_cache
    if _flm_footprint_cache is None:
        try:
            import fastflowlm_client as _flm
            _flm_footprint_cache = {
                c["id"]: c["size_gb"] for c in _flm.search_catalog("") if c.get("size_gb") is not None
            }
        except Exception:
            _flm_footprint_cache = {}
    return _flm_footprint_cache


def _resolve_checkpoint_size_gb(checkpoint: str | None) -> float | None:
    """Best-effort real size (GB) for a downloaded model whose Lemonade catalog entry
    carries no `size` — chiefly flm/NPU models, so the Models page shows a size instead of
    hiding it. A bare FLM tag ('qwen3:0.6b') resolves from FastFlowLM's catalog footprint;
    an HF-repo checkpoint ('org/repo' or 'org/repo:file') is summed from its file tree
    (cached in _download_size_gb). None only if neither applies."""
    if not checkpoint:
        return None
    if "/" not in checkpoint.split(":", 1)[0]:
        return _flm_footprint_map().get(checkpoint)
    return _download_size_gb(checkpoint)


def _loaded_context_length(entry: dict | None) -> int | None:
    """The context a *loaded* Lemonade model is actually running with, straight from
    /v1/health — llama-server's own --ctx-size is echoed back as recipe_options.ctx_size
    (confirmed live; e.g. the embedding model reports ctx_size 8192 while its declared
    max_context_window is only 2048, so ctx_size is the real server limit we want).
    Falls back to the entry's declared max_context_window when ctx_size is missing. None
    when the model isn't loaded / Lemonade didn't report it — the caller then advertises a
    backend-level default instead of guessing per model."""
    if not entry:
        return None
    ctx = (entry.get("recipe_options") or {}).get("ctx_size")
    if not isinstance(ctx, int) or ctx <= 0:
        ctx = entry.get("max_context_window")
    return ctx if isinstance(ctx, int) and ctx > 0 else None


def list_models() -> list[dict]:
    """All NPU (FastFlowLM) models pulled through this app, with load state — reshaped
    to the same {id, state, publisher, quantization, type} shape the frontend already
    expects from LM Studio's /api/v0/models. Best-effort: Lemonade not being
    installed/running is normal, not an error.

    Plain `GET /v1/models` only lists Lemonade's own GGUF download registry — confirmed
    live that pulled `user.*`/flm custom models never appear there, loaded or not, which
    made an unloaded NPU model disappear from this app's Models panel entirely (real bug
    report) instead of showing "not loaded" the way LM Studio models do. The actual fix,
    found by strace-ing the `lemonade` CLI itself (its own `list` command hits this
    exact endpoint) rather than guessing: `GET /v1/models?show_all=true` returns
    Lemonade's *entire* catalog, custom models included, regardless of load state. Its
    per-entry `downloaded` field is unreliable for flm models though (reports `false`
    even for ones confirmed on disk via `du -sh`), so flm entries are always kept
    regardless of that flag — reliable because Lemonade ships no built-in flm catalog
    entries of its own (confirmed live: every non-custom entry is llamacpp/sd-cpp/etc),
    so *any* flm-recipe entry here is one this app's Ryzen AI download path pulled.

    Non-flm entries are kept when `downloaded` is true — added after a second real bug
    report ("not all downloaded models are visible"), traced to this loop unconditionally
    skipping every non-flm entry regardless of download state. That was fine while the
    only non-flm custom downloads were the embedding model (which stayed loaded most of
    the time, so the loaded-only safety net below usually covered it), but the Download
    Models page can now pull arbitrary llamacpp-recipe GGUF models (its own local catalog
    or straight from Hugging Face) that don't auto-load afterward — confirmed live two
    real downloaded models (`Qwen3-Coder-30B-A3B-Instruct-GGUF`, unloaded, and
    `nomic-embed-text-v1-GGUF`, unloaded at the time) were completely absent from this
    function's output despite `downloaded: true` in the raw catalog response. Unlike flm,
    `downloaded` *is* reliable for llamacpp entries — confirmed live across multiple real
    pulls/deletes that it flips correctly, which is also what the Download Models page's
    own search already relies on for its "downloaded" badge.
    """
    try:
        models_resp = requests.get(
            f"{base_url()}/v1/models", params={"show_all": "true"}, headers=_auth_headers(), timeout=5
        )
        models_resp.raise_for_status()
    except requests.RequestException:
        return []

    # Real incident (2026-07-26): /v1/health blocks while Lemonade is mid-generation (a
    # llama.cpp/flm process actively decoding tokens) - confirmed live, a 5s (then even an
    # 8s) timeout hit reliably while a real request was streaming, even though /v1/models
    # answered in under 40ms the whole time. This used to be one try/except around BOTH
    # calls, so a slow health check nuked the entire catalog - "Lemonade models are
    # missing" while Lemonade was in fact healthy and busy, not down. Split so a health
    # timeout only costs the loaded/not-loaded state (defaults to "not-loaded" below,
    # corrected again on the next successful poll), never the model list itself.
    loaded_by_id: dict = {}
    try:
        health_resp = requests.get(f"{base_url()}/v1/health", headers=_auth_headers(), timeout=5)
        health_resp.raise_for_status()
        loaded_by_id = {
            entry.get("model_name"): entry
            for entry in health_resp.json().get("all_models_loaded", [])
        }
    except requests.RequestException:
        pass

    models = []
    seen_ids = set()
    for m in models_resp.json().get("data", []):
        if m.get("recipe") != "flm" and not m.get("downloaded"):
            continue
        model_id = m.get("id")
        seen_ids.add(model_id)
        loaded_entry = loaded_by_id.get(model_id)
        checkpoint = m.get("checkpoint") or ""
        publisher = checkpoint.split("/")[0] if "/" in checkpoint else "lemonade"
        # The catalog entry itself has no "type" field (unlike /v1/health's loaded
        # entries) — but it does carry "labels", which is what this is inferred from.
        # Checked here, not hardcoded "llm", since downloaded-but-unloaded non-flm
        # entries (the fix just above — e.g. the embedding model when it isn't
        # currently loaded) now flow through this loop too, and mislabeling one "llm"
        # would silently defeat the chat-picker's non-"llm" filters. Originally only
        # checked for "embeddings" - confirmed live this missed Whisper-Tiny/kokoro-v1
        # entirely (both came back type "llm", so they showed up in the chat model
        # picker as if they were chat models) until "transcription"/"tts" labels were
        # added here too.
        entry_type = _infer_type_from_labels(m.get("labels"))
        # Lemonade's catalog already carries a "size" (GB) for downloaded GGUF entries —
        # surface it so the Models panel can show on-disk size. Absent/None for flm/NPU
        # entries (Lemonade doesn't report a size for those); the UI just omits it then.
        raw_size = m.get("size")
        models.append(
            {
                "id": model_id,
                "state": "loaded" if loaded_entry else "not-loaded",
                "publisher": publisher,
                "quantization": _device_label(loaded_entry, m.get("recipe") or ""),
                "type": entry_type,
                "max_context_length": m.get("max_context_window"),
                # Real server context while loaded (see _loaded_context_length) — consumed by
                # help_data's OpenCode config generation so it never advertises more context
                # than Lemonade is actually serving.
                "loaded_context_length": _loaded_context_length(loaded_entry),
                "size_gb": raw_size if isinstance(raw_size, (int, float)) and raw_size > 0 else None,
                "_checkpoint": checkpoint,  # internal: used only to recover a missing size below
            }
        )

    # Lemonade reports no `size` for flm/NPU entries (and occasionally others) — recover a
    # real size from FastFlowLM's footprint catalog / the HF file tree so the Models page
    # shows a size for every model instead of silently hiding it. Concurrent + cached, so
    # the first load pays once and later loads are instant; fail-soft (leaves None).
    _missing = [e for e in models if e.get("size_gb") is None and e.get("_checkpoint")]
    if _missing:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(_missing))) as pool:
            for entry, size in zip(
                _missing, pool.map(lambda e: _resolve_checkpoint_size_gb(e["_checkpoint"]), _missing)
            ):
                if size is not None:
                    entry["size_gb"] = size
    for e in models:
        e.pop("_checkpoint", None)

    # Safety net, not the primary path anymore: if something is loaded but somehow
    # missing from the show_all catalog (e.g. a race with a pull still registering),
    # don't let it vanish from the list either. Map Lemonade's own per-entry "type"
    # (health reports "embedding", singular) to this app's "embeddings" (plural)
    # convention, matching how LM Studio's API already labels its embedding model -
    # "transcription"/"tts" need no such remapping, health already reports those as-is.
    for model_id, entry in loaded_by_id.items():
        if model_id in seen_ids:
            continue
        raw_type = entry.get("type") or "llm"
        entry_type = "embeddings" if raw_type == "embedding" else raw_type
        models.append(
            {
                "id": model_id,
                "state": "loaded",
                "publisher": "lemonade",
                "quantization": _device_label(entry, entry.get("recipe") or ""),
                "type": entry_type,
                "loaded_context_length": _loaded_context_length(entry),
            }
        )
    return models


def owns_model(model_id: str) -> bool:
    """Best-effort membership check so app.py can route /api/chat and load/unload
    to the right backend by model id alone, without hardcoding names — unlike
    Colibri, which was exactly one fixed model id, Lemonade can hold many."""
    return any(m["id"] == model_id for m in list_models())


def is_model_loaded(model_id: str) -> bool:
    """Checks /v1/health directly rather than list_models() — that function filters to
    recipe == 'flm' only, so it never sees non-flm models like the llamacpp-recipe
    embedding model. Used to avoid redundantly reloading an already-loaded model (see
    load_model's docstring for why that matters, not just being wasteful)."""
    try:
        resp = requests.get(f"{base_url()}/v1/health", headers=_auth_headers(), timeout=5)
        resp.raise_for_status()
    except requests.RequestException:
        return False
    return any(
        entry.get("model_name") == model_id and entry.get("loaded")
        for entry in resp.json().get("all_models_loaded", [])
    )


def _get_checkpoint(model_id: str) -> str | None:
    """Raw checkpoint string ('org/repo:file' for llamacpp-recipe models, a bare tag
    like 'qwen3.6-moe:35b-a3b' for flm/NPU ones) for a model id, queried directly from
    Lemonade's full catalog. list_models() above reshapes entries to a simplified
    {id, state, publisher, quantization, type} dict and drops this field entirely -
    check_safe_to_load() below needs the raw checkpoint to look up a real file size."""
    try:
        resp = requests.get(
            f"{base_url()}/v1/models", params={"show_all": "true"}, headers=_auth_headers(), timeout=5
        )
        resp.raise_for_status()
    except requests.RequestException:
        return None
    for m in resp.json().get("data", []):
        if m.get("id") == model_id:
            return m.get("checkpoint")
    return None


def _hf_checkpoint_size_bytes(checkpoint: str | None, timeout: float = 10.0) -> int | None:
    """Looks up the real size of a Hugging Face repo checkpoint via its file-tree API,
    for check_safe_to_load() below. Only meaningful for llamacpp-recipe checkpoints
    ('org/repo:filename') - flm/NPU checkpoints are bare tags with no HF-repo shape at
    all (_hf_repo_id_from_checkpoint returns None for those), so this correctly returns
    None for them rather than guessing. Also None on any lookup failure - callers must
    treat "unknown" as "don't block the load", not "0 bytes".

    Tries an exact filename match first, then falls back to a substring match summed
    across every matching file - confirmed live this fallback is required, not
    optional: Lemonade itself stores a bare quant *token* as the checkpoint's file part
    for some entries (e.g. "antirez/glm-5.2-gguf:UD-IQ2_XXS", not the real filename
    "GLM-5.2-UD-IQ2_XXS_RoutedIQ2XXS_blk78Q2K.gguf"), resolving it via what its own logs
    call "quant-token fallback" - a substring match against the repo's actual files.
    Without mirroring that same fallback here, the exact-match lookup silently finds
    nothing and the safety check below would wrongly wave through a checkpoint it
    couldn't actually size. Summing (not just taking one match) also makes this correct
    for a huge model split across multiple part files, which a single quant token could
    plausibly match more than one of."""
    if not checkpoint or ":" not in checkpoint:
        return None
    repo_id = _hf_repo_id_from_checkpoint(checkpoint)
    if not repo_id:
        return None
    token = checkpoint.split(":", 1)[1].lower()
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
    gguf_files = [e for e in entries if e.get("type") == "file" and e.get("path", "").lower().endswith(".gguf")]

    for e in gguf_files:
        path = e.get("path", "")
        if path.lower() == token or path.lower().rsplit("/", 1)[-1] == token:
            return e.get("size")

    matches = [e for e in gguf_files if token in e.get("path", "").lower()]
    if matches:
        return sum(e.get("size") or 0 for e in matches)
    return None


_SAFE_LOAD_RAM_FRACTION = 0.8


def check_safe_to_load(model_id: str) -> str | None:
    """Pre-flight sanity check, run before ever asking Lemonade to load a model: refuses
    when the model's real file size vastly exceeds this machine's total RAM, instead of
    letting the load reach llama-server (and, as happened, the GPU driver) and crash
    there. Triggered by a real incident: a GLM-5.2 quant ("UD-IQ2_XXS", already
    downloaded, presumably via Lemonade's own native UI) turned out to be a 211GB single
    file on a 43.7GB-RAM machine - not just slow, categorically impossible to load.
    Memory-mapping doesn't rescue this (llama.cpp still needs real working-set RAM for
    inference, and it crashed rather than degrading gracefully), and Lemonade itself
    doesn't check this before attempting a load.

    Returns a human-readable refusal message if the load should be blocked, None if it
    looks safe - including whenever the size can't be determined at all (a lookup
    failure must never itself block a load that might be perfectly fine; false negatives
    here are the safe direction to fail in, false positives are not).

    Deliberately about total system RAM (80% of it), not the much smaller ~24GB GPU/GTT
    budget the Vulkan crash earlier the same day was about - that crash happened on a
    ~20GB model well inside this RAM-based limit, a different and less catastrophic
    failure mode (GPU-specific instability, not "cannot possibly fit in memory") that
    this check isn't trying to replace - see the MTP-vs-NPU benchmark section in the
    README for that judgment call instead. This is a floor against the worst case, not a
    complete stability guarantee."""
    checkpoint = _get_checkpoint(model_id)
    size_bytes = _hf_checkpoint_size_bytes(checkpoint)
    if not size_bytes:
        return None
    total_ram_bytes = psutil.virtual_memory().total
    if size_bytes <= total_ram_bytes * _SAFE_LOAD_RAM_FRACTION:
        return None
    size_gb = size_bytes / 1e9
    ram_gb = total_ram_bytes / 1e9
    return (
        f"Refusing to load '{model_id}': this model is {size_gb:.0f}GB, but this machine only has "
        f"{ram_gb:.1f}GB of RAM total. It cannot fit in memory regardless of backend (CPU, GPU, or "
        f"NPU) - attempting to load it would very likely crash Lemonade rather than fail cleanly "
        f"(this refusal exists because that's exactly what happened). Pick a smaller quantization "
        f"of this model, or a smaller model."
    )


def load_model(model_key: str, context_length: int | None = None, **_ignored) -> None:
    """Extra kwargs (identifier, ttl, gpu) accepted-and-ignored so app.py's existing
    LM-Studio-shaped call site can pass them through unchanged regardless of which
    backend actually owns the model.

    Not a no-op when the model is already loaded — confirmed live (real 400 chat-completion
    failure) that Lemonade logs "Options changed, reloading model" and does a full evict+reload
    even when nothing meaningfully changed, briefly freeing the port the reloaded model's backend
    process was using. A concurrent /v1/chat/completions request landing during that window can
    get misrouted to whatever backend now owns that freed port — in the reproduced case, a chat
    request meant for the loaded LLM was answered by the embedding model instead ("Model does not
    support chat completion", 400). Callers that only care about "is this loaded, at all" (like
    _ensure_embedding_model_loaded in app.py) should check is_model_loaded() first and skip
    calling this when it's already true, rather than calling it unconditionally."""
    payload: dict = {"model_name": model_key}
    payload.update(lemonade_templates.load_options(model_key))
    if context_length:
        payload["ctx_size"] = context_length
    try:
        resp = requests.post(
            f"{base_url()}/v1/load", json=payload, headers=_auth_headers(), timeout=300
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise LemonadeError(f"Failed to load '{model_key}' on Lemonade: {exc}") from exc


def unload_model(identifier: str) -> None:
    try:
        resp = requests.post(
            f"{base_url()}/v1/unload",
            json={"model_name": identifier},
            headers=_auth_headers(),
            timeout=60,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise LemonadeError(f"Failed to unload '{identifier}' on Lemonade: {exc}") from exc


def delete_model(identifier: str) -> None:
    """Remove a downloaded model from disk (confirmed live: `POST /v1/delete` works
    correctly even mid-download or while registered, unlike LM Studio which has no
    such endpoint or CLI command at all — see lmstudio_client.py's delete_model for
    why that one refuses instead of guessing at file paths)."""
    try:
        resp = requests.post(
            f"{base_url()}/v1/delete",
            json={"model_name": identifier},
            headers=_auth_headers(),
            timeout=60,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise LemonadeError(f"Failed to delete '{identifier}' on Lemonade: {exc}") from exc


def search_and_download(model_name: str):
    """Pull a FastFlowLM (NPU) model by its exact tag (e.g. 'qwen3:0.6b') — the reason
    to use this backend at all is NPU acceleration, and Lemonade's plain catalog pull
    (`POST /v1/pull {"model_name": ...}`) only knows GGUF names; feeding it anything
    else (confirmed live) fails with "the model name must include the `user`
    namespace". FLM models instead need Lemonade's custom-model registration shape —
    also confirmed live, since the CLI-only `--checkpoint`/`--recipe` flags documented
    for this aren't in the public REST docs: {"model_name": "user.<slug>",
    "checkpoint": <exact flm tag>, "recipe": "flm"}. Exact tags come from
    `sudo <lemonade cache>/bin/flm/npu/flm list` (see README) or the Help panel — NOT
    a Hugging Face repo id, which looks similar but isn't accepted here.
    Streams human-readable progress lines, same generator contract as
    lmstudio_client.search_and_download() so app.py's SSE wiring needs no changes."""
    slug = re.sub(r"[^A-Za-z0-9_.-]", "-", model_name)
    registered_name = f"user.{slug}"
    try:
        resp = requests.post(
            f"{base_url()}/v1/pull",
            json={
                "model_name": registered_name,
                "checkpoint": model_name,
                "recipe": "flm",
                "stream": True,
            },
            headers=_auth_headers(),
            stream=True,
            timeout=(10, 1800),
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise LemonadeError(f"Could not reach Lemonade server at {base_url()}: {exc}") from exc

    event = None
    with resp:
        for raw_line in resp.iter_lines(decode_unicode=True):
            if not raw_line:
                continue
            if raw_line.startswith("event:"):
                event = raw_line[len("event:"):].strip()
                continue
            if not raw_line.startswith("data:"):
                continue
            try:
                data = json.loads(raw_line[len("data:"):].strip())
            except json.JSONDecodeError:
                continue
            if event == "error":
                raise LemonadeError(data.get("error") or f"Download of '{model_name}' failed")
            if event == "complete":
                yield f"{model_name}: download complete"
                return
            if event == "progress":
                pct = data.get("percent", 0)
                idx, total = data.get("file_index"), data.get("total_files")
                yield f"{model_name}: {data.get('file', '')} {pct}% (file {idx}/{total})"


def search_catalog(query: str, limit: int = 40) -> list[dict]:
    """Real, structured search over Lemonade's own model catalog (GET /v1/models?
    show_all=true) — unlike LM Studio, which has no search API at all (confirmed live:
    no `lms search` subcommand, no undocumented catalog/search HTTP endpoint either;
    `lms get` only ever resolves-and-downloads a single best match, never returns a
    list), Lemonade's catalog is a plain browsable list this app already fetches
    elsewhere, so filtering it by substring is a genuine search, not a workaround.

    Excludes flm-recipe entries deliberately: those are never real catalog listings a
    user could search for here, only ever custom registrations this app itself created
    for FastFlowLM/NPU models (see list_models()'s docstring) — FastFlowLM's actual NPU
    model list lives in a separate catalog (`flm list`) this app has no OS permission to
    read (root-owned binary), so NPU models stay on their existing exact-tag download
    flow (search_and_download) rather than pretending to search them here.

    Matches query as a substring against id/checkpoint/labels, case-insensitive. Results
    are sorted suggested-first, then smallest-first, since a smaller download is a
    reasonable default preference when several results match equally well."""
    try:
        resp = requests.get(
            f"{base_url()}/v1/models", params={"show_all": "true"}, headers=_auth_headers(), timeout=10
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise LemonadeError(f"Could not reach Lemonade server at {base_url()}: {exc}") from exc

    q = query.lower().strip()
    results = []
    for m in resp.json().get("data", []):
        if m.get("recipe") == "flm":
            continue
        haystack = " ".join(
            [m.get("id") or "", m.get("checkpoint") or "", " ".join(m.get("labels") or [])]
        ).lower()
        if q and q not in haystack:
            continue
        results.append(
            {
                "id": m.get("id"),
                "checkpoint": m.get("checkpoint"),
                "recipe": m.get("recipe"),
                "size_gb": m.get("size"),
                "labels": m.get("labels") or [],
                "downloaded": bool(m.get("downloaded")),
                "suggested": bool(m.get("suggested")),
            }
        )
    results.sort(key=lambda r: (not r["suggested"], r["size_gb"] if r["size_gb"] is not None else 0))
    return results[:limit]


def pull_catalog_model(model_id: str):
    """Download a model already known to Lemonade's own catalog, by id — no custom
    registration needed (that's only for FastFlowLM/NPU tags the catalog doesn't carry
    at all; see search_and_download). Yields structured progress dicts rather than text
    lines, so a caller can drive a real byte-level progress bar instead of a scrolling
    log — confirmed live this data is genuinely available (bytes_downloaded/bytes_total/
    percent per file) for ordinary catalog pulls, unlike the FLM path's coarser
    percent-only progress."""
    try:
        resp = requests.post(
            f"{base_url()}/v1/pull",
            json={"model_name": model_id, "stream": True},
            headers=_auth_headers(),
            stream=True,
            timeout=(10, 1800),
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise LemonadeError(f"Could not reach Lemonade server at {base_url()}: {exc}") from exc

    event = None
    with resp:
        for raw_line in resp.iter_lines(decode_unicode=True):
            if not raw_line:
                continue
            if raw_line.startswith("event:"):
                event = raw_line[len("event:"):].strip()
                continue
            if not raw_line.startswith("data:"):
                continue
            try:
                data = json.loads(raw_line[len("data:"):].strip())
            except json.JSONDecodeError:
                continue
            if event == "error":
                raise LemonadeError(data.get("error") or f"Download of '{model_id}' failed")
            if event == "complete":
                yield {"done": True}
                return
            if event == "progress":
                yield {
                    "file": data.get("file", ""),
                    "percent": data.get("percent", 0),
                    "file_index": data.get("file_index"),
                    "total_files": data.get("total_files"),
                    "bytes_downloaded": data.get("bytes_downloaded"),
                    "bytes_total": data.get("bytes_total"),
                }


def _extract_readme_description(text: str, max_len: int = 280) -> str | None:
    """Best-effort: the first substantial prose paragraph from a model's README, after
    stripping YAML frontmatter and skipping headers/badges/images/lists. Hugging Face's
    model API has no free-text description field at all (confirmed live: no `expand`
    value for one exists, only structured `cardData`), so the README is the only real
    source for "what does this model do" — genuinely useful, not synthesized filler."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    # Common "join our Discord/WeChat", "read our blog", "try it here" CTA boilerplate
    # that many model cards lead with — real, but not a description of what the model
    # does, so worth skipping past to find the actual first content paragraph.
    _cta_markers = (
        "join our", "discord", "wechat", "star this repo", "follow us", "check out the",
        "technical report", "api platform", "api services", "try", "blog", "read our",
    )
    in_code_fence = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            in_code_fence = not in_code_fence
            continue
        if in_code_fence:
            continue
        if not line or line.startswith(("#", "!", "[![", "<", "|", "-", "*", ">", "@")):
            continue
        line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)  # [text](url) -> text
        line = re.sub(r"<[^>]+>", "", line)  # strip inline/raw HTML tags (<a>, <img>, <br>, ...)
        line = re.sub(r"[*_`]", "", line)
        line = line.strip("[] \t")
        # A short trailing-colon phrase ("For more information, visit:") is a dangling
        # lead-in with nothing useful before it; a long one is often a substantial
        # paragraph that just happens to end by introducing a list (e.g. "...GLM-5.2's
        # new capabilities include:") — worth keeping, unlike the short case.
        if len(line) < 20 or (line.endswith(":") and len(line) < 60) or line.startswith(("http://", "https://")):
            continue
        if any(marker in line.lower() for marker in _cta_markers):
            continue
        return line[:max_len] + ("…" if len(line) > max_len else "")
    return None


def _fetch_readme_description(repo_id: str, timeout: float = 5.0) -> str | None:
    try:
        resp = requests.get(f"https://huggingface.co/{repo_id}/raw/main/README.md", timeout=timeout)
        if not resp.ok:
            return None
        return _extract_readme_description(resp.text)
    except Exception:
        return None


def _hf_repo_id_from_checkpoint(checkpoint: str | None) -> str | None:
    """'org/repo:file' or 'org/repo' -> 'org/repo'. None for checkpoints that aren't an
    org/repo pair at all (bare tags like FastFlowLM's 'qwen3:0.6b' never reach here
    anyway, since those are flm-recipe and already excluded before this is called)."""
    if not checkpoint:
        return None
    repo = checkpoint.split(":", 1)[0]
    return repo if "/" in repo else None


def enrich_with_hf_metadata(results: list[dict], max_fetch: int = 12) -> list[dict]:
    """For local catalog search results (from search_catalog, which has no date/
    popularity/description fields of its own — Lemonade's catalog is a thin curated
    list), best-effort fills in last_modified/likes/downloads/pipeline_tag/description
    by resolving each result's checkpoint to a Hugging Face repo id and querying HF's
    public API. Concurrent and bounded (max_fetch results, short per-request timeouts)
    so a slow/unreachable network doesn't stall the whole search — results that can't
    be resolved or fetched just keep their original fields, nothing breaks."""
    targets = [(i, r) for i, r in enumerate(results) if _hf_repo_id_from_checkpoint(r.get("checkpoint"))][:max_fetch]
    if not targets:
        return results

    def _fetch_one(repo_id: str) -> dict:
        meta: dict = {}
        try:
            resp = requests.get(
                f"https://huggingface.co/api/models/{repo_id}",
                params={"expand[]": ["lastModified", "likes", "downloads", "pipeline_tag", "tags"]},
                timeout=5,
            )
            if resp.ok:
                data = resp.json()
                meta["last_modified"] = data.get("lastModified")
                meta["likes"] = data.get("likes")
                meta["downloads"] = data.get("downloads")
                meta["pipeline_tag"] = data.get("pipeline_tag")
                meta["tags"] = data.get("tags") or []
        except Exception:
            pass
        meta["description"] = _fetch_readme_description(repo_id)
        return meta

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(targets)) as pool:
        future_to_idx = {
            pool.submit(_fetch_one, _hf_repo_id_from_checkpoint(r["checkpoint"])): i for i, r in targets
        }
        for fut, idx in future_to_idx.items():
            try:
                results[idx].update(fut.result(timeout=8))
            except Exception:
                pass
    return results


def search_huggingface(query: str, limit: int = 12) -> list[dict]:
    """Live search against Hugging Face's public model API (no key needed) — the exact
    endpoint Lemonade's own web UI calls from the browser, confirmed by inspecting its
    JS bundle directly rather than guessing: `renderer.bundle.js` calls
    `https://huggingface.co/api/models?search=...&filter=gguf&sort=downloads`. This is
    what makes a query like "glm 5.2" actually find something — search_catalog() alone
    only searches Lemonade's own ~111-entry curated catalog, which doesn't carry every
    model on Hugging Face (confirmed live: 0 results for "glm 5.2" there, despite
    several real, popular GGUF repos existing for it).

    Descriptions are filled in concurrently via each repo's README (see
    _fetch_readme_description) since the search API itself has no description field."""
    try:
        params = {
            "filter": "gguf",
            "limit": limit,
            "sort": "downloads",
            "expand[]": ["lastModified", "likes", "downloads", "pipeline_tag", "tags"],
        }
        if query.strip():
            params["search"] = query
        resp = requests.get(
            "https://huggingface.co/api/models",
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise LemonadeError(f"Hugging Face search failed: {exc}") from exc

    results = []
    for m in resp.json():
        repo_id = m.get("id")
        if not repo_id:
            continue
        results.append(
            {
                "id": repo_id,
                "checkpoint": repo_id,
                "recipe": "llamacpp",
                "source": "huggingface",
                "size_gb": None,  # a repo often holds many quant variants; real size is
                                  # only known once a specific file is picked at download time
                "labels": [m.get("pipeline_tag")] if m.get("pipeline_tag") else [],
                "downloaded": False,
                "suggested": False,
                "last_modified": m.get("lastModified"),
                "likes": m.get("likes"),
                "downloads": m.get("downloads"),
                "pipeline_tag": m.get("pipeline_tag"),
                "tags": m.get("tags") or [],
            }
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(results) or 1) as pool:
        future_to_idx = {pool.submit(_fetch_readme_description, r["id"]): i for i, r in enumerate(results)}
        for fut, idx in future_to_idx.items():
            try:
                results[idx]["description"] = fut.result(timeout=6)
            except Exception:
                results[idx]["description"] = None
    return results


_QUANT_PREFERENCE = (
    "q4_k_m", "q4_k_s", "q4_0", "q5_k_m", "q5_k_s", "q6_k", "q8_0", "q3_k_m", "q3_k_s", "q2_k",
)


def _pick_gguf_file(repo_id: str, timeout: float = 10.0) -> tuple[str, int] | None:
    """Auto-picks a default quantization from a Hugging Face GGUF repo's file tree —
    this app has no per-file quant picker UI, same simplicity as `lms get -y` auto-
    selecting rather than prompting. Prefers common mid-quality quants in the order the
    llama.cpp community generally treats as a sane default (Q4_K_M and friends);
    excludes mmproj/vision-projector files (auxiliary, not the main weights) and
    non-.gguf files. Falls back to the smallest .gguf file if nothing in the
    preference list matches. None if the repo has no .gguf files at all."""
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

    candidates = [
        e for e in entries
        if e.get("type") == "file"
        and e.get("path", "").lower().endswith(".gguf")
        and "mmproj" not in e.get("path", "").lower()
    ]
    if not candidates:
        return None

    for pref in _QUANT_PREFERENCE:
        for e in candidates:
            if pref in e["path"].lower():
                return e["path"], e.get("size", 0)

    smallest = min(candidates, key=lambda e: e.get("size") or float("inf"))
    return smallest["path"], smallest.get("size", 0)


_result_size_cache: dict[str, float | None] = {}


def _download_size_gb(model_id: str) -> float | None:
    """The actual download size for a search result, in GB: the default GGUF quant's size
    for a GGUF repo (what the download would really pull — not the whole multi-quant repo),
    or the full repo size otherwise. Cached (sizes are static). None only if every lookup
    fails."""
    if not model_id:
        return None
    if model_id in _result_size_cache:
        return _result_size_cache[model_id]
    repo = model_id.split(":", 1)[0]
    size_gb = None
    if repo and "/" in repo:
        picked = _pick_gguf_file(repo)  # GGUF repo → default-quant file size
        if picked and picked[1]:
            size_gb = round(picked[1] / 1e9, 2)
        else:
            try:  # non-GGUF (safetensors etc.) → sum the whole repo
                resp = requests.get(
                    f"https://huggingface.co/api/models/{repo}/tree/main",
                    params={"recursive": "true"}, timeout=10,
                )
                resp.raise_for_status()
                total = sum(e.get("size", 0) or 0 for e in resp.json() if e.get("type") == "file")
                size_gb = round(total / 1e9, 2) if total else None
            except requests.RequestException:
                size_gb = None
    _result_size_cache[model_id] = size_gb
    return size_gb


def fill_result_sizes(results: list[dict], max_fetch: int = 60) -> list[dict]:
    """Concurrently fill size_gb for every search result that's missing it, so the Downloads
    page can always show a real size instead of 'varies by quantization'. Per-repo failures
    just leave that one None (rendered as 'size unknown'); never raises."""
    todo = [r for r in results if r.get("size_gb") is None and r.get("id")][:max_fetch]
    if not todo:
        return results
    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(_download_size_gb, r["id"]): r for r in todo}
        for fut in cf.as_completed(futs):
            try:
                sz = fut.result()
            except Exception:
                sz = None
            if sz is not None:
                futs[fut]["size_gb"] = sz
    return results


def pull_huggingface_repo(repo_id: str):
    """Download a model straight from Hugging Face by repo id — not in Lemonade's own
    catalog, so this goes through the same custom-registration shape the NPU download
    path uses (confirmed live this genuinely works for arbitrary HF GGUF repos, not
    just FastFlowLM tags: {"model_name": "user.<slug>", "checkpoint": "<repo>:<file>",
    "recipe": "llamacpp"} — tested end-to-end against a real small repo outside
    Lemonade's catalog). Auto-picks a default quantization via _pick_gguf_file first,
    then yields the same structured progress dicts as pull_catalog_model(). Once
    downloaded, this registration makes the model indistinguishable from a native
    Lemonade catalog entry in future search_catalog() calls (same recipe, "llamacpp"),
    so it won't show as a stale duplicate on a repeat search."""
    picked = _pick_gguf_file(repo_id)
    if not picked:
        raise LemonadeError(f"No downloadable .gguf file found in '{repo_id}'")
    filename, _size = picked
    slug = re.sub(r"[^A-Za-z0-9_.-]", "-", repo_id)
    registered_name = f"user.{slug}"
    try:
        resp = requests.post(
            f"{base_url()}/v1/pull",
            json={
                "model_name": registered_name,
                "checkpoint": f"{repo_id}:{filename}",
                "recipe": "llamacpp",
                "stream": True,
            },
            headers=_auth_headers(),
            stream=True,
            timeout=(10, 3600),
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise LemonadeError(f"Could not reach Lemonade server at {base_url()}: {exc}") from exc

    event = None
    with resp:
        for raw_line in resp.iter_lines(decode_unicode=True):
            if not raw_line:
                continue
            if raw_line.startswith("event:"):
                event = raw_line[len("event:"):].strip()
                continue
            if not raw_line.startswith("data:"):
                continue
            try:
                data = json.loads(raw_line[len("data:"):].strip())
            except json.JSONDecodeError:
                continue
            if event == "error":
                raise LemonadeError(data.get("error") or f"Download of '{repo_id}' failed")
            if event == "complete":
                yield {"done": True, "file": filename}
                return
            if event == "progress":
                yield {
                    "file": data.get("file", filename),
                    "percent": data.get("percent", 0),
                    "file_index": data.get("file_index"),
                    "total_files": data.get("total_files"),
                    "bytes_downloaded": data.get("bytes_downloaded"),
                    "bytes_total": data.get("bytes_total"),
                }


def embed(texts: list[str]) -> list[list[float]]:
    """Embeddings via Lemonade's OpenAI-compatible /v1/embeddings, for the cross-chat
    memory feature — migrated here from LM Studio (see README) so cross-chat memory no
    longer depends on LM Studio being installed/running at all. Confirmed live:
    nomic-embed-text-v1-GGUF (llamacpp recipe) auto-loads on first request into its own
    'embedding' slot, same as LM Studio's embedding model did, and coexists fine
    alongside whatever chat model (NPU or otherwise) is currently loaded — Lemonade
    tracks embedding/llm/reranking/etc as separate slots (see /v1/health's max_models),
    so no "one model at a time" conflict here either.

    Inputs are truncated to 1400 chars — confirmed live that Lemonade's bundled
    llama-server has a hard 512-token physical batch ceiling per request; a ~2500-char
    assistant reply (612 tokens) 500'd with "input too large to process" during the
    migration off LM Studio's embedding model, which apparently didn't hit this limit.
    1800 chars still wasn't a safe margin for dense/markdown-heavy text (up to ~4
    chars/token instead of English's typical ~4-5); 1400 held across every case found
    in this app's existing chunk history. For cross-chat memory recall (this function's
    only caller), the first ~1400 chars carry the semantic gist just fine — that's all
    similarity search needs."""
    safe_texts = [t[:1400] for t in texts]
    try:
        resp = requests.post(
            f"{base_url()}/v1/embeddings",
            json={"model": EMBEDDING_MODEL_ID, "input": safe_texts},
            headers=_auth_headers(),
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise LemonadeError(f"Embedding request failed: {exc}") from exc
    # Wrapped because a 200 response is no guarantee of the expected shape — confirmed
    # live (2026-07-23): right after a reboot, with the embedding model not yet loaded,
    # Lemonade returned a 200 whose JSON had no "data" key at all, and the resulting
    # bare KeyError escaped every `except LemonadeError` upstream — killing api_chat's
    # worker thread before it could queue its end-of-stream sentinel, which left that
    # chat's SSE stream hung forever (the generator blocks on the queue indefinitely).
    try:
        data = sorted(resp.json()["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in data]
    except (ValueError, KeyError, TypeError) as exc:
        raise LemonadeError(f"Embedding response malformed: {exc}") from exc


# --- Voice: speech-to-text (Whisper) and text-to-speech (Kokoro), for mic input and
# spoken responses in the chat UI. Both live in their own Lemonade resource slots
# ("transcription" and "tts", separate from "llm"/"embedding" per /v1/health's
# max_models), so loading either one never evicts whatever chat model is currently
# loaded - confirmed live, same coexistence guarantee embed() already relies on above.
STT_MODEL_ID = os.environ.get("LEMONADE_STT_MODEL_ID", "Whisper-Tiny")
TTS_MODEL_ID = os.environ.get("LEMONADE_TTS_MODEL_ID", "kokoro-v1")
DEFAULT_TTS_VOICE = os.environ.get("LEMONADE_TTS_VOICE", "af_heart")


def _ensure_voice_model_loaded(model_id: str) -> None:
    """Loads a voice model on first use, same lazy-load pattern
    _ensure_embedding_model_loaded() in app.py uses for the embedding model. Whisper
    specifically needs one real fix baked in here, not left to Lemonade's own default:
    its default backend on this machine is ROCm, which - like the ComfyUI and llama.cpp
    GPU crashes documented elsewhere in this README - doesn't actually work here (the
    whisper-server process crashes immediately, exit code 134, confirmed live). The CPU
    backend works correctly and is plenty fast for Whisper-Tiny/Base-sized models, so
    it's forced explicitly on every load call - confirmed live this must be a top-level
    field on the /v1/load payload (`{"whispercpp_backend": "cpu"}`), not nested under
    `recipe_options` as every other per-recipe option in this file is passed - Lemonade
    silently ignored the nested form and kept defaulting to the broken ROCm backend."""
    if is_model_loaded(model_id):
        return
    payload: dict = {"model_name": model_id}
    if model_id == STT_MODEL_ID:
        payload["whispercpp_backend"] = "cpu"
    try:
        resp = requests.post(f"{base_url()}/v1/load", json=payload, headers=_auth_headers(), timeout=120)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise LemonadeError(f"Failed to load voice model '{model_id}': {exc}") from exc


def _convert_to_wav(audio_bytes: bytes) -> bytes:
    """Converts arbitrary recorded audio to 16kHz mono PCM WAV via ffmpeg, for
    transcribe_audio() below. Not optional: confirmed live that whisper-server rejects
    WebM/Opus outright (`400 Invalid request`) - the exact format Chrome's
    MediaRecorder produces by default with no format specified, i.e. what the mic
    button in this app's own UI actually sends - while it accepts 16kHz mono WAV
    cleanly. Requires the `ffmpeg` binary on PATH; raises a clear LemonadeError if it's
    missing rather than a cryptic subprocess failure three calls deep."""
    if not shutil.which("ffmpeg"):
        raise LemonadeError(
            "ffmpeg is not installed - required to convert recorded audio for transcription "
            "(install it with your system package manager, e.g. `sudo apt install ffmpeg`)."
        )
    with tempfile.NamedTemporaryFile(suffix=".input") as src, tempfile.NamedTemporaryFile(suffix=".wav") as dst:
        src.write(audio_bytes)
        src.flush()
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", src.name, "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", dst.name],
                capture_output=True,
                timeout=30,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            raise LemonadeError(
                f"Failed to convert recorded audio: {exc.stderr.decode(errors='replace')[:300]}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise LemonadeError("Audio conversion timed out") from exc
        return dst.read()


def transcribe_audio(audio_bytes: bytes, filename: str = "audio.webm") -> str:
    """Speech-to-text via Lemonade's OpenAI-compatible /v1/audio/transcriptions
    (Whisper.cpp under the hood). Loads the STT model on first use if it isn't already
    loaded. Raises LemonadeError on failure - callers (the /api/voice/transcribe route)
    turn that into a normal JSON error response, same pattern as every other backend
    call in this app."""
    _ensure_voice_model_loaded(STT_MODEL_ID)
    wav_bytes = _convert_to_wav(audio_bytes)
    try:
        resp = requests.post(
            f"{base_url()}/v1/audio/transcriptions",
            files={"file": ("audio.wav", wav_bytes)},
            data={"model": STT_MODEL_ID},
            headers=_auth_headers(),
            timeout=60,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise LemonadeError(f"Transcription failed: {exc}") from exc
    return (resp.json().get("text") or "").strip()


def synthesize_speech(text: str, voice: str = DEFAULT_TTS_VOICE) -> bytes:
    """Text-to-speech via Lemonade's OpenAI-compatible /v1/audio/speech (Kokoro under
    the hood) - returns raw MP3 bytes directly (confirmed live: no base64/JSON envelope,
    the response body *is* the audio file). Loads the TTS model on first use if it
    isn't already loaded. Text is capped at 2000 characters - a full multi-paragraph
    assistant reply would otherwise take a genuinely long time to synthesize and produce
    an unwieldy audio clip; a spoken response is meant to be listened to, not read via a
    text-to-speech engine word-for-word, so trimming to a reasonable spoken length is
    the right tradeoff over hard-failing or forcing the caller to trim it themselves."""
    _ensure_voice_model_loaded(TTS_MODEL_ID)
    try:
        resp = requests.post(
            f"{base_url()}/v1/audio/speech",
            json={"model": TTS_MODEL_ID, "input": text[:2000], "voice": voice},
            headers=_auth_headers(),
            timeout=60,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise LemonadeError(f"Speech synthesis failed: {exc}") from exc
    return resp.content


def chat_stream(
    messages: list[dict],
    model: str,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    frequency_penalty: float | None = 0.3,
    chat_template_kwargs: dict | None = None,
    enable_thinking: bool | None = None,
    tools: list[dict] | None = None,
):
    """Yield raw SSE 'data: ...' lines — identical contract to
    lmstudio_client.chat_stream(), since Lemonade's /v1/chat/completions is
    OpenAI-compatible the same way LM Studio's is.

    frequency_penalty defaults on (mild, 0.3) rather than off — a real user report of
    a response getting stuck repeating the same line over and over (traced mainly to a
    separate bug — a weather query silently falling through to mismatched generic search
    results, now fixed — but repetition-loop degeneration is a well-known local/quantized
    model failure mode independent of any particular trigger) surfaced that this app sent
    *no* repetition guard of any kind to either backend, unlike most local-LLM UIs, which
    default one on specifically because of how common this failure mode is. Confirmed
    live the param is accepted without error by both the llamacpp-recipe and flm-recipe
    (FastFlowLM) paths; pass frequency_penalty=None to disable if it ever fights a
    specific model's own output in a way that matters more than the safety net."""
    payload: dict = {"model": model, "messages": messages, "temperature": temperature, "stream": True}
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if frequency_penalty:
        payload["frequency_penalty"] = frequency_penalty
    if chat_template_kwargs:
        payload["chat_template_kwargs"] = chat_template_kwargs
    if enable_thinking is not None:
        payload["enable_thinking"] = enable_thinking
    if tools:
        payload["tools"] = tools
    # Tool-enabled chat must pass through Hearth's guard shim, which repairs
    # model-native tool markup and named choices before the chat worker consumes it.
    url = ("http://127.0.0.1:13306/v1/chat/completions" if tools
           else f"{base_url()}/v1/chat/completions")
    try:
        with requests.post(
            url,
            json=model_test_profiles.apply(payload, "lemonade"),
            headers=_auth_headers(),
            stream=True,
            timeout=(10, 600),
        ) as resp:
            resp.raise_for_status()
            # Requests' 512-byte default buffers small token events during thinking.
            for line in resp.iter_lines(chunk_size=1):
                if line:
                    yield line.decode("utf-8", errors="replace")
    except requests.RequestException as exc:
        raise LemonadeError(f"Chat request failed: {exc}") from exc


_npu_ids_cache: set[str] | None = None
_npu_ids_cache_at: float = 0.0
_NPU_IDS_TTL = 60.0


def npu_model_ids() -> set[str]:
    """The set of Lemonade catalog ids that are flm-recipe (NPU) models — the only ones that
    contend with FastFlowLM for the NPU and the only ones this app should ever count as
    "NPU usage" when a Lemonade request is served. TTL-cached (unlike npu_tops_max's
    permanent cache above): unlike static hardware specs, the catalog can gain/lose entries
    (a download, a delete) within a running process's lifetime. Needs `show_all=true` — the
    plain /v1/models listing (what list_models() below uses) doesn't reliably surface recipe
    for every entry, confirmed live (2026-07-26) while wiring up NPU-usage metrics. Returns
    the last-known set (or empty) on a lookup failure, same fail-soft contract as everything
    else here — a transient Lemonade hiccup should degrade "NPU busy?" to "unknown/no", never
    raise into a metrics/live-feed code path."""
    global _npu_ids_cache, _npu_ids_cache_at
    now = time.monotonic()
    if _npu_ids_cache is not None and (now - _npu_ids_cache_at) < _NPU_IDS_TTL:
        return _npu_ids_cache
    try:
        resp = requests.get(
            f"{base_url()}/v1/models", params={"show_all": "true"}, headers=_auth_headers(), timeout=4
        )
        resp.raise_for_status()
        _npu_ids_cache = {m["id"] for m in resp.json().get("data", []) if m.get("recipe") == "flm"}
        _npu_ids_cache_at = now
    except requests.RequestException:
        return _npu_ids_cache or set()
    return _npu_ids_cache


def is_npu_model(model_id: str | None) -> bool:
    """True if `model_id` is one of Lemonade's own flm-recipe (NPU) models. Convenience
    wrapper around npu_model_ids() for call sites that just want a yes/no for one id."""
    return bool(model_id) and model_id in npu_model_ids()


_tops_max_cache: float | None = None


def npu_tops_max() -> float | None:
    """This NPU's rated max int8 TOPS (58 on this machine's XDNA2 chip) — static
    hardware info, so cached in-process rather than re-fetched every message. Not the
    same thing as the per-response utilization snapshot below."""
    global _tops_max_cache
    if _tops_max_cache is not None:
        return _tops_max_cache
    try:
        resp = requests.get(f"{base_url()}/v1/system-info", headers=_auth_headers(), timeout=5)
        resp.raise_for_status()
        value = resp.json().get("devices", {}).get("amd_npu", {}).get("tops_max_int")
    except requests.RequestException:
        return None
    if value:
        _tops_max_cache = float(value)
    return _tops_max_cache


def npu_utilization_percent() -> float | None:
    """A live instantaneous reading from GET /v1/system-stats, not an average over any
    particular response — Lemonade/FastFlowLM don't report per-request compute the way
    they report per-request token counts, so this is the closest real signal available:
    sampled right after a response finishes, same "good enough, not exact" spirit as
    this app's own CPU/RAM stats elsewhere."""
    try:
        resp = requests.get(f"{base_url()}/v1/system-stats", headers=_auth_headers(), timeout=5)
        resp.raise_for_status()
        return resp.json().get("npu_percent")
    except requests.RequestException:
        return None
