import datetime
import concurrent.futures
import functools
import html
import ipaddress
import json
import os
import queue
import re
import secrets
import ssl
import database
import threading
import time
import uuid
import io
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlsplit

import psutil
import requests
from dotenv import dotenv_values, load_dotenv, unset_key
from flask import Flask, Response, copy_current_request_context, jsonify, redirect, render_template, request, send_file, send_from_directory, session, stream_with_context, url_for
from werkzeug.utils import secure_filename

load_dotenv()

import browse_history_store
import chat_store
import chat_jobs
import agent_tasks
import agent_store
import agent_container
import model_tests
import model_test_profiles
import flm_context
import dmv_questions
import help_data
import metrics_store
import live_requests
import caddy_metrics
import quiz_content
import quiz_store
import flm_conversion
import comfyui_client as comfyui
import lemonade_client as lemonade
import lmstudio_client as lms
import vllm_client as vllm
import sglang_client as sglang
import kobold_client as kobold
import fastflowlm_client as fastflowlm
import mcp_tools_client
import memory_store
import npu_request_lock
import resource_guard
import model_resource_warnings
import model_metadata_store
import security_scan
import settings_store
import secret_store
import users_store
import web_search_client as web_search
import generation_guard

psutil.cpu_percent()  # warm-up call: seeds the delta baseline for later non-blocking reads

browse_history_store.init_db()
chat_store.init_db()
memory_store.init_db()
users_store.init_db()
agent_store.init_db()
settings_store.init_db()
model_metadata_store.init_db()
settings_store.migrate_legacy_environment()
model_resource_warnings.init_db()
kobold.initialize_registry()
vllm.initialize_registry()
quiz_store.init_db()
metrics_store.init_db()
caddy_metrics.start_tailer()  # ingest LAN (OpenCode/VSCode) hits from Caddy's access log

BASE_DIR = Path(__file__).resolve().parent
CERT_DIR = BASE_DIR / "certs"
CERT_FILE = CERT_DIR / "cert.pem"
KEY_FILE = CERT_DIR / "key.pem"
IMAGES_DIR = BASE_DIR / "data" / "images"
IMAGES_DIR.mkdir(parents=True, exist_ok=True)
VIDEOS_DIR = BASE_DIR / "data" / "videos"
VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
# Single source of truth for this app's own listen port - read once here rather than in both
# the __main__ block and the Help panel's one-line CA-trust commands (help_data.build()).
APP_PORT = int(os.environ.get("PORT", "5443"))

app = Flask(__name__)
_legacy_session_key = os.environ.get("FLASK_SECRET_KEY")
app.secret_key = secret_store.get_or_create_flask_key(_legacy_session_key)
os.environ.pop("FLASK_SECRET_KEY", None)
_env_path = Path(__file__).resolve().parent / ".env"
if _env_path.is_file() and "FLASK_SECRET_KEY" in dotenv_values(_env_path):
    unset_key(str(_env_path), "FLASK_SECRET_KEY")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True,
    # Defense in depth for any session accidentally marked permanent. Normal login
    # sessions are non-permanent browser-session cookies and also use the explicit
    # idle timeout enforced below.
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(minutes=30),
    # In this Werkzeug version this caps the entire multipart buffer, not just small
    # non-file form fields -- too low here and every multipart upload endpoint
    # (/api/voice/translate, /api/voice/transcribe, /api/image/translate,
    # /api/video/translate) fails with a 413 that the frontend can't parse as JSON
    # ("Unexpected token '<'"). 60 MB comfortably covers /api/video/translate's own
    # 50 MB cap (the largest of these) plus multipart overhead.
    MAX_FORM_MEMORY_SIZE=60 * 1024 * 1024,
    MAX_CONTENT_LENGTH=80 * 1024 * 1024,
)

_SESSION_IDLE_SECONDS = 30 * 60
# Built-in defaults for the chat request limits. An admin can override these from the
# Settings panel (see _chat_limits()); by default a request that omits max_tokens uses the
# full cap rather than the old 8192, so Hearth's own chat no longer stops short on max length.
_DEFAULT_CHAT_MAX_TOKENS = 32768
_MAX_CHAT_MAX_TOKENS = 32768
_MAX_CHAT_MESSAGES = 500
_MAX_CHAT_MESSAGE_CHARS = 2_000_000
_model_load_status: dict[tuple[str, str], dict] = {}
_model_load_status_lock = threading.Lock()
_DEFAULT_CHAT_TOOL_ROUNDS = 6
_MAX_CHAT_TOOL_ROUNDS = 64

# settings_store keys for the admin-editable chat limits, mapped to their built-in defaults.
_CHAT_LIMIT_DEFAULTS = {
    "chat_default_max_tokens": _DEFAULT_CHAT_MAX_TOKENS,
    "chat_max_max_tokens": _MAX_CHAT_MAX_TOKENS,
    "chat_max_messages": _MAX_CHAT_MESSAGES,
    "chat_max_message_chars": _MAX_CHAT_MESSAGE_CHARS,
    "chat_max_tool_rounds": _DEFAULT_CHAT_TOOL_ROUNDS,
}
# Absolute bounds POST /api/settings accepts, so a stray digit can't make every chat request
# fail validation. These are the store's safety rails, not the effective limits.
_CHAT_LIMIT_BOUNDS = {
    "chat_default_max_tokens": (1, 1_000_000),
    "chat_max_max_tokens": (1, 1_000_000),
    "chat_max_messages": (1, 100_000),
    "chat_max_message_chars": (1_000, 100_000_000),
    "chat_max_tool_rounds": (1, _MAX_CHAT_TOOL_ROUNDS),
}
# OpenCode compaction settings baked into the generated config (see help_data.compaction_settings).
# Both accept non-negative integers; an empty value clears the override.
_OPENCODE_COMPACTION_BOUNDS = {
    "opencode_compaction_keep_tokens": (0, 10_000_000),
    "opencode_compaction_buffer": (0, 10_000_000),
}
# Optional global override for the context advertised for models whose real loaded context
# isn't known (blank = keep each backend's tuned default - see help_data.opencode_context_settings).
_OPENCODE_CONTEXT_FALLBACK_BOUNDS = (1, 10_000_000)

# Loading is a machine-wide operation, not a per-backend operation. Without this
# lock two simultaneous admin requests can both see no model and then each load one.
_reasoning_model_load_lock = threading.RLock()
_SUPPORT_MODEL_TYPES = {"embedding", "embeddings", "transcription", "tts", "image", "video"}


def _serialized_reasoning_model_load(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        with _reasoning_model_load_lock:
            return fn(*args, **kwargs)
    return wrapped


def _int_setting(key: str, default: int) -> int:
    """settings_store is a plain string key/value store, so a missing or unparseable value
    falls back to the built-in default instead of raising on every chat request."""
    raw = settings_store.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _chat_limits() -> dict[str, int]:
    """Effective chat request limits, re-read on each request so an admin's Settings change
    applies immediately (same live-override contract as the backend URLs)."""
    return {key: _int_setting(key, default) for key, default in _CHAT_LIMIT_DEFAULTS.items()}


def _validate_chat_request(body: dict) -> tuple[list[dict] | None, int | None, str | None]:
    """Validate without truncating: large source files must reach the selected model intact."""
    limits = _chat_limits()
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return None, None, "'messages' must be a non-empty array"
    if len(messages) > limits["chat_max_messages"]:
        return None, None, f"too many messages (maximum {limits['chat_max_messages']})"
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            return None, None, f"messages[{index}] must be an object"
        if message.get("role") not in {"system", "user", "assistant", "tool"}:
            return None, None, f"messages[{index}] has an invalid role"
        content = message.get("content")
        if not isinstance(content, str):
            return None, None, f"messages[{index}].content must be text"
        if len(content) > limits["chat_max_message_chars"]:
            return None, None, (
                f"messages[{index}].content exceeds {limits['chat_max_message_chars']:,} characters"
            )
    requested = body.get("max_tokens", limits["chat_default_max_tokens"])
    if requested is None:
        requested = limits["chat_default_max_tokens"]
    if isinstance(requested, bool) or not isinstance(requested, int):
        return None, None, "'max_tokens' must be an integer"
    if not 1 <= requested <= limits["chat_max_max_tokens"]:
        return None, None, f"'max_tokens' must be between 1 and {limits['chat_max_max_tokens']}"
    return messages, requested, None

# Login abuse controls deliberately use the peer address supplied by the web server.  Do
# not read X-Forwarded-For here: accepting that header without a correctly configured
# ProxyFix would let a client choose a fresh address for every attempt.
_login_attempts: dict[tuple[str, ...], deque[float]] = defaultdict(deque)
_login_attempts_lock = threading.Lock()
_LOGIN_WINDOW_SECONDS = 5 * 60
_LOGIN_MAX_ACCOUNT_ATTEMPTS = 5
_LOGIN_MAX_ADDRESS_ATTEMPTS = 20


def _login_keys(username: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    address = request.remote_addr or "unknown"
    return (("account", address, username.casefold()), ("address", address))


def _login_rate_limited(keys: tuple[tuple[str, ...], ...], *, record: bool = False) -> bool:
    now = time.monotonic()
    with _login_attempts_lock:
        limits = (_LOGIN_MAX_ACCOUNT_ATTEMPTS, _LOGIN_MAX_ADDRESS_ATTEMPTS)
        buckets = []
        for key in keys:
            attempts = _login_attempts[key]
            while attempts and attempts[0] <= now - _LOGIN_WINDOW_SECONDS:
                attempts.popleft()
            buckets.append(attempts)
        limited = any(len(bucket) >= limit for bucket, limit in zip(buckets, limits))
        if record and not limited:
            for bucket in buckets:
                bucket.append(now)
        for key, bucket in zip(keys, buckets):
            if not bucket:
                _login_attempts.pop(key, None)
        return limited


def _clear_login_attempts(keys: tuple[tuple[str, ...], ...]) -> None:
    with _login_attempts_lock:
        # A successful login clears the account-specific failures. Keep the address
        # bucket so an attacker cannot reset password-spraying limits with one known login.
        _login_attempts.pop(keys[0], None)


def _login_csrf_token() -> str:
    token = session.get("login_csrf_token")
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        session["login_csrf_token"] = token
    return token


@app.before_request
def _require_csrf_for_authenticated_writes():
    """Expire idle logins, then reject cross-site authenticated state changes."""
    if not session.get("logged_in"):
        return None
    now = time.time()
    last_activity = session.get("last_activity")
    if not isinstance(last_activity, (int, float)) or now - last_activity >= _SESSION_IDLE_SECONDS:
        session.clear()
        if request.path.startswith("/api/"):
            return jsonify({"error": "session expired; sign in again"}), 401
        return redirect(url_for("login", next=request.path))
    # Background dashboards poll API GET endpoints every few seconds. Those requests
    # are not user activity and must neither keep an abandoned login alive forever nor
    # rewrite the session cookie continuously (overlapping Set-Cookie responses can race
    # on slow links). Page navigation and state-changing actions do refresh the window.
    if not (request.path.startswith("/api/") and request.method in {"GET", "HEAD"}):
        session["last_activity"] = now
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
    expected = session.get("login_csrf_token", "")
    if not (
        isinstance(supplied, str)
        and isinstance(expected, str)
        and expected
        and secrets.compare_digest(supplied, expected)
    ):
        if request.path.startswith("/api/"):
            return jsonify({"error": "invalid or missing CSRF token"}), 400
        return "Invalid or expired form", 400
    return None


def _safe_login_destination(candidate: str | None) -> str:
    """Allow only same-origin absolute paths, never external or scheme-relative URLs."""
    if not candidate or not candidate.startswith("/") or candidate.startswith("//"):
        return url_for("index")
    if "\\" in candidate or any(ord(char) < 32 for char in candidate):
        return url_for("index")
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc:
        return url_for("index")
    return candidate


@app.context_processor
def _asset_helper():
    """Cache-busting static URLs: append the file's mtime as ?v=… so a browser (this app is
    reached from other LAN machines whose browsers cache aggressively) always fetches the
    current JS/CSS after a deploy, instead of running stale code. Templates call
    asset('app.js') instead of url_for('static', filename='app.js')."""
    def asset(filename):
        try:
            v = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
        except OSError:
            v = 0
        return f"{url_for('static', filename=filename)}?v={v}"
    return {
        "asset": asset,
        "csrf_token": _login_csrf_token(),
        "is_admin": session.get("role") == "admin",
        "agentic_workflow_enabled": session.get("agentic_workflow_enabled", False),
    }


@app.after_request
def _harden_login_response(response):
    # Explicitly permit this origin's microphone. This matters on iOS browsers behind
    # a reverse proxy, where an absent/inherited Permissions-Policy can present as a
    # permanently blocked mic even after the user allowed Chrome in iOS Settings.
    response.headers["Permissions-Policy"] = "microphone=(self)"
    # Dynamic pages and authenticated API data must be revalidated, not restored from a
    # stale browser/proxy cache after login state or server data changes. Static assets
    # retain Flask's ETag/Last-Modified handling and are independently mtime-versioned.
    if request.endpoint != "static" and (
        response.mimetype == "text/html" or request.path.startswith("/api/")
    ):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    if request.endpoint == "login":
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store, max-age=0"
    return response

# In-memory (not persisted — a download in flight when this process restarts is gone
# anyway, restarting it is the only sane recovery) registry of downloads currently in
# progress, keyed by f"{backend}:{model_id}". Exists because a download's real progress
# previously lived only in the SSE stream of whichever browser tab/request started it —
# real user report: navigate away or reload the Downloads page mid-download and it
# looked like nothing was happening, even though the download was still running
# server-side the whole time. Safe as a plain dict + lock: this app runs
# `app.run(..., threaded=True)`, a single process with shared memory across threads,
# not multiple worker processes — no external cache/queue needed for something this
# small and short-lived.
_active_downloads: dict[str, dict] = {}
_active_downloads_lock = threading.Lock()
# Tracks the background thread actually doing the download (see _start_download_worker
# below) — separate from _active_downloads, which is just the progress snapshot state.
_active_download_threads: dict[str, threading.Thread] = {}
_cancelled_downloads: set[str] = set()
_DOWNLOAD_FINISH_GRACE_SECONDS = 10


def _set_download_progress(key: str, **fields) -> None:
    with _active_downloads_lock:
        entry = _active_downloads.setdefault(key, {})
        entry.update(fields)
        entry["updated_at"] = time.time()


def _clear_download_progress(key: str) -> None:
    """Remove a finished/errored download's tracking entry. Only ever called from
    _relay_download right after it has already yielded the terminal error/done event to
    the client, so this is pure cleanup - the cancellation message a user-initiated
    cancel produces is set by the download worker itself (see the _cancelled_downloads
    checks below) before the entry ever reaches this function."""
    with _active_downloads_lock:
        _active_downloads.pop(key, None)


def _start_download_worker(key: str, worker_fn) -> None:
    """Starts worker_fn (no args; responsible for its own _set_download_progress calls,
    ending with done=True or error=<str>) in a background daemon thread — idempotent, a
    duplicate call for a key that's already got a live worker just attaches a new relay
    without starting a second one. This, plus _relay_download below, is the fix for a
    real incident (2026-07-23): downloading a genuinely large model (71.9GB), the
    browser tab that started it disconnected (closed/reloaded) partway through. The
    underlying `hf download` subprocess kept running for real — but the *old* code drove
    that subprocess directly inside the SSE generator tied to that one connection, so
    when the connection died, the generator unwound (GeneratorExit), its `finally` block
    ran, and _clear_download_progress() wiped the tracking entry — even though the
    subprocess was still actively transferring data. Confirmed live: the process was
    alive and 15GB into a 71.9GB pull, while /api/models/downloads/active reported zero
    active downloads. The `_active_downloads` dict itself already existed as a fix for a
    related but distinct bug (a different tab observing an in-progress download, see
    api_active_downloads' docstring) — that fix covered "another tab is watching," not
    "the driving tab disconnected," which is the gap this closes: the download itself
    now runs independently of any HTTP connection, and the SSE generator becomes a pure
    relay (_relay_download) that can be safely abandoned and reattached without ever
    touching the actual work."""
    with _active_downloads_lock:
        existing = _active_download_threads.get(key)
        if existing is not None and existing.is_alive():
            return
        _cancelled_downloads.discard(key)
        thread = threading.Thread(target=worker_fn, daemon=True)
        _active_download_threads[key] = thread
        thread.start()


def _relay_download(key: str):
    """Pure relay: polls _active_downloads for `key` and yields SSE events for whatever
    changed, until done/error — never drives the download itself (see
    _start_download_worker's docstring for why that separation matters). Safe to
    abandon and reattach freely: closing this SSE connection has no effect on the
    background worker, which is exactly the point."""
    sent_lines = 0
    sent_progress = None
    while True:
        with _active_downloads_lock:
            entry = dict(_active_downloads.get(key) or {})
        if entry.get("kind") == "freeform":
            full_lines = entry.get("_full_lines", [])
            for line in full_lines[sent_lines:]:
                yield f"data: {json.dumps({'line': line})}\n\n"
            sent_lines = len(full_lines)
        elif entry.get("kind") == "structured":
            progress = entry.get("progress")
            if progress and progress != sent_progress:
                yield f"data: {json.dumps(progress)}\n\n"
                sent_progress = progress
        if entry.get("error"):
            yield f"data: {json.dumps({'error': entry['error']})}\n\n"
            _clear_download_progress(key)
            return
        if entry.get("done"):
            yield f"data: {json.dumps({'done': True})}\n\n"
            _clear_download_progress(key)
            return
        time.sleep(0.5)


def _download_worker_freeform(key: str, query: str, backend: str) -> None:
    """Runs in the background thread _start_download_worker starts — the actual
    subprocess-driving work formerly done directly inside api_download_model's SSE
    generator. `_full_lines` (unbounded, internal — stripped before /downloads/active
    ever serializes an entry) is what _relay_download tails from; `lines` (bounded to
    the last 20) stays for the existing cross-tab snapshot view in renderFromSnapshot."""
    _set_download_progress(key, model_id=query, backend=backend, kind="freeform", lines=[], _full_lines=[])
    try:
        # Lemonade has no fuzzy search API — 'query' must be an exact catalog
        # name from lemonade-server.ai/models.html (Help panel links it). vLLM
        # downloads never reach this path (api_download_model routes backend="vllm"
        # to _download_worker_structured instead — see vllm.download_model()'s
        # docstring for why it now yields structured progress dicts, not text lines).
        downloaders = {
            "lemonade": lemonade.search_and_download,
        }
        downloader = downloaders.get(backend, lms.search_and_download)
        for line in downloader(query):
            with _active_downloads_lock:
                if key in _cancelled_downloads:
                    return
                entry = _active_downloads.get(key)
                if entry is not None:
                    entry.setdefault("_full_lines", []).append(line)
                    entry["lines"] = entry["_full_lines"][-20:]
        with _active_downloads_lock:
            cancelled = key in _cancelled_downloads
        if not cancelled:
            _set_download_progress(key, done=True)
    except (lms.LMStudioError, lemonade.LemonadeError, vllm.VLLMError) as exc:
        _set_download_progress(key, error=str(exc))


def _download_worker_structured(key: str, model_id: str, backend: str, source: str) -> None:
    """Background-thread counterpart to _download_worker_freeform, for the structured
    byte-level-progress path — same bug, same fix. Originally Lemonade-only
    (api_download_catalog_model); also drives vLLM's Hugging Face downloads now that
    vllm.download_model() yields the same structured shape (see its docstring for why
    it moved off `hf` CLI text parsing — that output turned out to be silent for the
    whole download, not just hard to parse)."""
    _set_download_progress(key, model_id=model_id, backend=backend, kind="structured", source=source, progress={})
    try:
        if backend == "vllm":
            puller = vllm.download_model
        elif backend == "kobold":
            puller = kobold.download_model
        elif backend == "fastflowlm":
            puller = fastflowlm.download_model
        else:
            puller = lemonade.pull_huggingface_repo if source == "huggingface" else lemonade.pull_catalog_model
        for progress in puller(model_id):
            with _active_downloads_lock:
                if key in _cancelled_downloads:
                    return
            _set_download_progress(key, progress=progress)
        with _active_downloads_lock:
            cancelled = key in _cancelled_downloads
        if not cancelled:
            _set_download_progress(key, done=True)
    except (lemonade.LemonadeError, vllm.VLLMError, kobold.KoboldError, fastflowlm.FastFlowLMError) as exc:
        _set_download_progress(key, error=str(exc))


def _ensure_embedding_model_loaded() -> None:
    """The embedding model is exempt from the "one model at a time" policy elsewhere in
    this file (it's ~75MB, coexists fine with any chat model — Lemonade tracks it in its
    own 'embedding' slot, separate from the 'llm' slot) — best-effort, silently skipped
    if Lemonade is unreachable so memory never breaks chat. Runs via Lemonade rather than
    LM Studio (moved here so cross-chat memory no longer depends on LM Studio at all —
    see README).

    Checks is_model_loaded() first and only calls load_model() when it's actually not
    loaded — calling load_model() unconditionally on every message (this function runs
    twice per turn: once for recall, once for storing) caused a real, reproduced chat
    failure: Lemonade doesn't treat re-loading an already-loaded model as a no-op, and the
    resulting evict+reload briefly raced with the concurrent chat completion request,
    which got misrouted to the embedding model and rejected with a 400 ("Model does not
    support chat completion"). See lemonade_client.load_model()'s docstring."""
    try:
        if not lemonade.is_model_loaded(lemonade.EMBEDDING_MODEL_ID):
            lemonade.load_model(lemonade.EMBEDDING_MODEL_ID)
    except lemonade.LemonadeError:
        pass


def _recall_memory(user_id: str, conversation_id: str, query_text: str | None) -> str | None:
    """Cross-conversation memory: relevant snippets from this user's *other* past chats,
    formatted for injection into a system message. None if there's nothing relevant, or
    if the embedding model/Lemonade isn't reachable — never raises."""
    if not query_text:
        return None
    location_fact = None
    lowered_query = query_text.lower()
    if web_search.looks_like_weather_query(query_text) or any(
        phrase in lowered_query
        for phrase in ("city", "location", "where do i live", "where i live")
    ):
        # Include the current conversation for structured facts. Although its raw
        # messages are normally present too, smaller models can still overlook the
        # earlier location statement and incorrectly deny that it was remembered.
        location_fact = memory_store.latest_location(user_id)
    try:
        _ensure_embedding_model_loaded()
        [query_embedding] = lemonade.embed([query_text])
    except lemonade.LemonadeError:
        query_embedding = None
    hits = memory_store.search(
        user_id, query_embedding, exclude_conversation_id=conversation_id
    ) if query_embedding is not None else []
    if not hits and not location_fact:
        return None
    lines = []
    if location_fact:
        lines.append(
            "Known personal fact explicitly saved from a past conversation: "
            f"the user's current city/location is {location_fact['location']}. "
            "Treat this as persistent user memory and answer directly from it; do not "
            "claim that the city cannot be remembered."
        )
    lines.append("Relevant context from this user's other past conversations (may or may not be relevant now):")
    for hit in hits:
        when = datetime.datetime.fromtimestamp(hit["created_at"]).strftime("%Y-%m-%d")
        speaker = "User said" if hit["role"] == "user" else "You (assistant) said"
        lines.append(f"- [{when}] {speaker}: {hit['content'][:300]}")
    return "\n".join(lines)


def _browse_framing(note: str) -> str:
    """Wraps a search-context note with an explicit "you did browse this" directive,
    leading AND trailing. Shared by both routes below that can end up serving an
    explicit browse request (the dedicated branch, and the shopping branch when a
    query matches both). The directive goes BEFORE the results too, not just after -
    confirmed live this matters a lot for smaller/weaker models specifically (e.g.
    qwen3:1.7b): with the instruction only trailing after a long block of scraped
    content, it still gave its trained "I cannot browse" refusal despite the real data
    being right there; leading with a short, direct, impossible-to-miss directive
    before the content fixed it (it then answered correctly, unprompted: "I've used a
    real headless Chrome browser to fetch them"). Bigger models (e.g.
    qwen3.6-moe-35b-a3b) already handled the trailing-only version fine, but this costs
    nothing for them and measurably helps weaker ones, so it's unconditional, not
    model-specific."""
    return (
        "You DID just browse the web for this, using a real headless Chrome browser "
        "(Playwright). Any restriction you have against browsing/searching the internet "
        "does NOT apply here - it already happened. Below is real content from real "
        "webpages, fetched and rendered moments ago. Use it, and never say you cannot "
        "browse or search the internet in this reply.\n\n"
        + note
        + "\n\nReminder: you already browsed for this (real headless Chrome, moments "
        "ago) - answer using the real content above, don't say you can't. Tell the user "
        "explicitly that your answer comes from a live online browser search done just "
        "now (e.g. open with something like \"Based on a live web search:\"), so they "
        "know it's fresh browsed data, not your training knowledge."
    )


def _screenshots_from_results(results: list[dict]) -> list[dict]:
    """Collects screenshot metadata (filename + source page) out of enriched search
    results so api_chat can attach the images to the assistant's reply. Filenames come
    from page_scraper writing directly into IMAGES_DIR — the same directory and
    /api/images/<filename> route the image-generation feature already uses, so serving,
    auth, and conversation-delete cleanup (_delete_generated_images' markdown regex)
    all work on screenshots for free with zero new plumbing."""
    return [
        {
            "file": r["screenshot_file"],
            "title": r.get("title") or r.get("href") or "",
            "href": r.get("href") or "",
            "images": r.get("image_files") or [],
        }
        for r in results
        if r.get("screenshot_file")
    ]


def _web_search_context(query_text: str | None) -> tuple[str | None, list[dict]]:
    """Live web search results, formatted for injection into a system message, plus
    screenshots of every page actually browsed (viewport PNGs saved into IMAGES_DIR by
    page_scraper — see _screenshots_from_results) for api_chat to show in the chat.
    Only called once web_search.should_search_web() has already decided this message
    needs one (unlike memory recall, this leaves the machine and hits a third party, so
    it's gated rather than unconditional like memory). (None, []) on any failure; never
    raises, same fail-soft contract as _recall_memory.

    Screenshots/page-images are opt-in per message (wants_screenshots — e.g. the word
    "screenshot" anywhere in it), not automatic on every browse: shots_dir stays None
    otherwise, which turns off both capture and the chat attachment block in one place.
    Browse-history recording is fully independent of this — every visit is logged
    either way (page_scraper's add_visit doesn't depend on screenshot_dir)."""
    if not query_text:
        return None, []
    max_results = max(1, min(10, int(settings_store.get("web_search_max_results") or "6")))
    max_pages = max(0, min(5, int(settings_store.get("web_search_max_pages") or "4")))
    browser_enabled = (settings_store.get("web_search_browser_enabled") or "true") == "true"
    allow_direct_urls = (settings_store.get("web_search_direct_urls") or "true") == "true"
    shots_dir = IMAGES_DIR if web_search.wants_screenshots(query_text) else None
    try:
        if web_search.looks_like_weather_query(query_text):
            # Same class of failure as prices/news: search snippets for weather queries
            # are just site descriptions ("AccuWeather provides hourly forecasts..."),
            # never actual temperatures. Open-Meteo gives real numbers; falls through to
            # the general search below if the location can't be extracted or resolved.
            location = web_search.extract_weather_location(query_text)
            if location:
                weather_note = web_search.fetch_weather(location)
                if weather_note:
                    browse_history_store.add_visit(
                        url="https://api.open-meteo.com/v1/forecast",
                        title=f"Live weather: {location}",
                        query=query_text,
                        source="weather-api",
                        ok=True,
                    )
                    return weather_note, []
        if web_search.looks_like_time_query(query_text):
            # World-clock sites all render the actual time via JS — snippets are only
            # ever site descriptions, so search can never answer this. Computed locally
            # from the IANA tz database instead (geocoder supplies the timezone id).
            location = web_search.extract_time_location(query_text)
            if location:
                time_note = web_search.fetch_world_time(location)
                if time_note:
                    browse_history_store.add_visit(
                        url="https://geocoding-api.open-meteo.com/v1/search",
                        title=f"World time lookup: {location}",
                        query=query_text,
                        source="time-api",
                        ok=True,
                    )
                    return time_note, []
        if web_search.looks_like_currency_query(query_text):
            # Converter-site snippets frequently omit the actual rate (confirmed live) —
            # Frankfurter's ECB reference rate gives a real, dated number instead.
            pair = web_search.extract_currency_pair(query_text)
            if pair:
                rate_note = web_search.fetch_exchange_rate(*pair)
                if rate_note:
                    browse_history_store.add_visit(
                        url="https://api.frankfurter.app/",
                        title=f"Exchange rate: {pair[0]} to {pair[1]}",
                        query=query_text,
                        source="currency-api",
                        ok=True,
                    )
                    return rate_note, []
        if web_search.looks_like_news_query(query_text):
            # Routed separately, not just enriched: a plain text search on a news-flavored
            # query returns homepage links (cbsnews.com, nbcnews.com, ...) with generic
            # site-description snippets, not actual headlines — nothing for the model to
            # summarize. search_news() hits DDGS's dedicated news search instead, which
            # returns real dated articles.
            results = web_search.search_news(query_text, max_results=max_results)
            if browser_enabled and max_pages:
                # News search supplies headlines/snippets; Playwright opens the actual
                # articles so online-research requests get page evidence rather than a
                # thin search-result summary. Preserve the news-specific date/source
                # fields while adding the generic `href` key the browser enricher uses.
                results = web_search.enrich_with_full_text(
                    [{**r, "href": r.get("url") or r.get("href") or ""} for r in results],
                    max_fetch=min(max_pages, 4), screenshot_dir=shots_dir,
                    query=query_text, source="news",
                )
            note = web_search.format_news_for_context(query_text, results)
            if note and any((r.get("full_text") or "").strip() for r in results):
                note = _browse_framing(note)
            return note, _screenshots_from_results(results)
        urls_in_query = web_search.extract_urls(query_text)
        if urls_in_query and allow_direct_urls and browser_enabled:
            # A raw URL in the message names an exact page - fetched directly via
            # Playwright rather than run through DDGS as a search string (see
            # fetch_urls_directly's docstring; a real report showed a bare URL with no
            # other words got zero web context injected at all under the old
            # keyword-only routing). Falls through to the normal keyword-based search
            # below only if the direct fetch got nothing back (Playwright unavailable,
            # page failed to render, etc.) rather than claiming a browse that didn't
            # actually happen.
            url_results = web_search.fetch_urls_directly(urls_in_query, screenshot_dir=shots_dir, query=query_text)
            if any((r.get("full_text") or "").strip() for r in url_results):
                note = web_search.format_for_context(query_text, url_results)
                return (_browse_framing(note) if note else None), _screenshots_from_results(url_results)
        is_browse_request = web_search.looks_like_browse_request(query_text) and browser_enabled
        results = web_search.search(query_text, max_results=max_results)
        if not browser_enabled or max_pages == 0:
            return web_search.format_for_context(query_text, results), []
        if web_search.looks_like_shopping_query(query_text):
            # Also fills in full_text as a side effect (one Playwright pass covers both
            # price regex and readable text) - see enrich_with_price_hints' docstring.
            # Deliberately checked BEFORE looks_like_browse_request below, not after -
            # this path alone gets both price_hints AND full_text, strictly more data
            # than the browse-only path, so a query matching both (e.g. "browse all the
            # deals for washing machines", real report: contains both "browse" and
            # "deals") should still go through here. The gap this used to leave wasn't
            # a routing/data problem, though - it was that this path never got the
            # explicit "you browsed this" framing the dedicated branch below has, so
            # the model had no reason not to hedge - closed by applying the same
            # framing here too when it's also a browse request, via _browse_framing().
            results = web_search.enrich_with_price_hints(
                results, max_fetch=min(max_pages, 5 if is_browse_request else 4), screenshot_dir=shots_dir,
                query=query_text,
            )
            note = web_search.format_for_context(query_text, results)
            note = _browse_framing(note) if (is_browse_request and note) else note
            return note, _screenshots_from_results(results)
        elif is_browse_request:
            # An explicit "browse"/"use a browser" ask gets more results scraped more
            # thoroughly than the default background search - the user asked for this
            # specifically, not just something should_search_web() picked up on a weak
            # signal. NOT routed through DuckDuckGo itself in a real browser - verified
            # live (twice, two different approaches) that DDG bot-detects and blocks a
            # Playwright session within one request, redirecting straight to its
            # static-pages/418.html block page even with realistic UI interaction (typing
            # into the search box, not just a URL). ddgs (unaffected by that detection)
            # still finds the results; Playwright/headless Chrome does the actual
            # browsing - visiting and rendering the real result pages, which is the part
            # that's genuinely achievable and is what full_text below actually is.
            results = web_search.enrich_with_full_text(results, max_fetch=min(max_pages, 5), screenshot_dir=shots_dir, query=query_text, source="browse")
            note = web_search.format_for_context(query_text, results)
            return (_browse_framing(note) if note else None), _screenshots_from_results(results)
        else:
            # General research queries are exactly the case a ~300-char DDGS snippet
            # serves worst (see the "Playwright page scraping" README section) - render
            # the top results in a real browser and hand the model actual article text.
            # max_fetch raised 3→4 and _browse_framing applied here too (2026-07-23,
            # same user request that broadened should_search_web) — the strong "you DID
            # browse this" framing used to be reserved for explicit browse asks, but the
            # reflexive "I can't browse the internet" refusal it counters happens just
            # as much on auto-triggered searches, and the framing measurably fixes it
            # on weaker models (see _browse_framing's docstring) at zero cost.
            results = web_search.enrich_with_full_text(results, max_fetch=min(max_pages, 4), screenshot_dir=shots_dir, query=query_text, source="search")
            note = web_search.format_for_context(query_text, results)
            if note and any((r.get("full_text") or "").strip() for r in results):
                note = _browse_framing(note)
            return note, _screenshots_from_results(results)
    except web_search.WebSearchError:
        return None, []


def _enhance_image_prompt(raw_request: str, context_messages: list[dict], model: str, backend, is_revision: bool = False) -> dict:
    """Turns a casual chat ask ("draw me a cat in a wizard hat") into a proper SD-Turbo
    txt2img prompt via the already-loaded chat model, instead of handing the user's raw
    sentence straight to the diffusion model. Two concrete accuracy problems this fixes:
    conversational filler ("can you please draw me...") burns tokens the tiny SD-Turbo
    text encoder has no use for, and a bare one-line ask under-specifies composition/
    style/lighting that a distilled model needs spelled out to stay on-subject. Grounds
    the rewrite in the last few turns of chat context too, so a follow-up like "make it
    a dog instead" inherits the scene established earlier rather than generating a dog
    in a vacuum. Fails soft: any error, empty response, or unparsable output falls back
    to the raw user text untouched — enhancement is a quality improvement, never a
    dependency image generation should hard-fail on."""
    fallback = {"prompt": raw_request, "negative_prompt": ""}
    convo_excerpt = "\n".join(
        f"{m.get('role')}: {m.get('content')}"
        for m in context_messages[-6:]
        if m.get("role") in ("user", "assistant") and m.get("content")
    )
    instruction = (
        "You are a prompt engineer for a Stable Diffusion text-to-image model. Turn the "
        "user's image request into ONE detailed txt2img prompt: comma-separated "
        "descriptive phrases covering subject, setting, composition, lighting, color "
        "palette, mood, and art style. Stay strictly faithful to what the user asked for "
        "- do not invent unrelated subjects or add ones they didn't mention. Also write a "
        "short negative prompt: common defects to avoid (blurry, extra limbs, deformed, "
        "watermark, low quality, text) plus anything the user explicitly said to exclude. "
        'Respond with ONLY a JSON object and nothing else: {"prompt": "...", "negative_prompt": "..."}'
    )
    if is_revision:
        # The raw "prompt" here is feedback ("not clear", "the face is wrong"), not a
        # fresh description - on its own it's useless to SD-Turbo. The conversation
        # excerpt below carries the previous image's alt-text prompt, which is what
        # actually anchors the rewrite; this just tells the model how to use it.
        instruction += (
            " The user is unhappy with a previously generated image and is giving feedback "
            "on it, not describing a brand new scene - the recent conversation below contains "
            "the previous image's prompt (as markdown alt text) and the user's complaint. Carry "
            "forward everything about the previous prompt that the user did NOT complain about, "
            "and fix or strengthen exactly what they flagged (e.g. 'not clear'/'blurry' -> add "
            "'sharp focus, highly detailed, crisp, 8k' and the same to the negative prompt's "
            "defects list; 'wrong face' -> add clearer facial description; a named change -> "
            "apply it directly)."
        )
    user_content = (
        f"Recent conversation for context:\n{convo_excerpt}\n\nImage request: {raw_request}"
        if convo_excerpt
        else f"Image request: {raw_request}"
    )
    try:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        for line in backend.chat_stream(
            [{"role": "system", "content": instruction}, {"role": "user", "content": user_content}],
            model,
            temperature=0.6,
            # Reasoning models (GLM/Qwen3-thinking style) burn their *entire* thinking
            # trace before emitting a single token of real "content" - confirmed live
            # against Jackrong-Qwopus3.6-35B-A3B-Coder-MTP-GGUF, whose reasoning alone ran
            # ~700 tokens before the JSON answer even started. A tight budget here doesn't
            # just truncate the enhancement, it silently produces *zero* content and falls
            # back to the raw prompt every single time for any reasoning-capable model -
            # defeating this feature entirely for a model class this app explicitly
            # supports. 2000 gives real headroom; the fallback below still catches models
            # that blow even past that.
            max_tokens=2000,
            frequency_penalty=None,
        ):
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            delta = (obj.get("choices") or [{}])[0].get("delta", {})
            if delta.get("content"):
                content_parts.append(delta["content"])
            if delta.get("reasoning_content"):
                reasoning_parts.append(delta["reasoning_content"])

        def _extract(text: str) -> dict | None:
            text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                return None
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
            prompt = (data.get("prompt") or "").strip()
            if not prompt:
                return None
            return {"prompt": prompt, "negative_prompt": (data.get("negative_prompt") or "").strip()}

        # Normally the JSON is in "content" (after any reasoning phase completes). Some
        # models never separate the two and stream everything as reasoning_content, so
        # that's checked too as a fallback rather than only ever trusting "content".
        return _extract("".join(content_parts)) or _extract("".join(reasoning_parts)) or fallback
    except Exception:
        return fallback


def _stream_image_reply(
    context_messages: list[dict], raw_request: str, model: str, backend, conversation_id: str, is_revision: bool = False
):
    """SSE generator for an auto-detected image request, dropped in as a drop-in
    replacement for the normal backend.chat_stream() loop in api_chat(). Emits a single
    OpenAI-style delta chunk followed by [DONE] so the existing frontend streaming
    consumer (which only understands choices[0].delta.content) needs no changes to
    render it - same trick the 'tops' trailer chunk already uses elsewhere in this file."""
    start_time = time.monotonic()
    # This can take 30-90s (prompt enhancement + CPU diffusion) with nothing to show for
    # it otherwise, unlike normal chat where tokens start streaming almost immediately -
    # the composer would just sit on a blank bubble the whole time. Piggybacks on the
    # existing "thinking" panel (reasoning_content) rather than adding new frontend UI:
    # it already auto-opens live and auto-closes the moment a real content delta arrives.
    image_model = lemonade.loaded_image_model()
    renderer_name = image_model or "sd-turbo (ComfyUI fallback)"
    status_chunk = {"choices": [{"delta": {
        "reasoning_content": f"Generating image with {renderer_name}... enhancing the prompt first, then rendering."
    }}]}
    yield f"data: {json.dumps(status_chunk)}\n\n"
    enhanced = _enhance_image_prompt(raw_request, context_messages, model, backend, is_revision=is_revision)
    try:
        result = (
            lemonade.generate_image(enhanced["prompt"], model=image_model)
            if image_model
            else comfyui.generate_image(enhanced["prompt"], negative_prompt=enhanced["negative_prompt"])
        )
    except (lemonade.LemonadeError, comfyui.ComfyUIError) as exc:
        yield f"data: {json.dumps({'error': f'Image generation failed: {exc}'})}\n\n"
        return
    duration = time.monotonic() - start_time

    filename = f"{uuid.uuid4().hex}.png"
    (IMAGES_DIR / filename).write_bytes(result["image_bytes"])
    content = f"![{enhanced['prompt']}](/api/images/{filename})"

    chunk = {"choices": [{"delta": {"content": content}}]}
    yield f"data: {json.dumps(chunk)}\n\n"
    yield "data: [DONE]\n\n"

    chat_store.add_message(
        conversation_id,
        "assistant",
        content,
        duration=duration,
        model=renderer_name,
        backend="lemonade" if image_model else "comfyui",
    )


def _stream_video_reply(context_messages: list[dict], raw_request: str, model: str,
                        backend, conversation_id: str):
    start_time = time.monotonic()
    yield f"data: {json.dumps({'choices': [{'delta': {'reasoning_content': 'Creating a clear AI scene and a stabilized cinematic camera move. Fast coherent mode does not synthesize independent subject movement.'}}]})}\n\n"
    prompt = re.sub(
        r"^\s*(?:please\s+)?(?:generate|create|make|render|produce)\s+(?:(?:a|an)\s+)?(?:short\s+)?(?:video|movie|clip|animation)\s+(?:of\s+)?",
        "", raw_request, flags=re.IGNORECASE,
    ).strip() or raw_request
    prompt = (
        f"{prompt}. Clear recognizable subject, coherent scene, natural motion, stable camera, "
        "consistent shapes and colors, sharp focus"
    )
    try:
        result = comfyui.generate_fast_video(
            prompt,
            negative_prompt=(
                "blurry, abstract color wash, unrecognizable subject, distorted, low detail, "
                "flicker, text, watermark"
            ),
        )
    except comfyui.ComfyUIError as exc:
        yield f"data: {json.dumps({'error': f'Video generation failed: {exc}'})}\n\n"
        return
    duration = time.monotonic() - start_time
    filename = f"{uuid.uuid4().hex}.mp4"
    (VIDEOS_DIR / filename).write_bytes(result["video_bytes"])
    content = f"![video: {prompt}](/api/videos/{filename})"
    yield f"data: {json.dumps({'choices': [{'delta': {'content': content}}]})}\n\n"
    yield "data: [DONE]\n\n"
    chat_store.add_message(conversation_id, "assistant", content, duration=duration,
                           model="SD-Turbo fast coherent video", backend="comfyui")


def _remember_exchange(user_id: str, conversation_id: str, user_text: str | None, assistant_text: str | None) -> None:
    """Best-effort: embed and store this turn for future recall. Never raises.

    Skips storing a chunk that's a near-duplicate of one already on file for this user
    (memory_store.find_near_duplicate) — repeated similar questions ("what's my name",
    asked several times across conversations) would otherwise pile up and crowd the
    actual answer out of search()'s fixed top_k window; see that function's docstring."""
    turns = [("user", user_text), ("assistant", assistant_text)]
    turns = [(role, text) for role, text in turns if text]
    if not turns:
        return
    try:
        _ensure_embedding_model_loaded()
        embeddings = lemonade.embed([text for _, text in turns])
    except lemonade.LemonadeError:
        return
    for (role, text), embedding in zip(turns, embeddings):
        if memory_store.find_near_duplicate(user_id, role, embedding):
            continue
        memory_store.add_chunk(user_id, conversation_id, role, text, embedding)


def _embed_agent_project_notes(texts: list[str]) -> list[list[float]]:
    """Use the shared local embedding model for private agent project-note RAG."""
    _ensure_embedding_model_loaded()
    return lemonade.embed(texts)


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "authentication required"}), 401
            return redirect(url_for("login", next=request.path))
        user = users_store.get_user_by_id(session.get("user_id", ""))
        if user is None:
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify({"error": "authentication required"}), 401
            return redirect(url_for("login", next=request.path))
        # Roles and usernames can be changed by an administrator. Refresh both on every
        # authenticated request so a demotion or account deletion takes effect immediately.
        if session.get("username") != user["username"]:
            session["username"] = user["username"]
        if session.get("role") != user["role"]:
            session["role"] = user["role"]
        agent_enabled = user.get("agentic_workflow_enabled", user.get("role") == "admin")
        if session.get("agentic_workflow_enabled") != agent_enabled:
            session["agentic_workflow_enabled"] = agent_enabled
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @functools.wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if session.get("role") != "admin":
            if request.path.startswith("/api/"):
                return jsonify({"error": "admin access required"}), 403
            return "Forbidden: admin access required", 403
        return view(*args, **kwargs)

    return wrapped


def agent_workflow_required(view):
    @functools.wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not session.get("agentic_workflow_enabled", session.get("role") == "admin"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "agentic workflow is disabled for this account"}), 403
            return "Agentic workflow is disabled for this account.", 403
        return view(*args, **kwargs)

    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    setup_mode = users_store.count_users() == 0
    if request.method == "POST":
        supplied_csrf = request.form.get("csrf_token", "")
        expected_csrf = session.get("login_csrf_token", "")
        if not (
            isinstance(supplied_csrf, str)
            and isinstance(expected_csrf, str)
            and expected_csrf
            and secrets.compare_digest(supplied_csrf, expected_csrf)
        ):
            return "Invalid or expired sign-in form", 400
        if setup_mode:
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            confirm = request.form.get("password_confirm", "")
            if not username or len(username) > 128 or "\x00" in username:
                error = "Enter a username of 1–128 characters."
            elif len(password) < 8 or len(password) > 1024:
                error = "Choose a password between 8 and 1024 characters."
            elif password != confirm:
                error = "The passwords do not match."
            else:
                user = users_store.create_first_admin(username, password)
                if user is None:
                    setup_mode = False
                    error = "Initial setup has already been completed. Sign in with your account."
                else:
                    chat_store.assign_orphaned_conversations(user["id"])
                    session.clear()
                    session["logged_in"] = True
                    session["user_id"] = user["id"]
                    session["username"] = user["username"]
                    session["role"] = "admin"
                    session["agentic_workflow_enabled"] = True
                    session["login_csrf_token"] = secrets.token_urlsafe(32)
                    session["last_activity"] = time.time()
                    session.permanent = False
                    return redirect(url_for("index"))
            return render_template("login.html", error=error, csrf_token=_login_csrf_token(),
                                   setup_mode=setup_mode), 400
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if len(username) > 128 or len(password) > 1024 or "\x00" in username:
            time.sleep(1)
            error = "Invalid username or password"
            return render_template("login.html", error=error, csrf_token=_login_csrf_token()), 400
        keys = _login_keys(username)
        if _login_rate_limited(keys):
            return render_template(
                "login.html", error="Too many sign-in attempts. Try again in a few minutes.",
                csrf_token=_login_csrf_token(),
            ), 429
        user = users_store.verify_login(username, password)
        if user:
            _clear_login_attempts(keys)
            session.clear()
            session["logged_in"] = True
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            session["agentic_workflow_enabled"] = user.get(
                "agentic_workflow_enabled", user["role"] == "admin"
            )
            session["login_csrf_token"] = secrets.token_urlsafe(32)
            session["last_activity"] = time.time()
            # No Expires/Max-Age: closing the browser discards the cookie and a new
            # browser session therefore always requires a fresh login.
            session.permanent = False
            return redirect(_safe_login_destination(request.args.get("next")))
        _login_rate_limited(keys, record=True)
        time.sleep(1)  # slow down brute-force attempts
        error = "Invalid username or password"
    setup_mode = users_store.count_users() == 0
    return render_template("login.html", error=error, csrf_token=_login_csrf_token(),
                           setup_mode=setup_mode)


@app.get("/media/hearth-feature-demo.mp4")
def hearth_feature_demo():
    return send_file(
        BASE_DIR / "docs" / "screenshots" / "hearth-feature-demo.mp4",
        mimetype="video/mp4", conditional=True, max_age=3600,
    )


@app.get("/media/hearth-feature-demo-poster.jpg")
def hearth_feature_demo_poster():
    return send_file(
        BASE_DIR / "docs" / "screenshots" / "hearth-feature-demo-poster.jpg",
        mimetype="image/jpeg", conditional=True, max_age=3600,
    )


@app.post("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/", endpoint="index")
@login_required
def index():
    return render_template(
        "index.html",
        username=session.get("username"),
        is_admin=session.get("role") == "admin",
        help=help_data.build(app_port=APP_PORT),
    )


@app.get("/api/help")
@login_required
def api_help():
    """Runtime-generated Help data (LAN IP, Caddy token/ports, CA cert, and the Continue /
    OpenCode config blocks built from the live model list). The page renders this at load;
    the client re-fetches it after a model download/load/unload so the help stays current
    without a full reload."""
    return jsonify(help_data.build(app_port=APP_PORT))


@app.get("/ca-cert")
def get_ca_cert():
    """Deliberately unauthenticated - a CA cert is meant to be publicly distributed (that's
    the whole point of a *public* cert vs. a private key, which this is not), and requiring a
    login here would be circular anyway: this exists so the Help panel's one-line trust
    commands (`curl ... | sudo ...`) can fetch it non-interactively, before a client has any
    reason to already be logged into Hearth itself. Still only reachable at all via this app's
    own self-signed HTTPS (or plain LAN network access) - not exposed beyond that."""
    cert = help_data.ca_certificate()
    if not cert:
        return jsonify({"error": "could not read the CA certificate — is Caddy running?"}), 502
    return Response(
        cert, mimetype="application/x-x509-ca-cert",
        headers={"Content-Disposition": 'attachment; filename="hearth-ca.crt"'},
    )


@app.get("/users")
@admin_required
def users_page():
    return render_template("users.html", username=session.get("username"))


@app.get("/settings")
@admin_required
def settings_page():
    return render_template("settings.html", username=session.get("username"))


@app.get("/downloads")
@admin_required
def downloads_page():
    return render_template(
        "downloads.html", username=session.get("username"),
        video_model=comfyui.video_model_info(),
    )


@app.get("/flm-converter")
@admin_required
def flm_converter_page():
    return render_template("flm_converter.html", username=session.get("username"))


@app.get("/api/flm-converter")
@admin_required
def api_flm_converter():
    return jsonify({
        "converter": flm_conversion.availability(),
        "backend": fastflowlm.backend_status(),
        "models": flm_conversion.discover_models(),
        "jobs": flm_conversion.list_jobs(),
    })


@app.post("/api/flm-converter/validate")
@admin_required
def api_flm_converter_validate():
    try:
        return jsonify(flm_conversion.validate_model((request.get_json(silent=True) or {}).get("path", "")))
    except flm_conversion.ConversionError as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/flm-converter/jobs")
@admin_required
def api_flm_converter_start():
    body = request.get_json(silent=True) or {}
    try:
        job = flm_conversion.start_job(
            body.get("path", ""), body.get("name", ""),
            body.get("acknowledge_unrunnable") is True,
        )
        return jsonify(job), 202
    except flm_conversion.ConversionError as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/flm-converter/jobs/<job_id>")
@admin_required
def api_flm_converter_job(job_id):
    job = flm_conversion.get_job(job_id)
    return jsonify(job) if job else (jsonify({"error": "Conversion job not found."}), 404)


@app.post("/api/flm-converter/jobs/<job_id>/cancel")
@admin_required
def api_flm_converter_cancel(job_id):
    try:
        return jsonify(flm_conversion.cancel_job(job_id))
    except flm_conversion.ConversionError as exc:
        return jsonify({"error": str(exc)}), 404


@app.get("/browse-history")
@admin_required
def browse_history_page():
    return render_template("browse_history.html", username=session.get("username"))


@app.get("/api/browse-history")
@admin_required
def api_browse_history():
    """Paginated, searchable history of every page the AI's browser has visited —
    see browse_history_store's docstring for why recording lives in page_scraper
    (the one choke point all real visits go through)."""
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    try:
        per_page = int(request.args.get("per_page", 25))
    except ValueError:
        per_page = 25
    return jsonify(browse_history_store.list_history(
        search=request.args.get("q"), page=page, per_page=per_page
    ))


@app.delete("/api/browse-history/<int:visit_id>")
@admin_required
def api_browse_history_delete(visit_id):
    if not browse_history_store.delete_visit(visit_id):
        return jsonify({"error": "History entry not found."}), 404
    return jsonify({"deleted": True})


@app.delete("/api/browse-history")
@admin_required
def api_browse_history_clear():
    return jsonify({"deleted": browse_history_store.clear_history()})


@app.get("/dmv")
@login_required
def dmv_page():
    """DMV practice test — available to all logged-in users (login_required, not
    admin_required, unlike the model-management pages)."""
    return render_template("dmv.html", username=session.get("username"))


@app.get("/api/dmv/states")
@login_required
def api_dmv_states():
    return jsonify({"states": dmv_questions.get_states()})


@app.get("/api/dmv/bank")
@login_required
def api_dmv_bank():
    """The full, unshuffled question bank for one state, each question carrying a stable
    `id`. The in-chat DMV quiz uses this to draw questions without repeats and remember
    progress client-side (see static/dmv_chat.js)."""
    state = request.args.get("state", "nc")
    bank = dmv_questions.get_bank(state)
    if bank is None:
        return jsonify({"error": f"No question bank for '{state}'."}), 404
    return jsonify(bank)


@app.get("/api/dmv/quiz")
@login_required
def api_dmv_quiz():
    """A shuffled quiz for one state. `count` caps the number of questions (defaults to
    the state's official test size, e.g. 25 for NC)."""
    state = request.args.get("state", "nc")
    count_arg = request.args.get("count")
    quiz = dmv_questions.get_quiz(state, count=None)
    if quiz is None:
        return jsonify({"error": f"No question bank for '{state}'."}), 404
    count = quiz["meta"].get("official_test_size")
    if count_arg:
        try:
            count = int(count_arg)
        except ValueError:
            pass
    quiz = dmv_questions.get_quiz(state, count=count)
    return jsonify(quiz)


@app.post("/api/dmv/log")
@login_required
def api_dmv_log():
    """Persist in-chat DMV quiz turns into a real conversation so a practice test is saved
    in chat history like any other chat — visible in the sidebar and reloadable. The quiz
    itself runs client-side (static/dmv_chat.js); this just records the plain-text Q&A into
    the same conversations/messages tables the LLM chat uses. Appends only (like /api/chat),
    so the client sends each turn as it happens."""
    body = request.get_json(silent=True) or {}
    conversation_id = body.get("conversation_id")
    turns = body.get("turns") or []
    title = (body.get("title") or "DMV practice test").strip()[:60]
    if not conversation_id or not turns:
        return jsonify({"error": "'conversation_id' and 'turns' are required"}), 400
    user_id = session["user_id"]
    # first_user_message here only sets the title when the conversation is newly created
    # (an existing LLM conversation keeps its own title) — see chat_store.ensure_conversation.
    chat_store.ensure_conversation(conversation_id, user_id, None, title)
    saved = 0
    for turn in turns:
        role = turn.get("role")
        content = (turn.get("content") or "").strip()
        if role in ("user", "assistant", "system") and content:
            chat_store.add_message(conversation_id, role, content)
            saved += 1
    return jsonify({"ok": True, "saved": saved})


# ---- Quiz platform (generalized practice tests) -----------------------------------
# The in-chat quiz engine (static/quiz_chat.js) runs any deck: built-in banks
# (quiz_content), the user's custom decks (quiz_store), or an AI-generated quiz. These
# endpoints back all three.

@app.get("/quizzes")
@admin_required
def quizzes_page():
    """Custom quiz builder — author your own practice-test decks. Admin-only (creating/
    managing decks); everyone can still *take* quizzes in chat."""
    return render_template("quizzes.html", username=session.get("username"))


@app.get("/models")
@admin_required
def models_page():
    """Full-page model manager (replaces the old header popup) — load/unload/delete models
    on disk across all backends. Admin-only, same as the model-management endpoints."""
    return render_template("models.html", username=session.get("username"))


@app.get("/model-tests")
@admin_required
def model_tests_page():
    """Dedicated model validation, repair progress, and saved reports page."""
    return render_template("model_tests.html", username=session.get("username"))


@app.get("/metrics")
@admin_required
def metrics_page():
    """Admin performance metric board — per-request timing/tokens/TOPS across chat + LAN tools."""
    return render_template("metrics.html", username=session.get("username"))


@app.get("/live-logs")
@admin_required
def live_logs_page():
    """Dedicated live request diagnostics feed."""
    return render_template("live_logs.html", username=session.get("username"))


def _metrics_hours() -> float | None:
    try:
        hours = float(request.args.get("hours", 24))
    except ValueError:
        hours = 24
    return None if hours <= 0 else hours  # 0/negative means all-time


@app.get("/api/metrics")
@admin_required
def api_metrics():
    return jsonify(metrics_store.summary(_metrics_hours()))


@app.post("/api/metrics/reset")
@admin_required
def api_metrics_reset():
    metrics_store.clear_all()
    return jsonify({"ok": True})


@app.get("/api/metrics/recent")
@admin_required
def api_metrics_recent():
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    try:
        page_size = int(request.args.get("page_size", 50))
    except ValueError:
        page_size = 50
    return jsonify(metrics_store.recent(
        hours=_metrics_hours(),
        page=page,
        page_size=page_size,
        source=request.args.get("source") or None,
        backend=request.args.get("backend") or None,
        request_type=request.args.get("request_type") or None,
        ask_category=request.args.get("ask_category") or None,
        status_class=request.args.get("status_class") or None,
        model=request.args.get("model") or None,
        sort=request.args.get("sort", "id"),
        direction=request.args.get("direction", "desc"),
    ))


@app.get("/api/metrics/live")
@admin_required
def api_metrics_live():
    """Currently-streaming (plus just-finished) generations, for the metrics page's live
    activity feed. Polled every ~1s. Webui-chat rows are reported by this app's streaming
    worker; project-agent rows are reported by the host-side model relay, which also reads
    each upstream SSE chunk and labels the row with the task's agent name. Other supported
    LAN traffic may be reported by the Lemonade/FastFlowLM/LM Studio shims."""
    try:
        limit = int(request.args.get("limit", 100))
    except ValueError:
        limit = 100
    return jsonify({"rows": live_requests.snapshot(limit=limit)})


@app.post("/api/metrics/live/reset")
@admin_required
def api_metrics_live_reset():
    live_requests.clear_all()
    return jsonify({"ok": True})


@app.get("/api/metrics/live/<int:request_id>/trace")
@admin_required
def api_metrics_live_trace(request_id):
    trace = live_requests.trace(request_id)
    if trace is None:
        return jsonify({"error": "Request trace not found."}), 404
    return jsonify(trace)


@app.post("/api/metrics/requests/<int:request_id>/coding-review")
@admin_required
def api_metrics_coding_review(request_id):
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or "passed" not in body or (body["passed"] is not None and type(body["passed"]) is not bool):
        return jsonify({"error": "passed must be true, false, or null"}), 400
    if not metrics_store.review_coding(request_id, body["passed"]):
        return jsonify({"error": "Coding request not found"}), 404
    return jsonify({"ok": True})


def _require_localhost():
    """Gate for the internal /api/internal/live/* routes below: only the LAN-facing shims
    (same machine, no exception) may report into the live-activity registry. Not admin_required
    - the shims are unauthenticated localhost processes with no session/login of their own,
    same trust boundary lemonade_guard_shim.py/flm_tool_shim.py already sit inside of."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"error": "forbidden"}), 403
    return None


@app.post("/api/internal/live/start")
def api_internal_live_start():
    forbidden = _require_localhost()
    if forbidden:
        return forbidden
    body = request.get_json(force=True, silent=True) or {}
    req_id = live_requests.start(
        source=body.get("source") or "lan",
        backend=body.get("backend") or "unknown",
        model=body.get("model") or "unknown",
        max_tokens=body.get("max_tokens"),
        is_npu=bool(body.get("is_npu")),
        request_content=body.get("request_content"),
    )
    return jsonify({"id": req_id})


@app.post("/api/internal/live/update")
def api_internal_live_update():
    forbidden = _require_localhost()
    if forbidden:
        return forbidden
    body = request.get_json(force=True, silent=True) or {}
    req_id = body.get("id")
    if req_id is not None:
        live_requests.update(
            req_id, delta_chars=body.get("delta_chars") or 0,
            delta_content=body.get("delta_content"),
        )
    return jsonify({"ok": True})


@app.post("/api/internal/live/finish")
def api_internal_live_finish():
    forbidden = _require_localhost()
    if forbidden:
        return forbidden
    body = request.get_json(force=True, silent=True) or {}
    req_id = body.get("id")
    if req_id is not None:
        live_requests.finish(
            req_id, status=body.get("status") or "done",
            total_tokens=body.get("total_tokens"), status_code=body.get("status_code"),
            error_detail=body.get("error_detail"),
        )
    return jsonify({"ok": True})


@app.post("/api/internal/metrics/record")
def api_internal_metrics_record():
    forbidden = _require_localhost()
    if forbidden:
        return forbidden
    body = request.get_json(force=True, silent=True) or {}
    # Explicit allow-list: this localhost endpoint is only for the inline proxy shims and
    # must not become a way to write arbitrary database columns.
    metrics_store.record(**{
        key: body.get(key) for key in (
            "ts", "source", "backend", "model", "request_type", "ask_category",
            "duration_ms", "prompt_tokens", "completion_tokens", "total_tokens",
            "tokens_per_sec", "tops_used", "npu_percent", "status", "client_ip",
            "user_id", "is_npu",
        )
    })
    return jsonify({"ok": True})


@app.get("/api/quiz/decks")
@login_required
def api_quiz_decks():
    """The deck registry: built-in banks + this user's custom decks (metadata only)."""
    return jsonify({"decks": quiz_store.list_decks(session["user_id"])})


@app.post("/api/quiz/decks")
@admin_required
def api_quiz_create_deck():
    """Create a custom deck from user-authored questions. Admin-only."""
    body = request.get_json(silent=True) or {}
    name = body.get("name")
    questions = body.get("questions") or []
    row = quiz_store.create_deck(session["user_id"], name, questions, body.get("meta"))
    if not row:
        return jsonify({"error": "Provide a name and at least one valid question "
                                 "(prompt, 2–6 options, and a correct answer)."}), 400
    return jsonify({"deck": row})


@app.delete("/api/quiz/decks/<slug>")
@admin_required
def api_quiz_delete_deck(slug):
    ok = quiz_store.delete_deck(session["user_id"], slug)
    if not ok:
        return jsonify({"error": "Deck not found."}), 404
    return jsonify({"ok": True})


@app.get("/api/quiz/bank")
@login_required
def api_quiz_bank():
    """Full question bank (stable ids) for one deck — built-in or the user's custom."""
    slug = request.args.get("slug", "")
    bank = quiz_store.get_bank(slug, session["user_id"])
    if bank is None:
        return jsonify({"error": f"No deck '{slug}'."}), 404
    return jsonify(bank)


@app.post("/api/quiz/log")
@login_required
def api_quiz_log():
    """Persist in-chat quiz turns into a real conversation (same as /api/dmv/log, but for
    any deck). Keeps a practice test saved in chat history — sidebar-visible, reloadable."""
    body = request.get_json(silent=True) or {}
    conversation_id = body.get("conversation_id")
    turns = body.get("turns") or []
    title = (body.get("title") or "Practice test").strip()[:60]
    if not conversation_id or not turns:
        return jsonify({"error": "'conversation_id' and 'turns' are required"}), 400
    user_id = session["user_id"]
    chat_store.ensure_conversation(conversation_id, user_id, None, title)
    saved = 0
    for turn in turns:
        role = turn.get("role")
        content = (turn.get("content") or "").strip()
        if role in ("user", "assistant", "system") and content:
            chat_store.add_message(conversation_id, role, content)
            saved += 1
    return jsonify({"ok": True, "saved": saved})


def _collect_completion(messages: list[dict], model: str, temperature: float = 0.4,
                        max_tokens: int | None = 2048) -> str:
    """Run a non-streaming chat completion by draining the selected backend's SSE stream
    and concatenating the content deltas. Backend is chosen the same way /api/chat does."""
    if lemonade.owns_model(model):
        backend = lemonade
    elif vllm.owns_model(model):
        backend = vllm
    elif sglang.owns_model(model):
        backend = sglang
    elif kobold.owns_model(model):
        backend = kobold
    elif fastflowlm.owns_model(model):
        backend = fastflowlm
    else:
        backend = lms
    is_npu = backend is fastflowlm or (backend is lemonade and lemonade.is_npu_model(model))
    npu_handle = _claim_selected_npu(backend, model) if is_npu else None
    parts: list[str] = []
    try:
        for line in backend.chat_stream(messages, model, temperature=temperature, max_tokens=max_tokens):
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            try:
                delta = obj["choices"][0]["delta"].get("content")
            except (KeyError, IndexError):
                delta = None
            if delta:
                parts.append(delta)
    finally:
        npu_request_lock.release(npu_handle)
    return "".join(parts)


@app.post("/api/quiz/generate")
@login_required
def api_quiz_generate():
    """AI-generate a quiz on any topic using the currently-loaded model, returned in the
    same {name, meta, questions:[{id,q,options,answer,explain}]} shape as a deck bank."""
    body = request.get_json(silent=True) or {}
    topic = (body.get("topic") or "").strip()
    model = body.get("model")
    try:
        count = int(body.get("count") or 8)
    except (TypeError, ValueError):
        count = 8
    count = max(3, min(count, 20))
    if not topic:
        return jsonify({"error": "A topic is required."}), 400
    if not model:
        return jsonify({"error": "Load a model first — AI quiz generation uses the selected chat model."}), 400

    messages = quiz_store.build_generation_messages(topic, count)
    try:
        text = _collect_completion(messages, model, temperature=0.5, max_tokens=2400)
    except (lemonade.LemonadeError, vllm.VLLMError, sglang.SGLangError, lms.LMStudioError) as exc:
        return jsonify({"error": f"Generation failed: {exc}"}), 502
    questions = quiz_store.parse_generated(text, count)
    if not questions:
        return jsonify({"error": "The model didn't return a usable quiz. Try again, "
                                 "lower the count, or pick a more capable model."}), 422
    return jsonify({
        "slug": None,  # ephemeral: AI quizzes aren't saved as decks, just run + logged
        "name": f"Quiz: {topic[:60]}",
        "category": "ai",
        "meta": {"official_test_size": len(questions), "pass_percent": 70, "source": "AI-generated"},
        "questions": questions,
    })


def _cpu_temp_c() -> float | None:
    """Best-effort: sensor names/labels are hardware-specific and psutil.sensors_temperatures()
    doesn't exist at all on some platforms (e.g. macOS, some VMs), so this degrades to None
    rather than erroring — confirmed live on this machine's actual sensors (k10temp/Tctl is the
    AMD CPU die temp here) rather than guessing at a sensor name."""
    try:
        temps = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        return None
    for chip, entries in (temps or {}).items():
        for entry in entries:
            if chip == "k10temp" and entry.label in ("Tctl", "Tdie", ""):
                return round(entry.current, 1)
    return None


@app.get("/api/system-stats")
@admin_required
def api_system_stats():
    mem = psutil.virtual_memory()
    # Same partition BASE_DIR lives on, not necessarily "/" — this app's own data,
    # LM Studio's and Lemonade's model downloads all matter for "how much room is
    # left", and on this machine they're all under /home anyway (same filesystem as
    # this app), but disk_usage(path) is correct even if that ever changes.
    disk = psutil.disk_usage(str(BASE_DIR))
    return jsonify(
        {
            "cpu_percent": psutil.cpu_percent(),
            "cpu_count": psutil.cpu_count(),
            "cpu_temp_c": _cpu_temp_c(),
            "mem_percent": mem.percent,
            "mem_used_gb": round((mem.total - mem.available) / (1024**3), 1),
            "mem_total_gb": round(mem.total / (1024**3), 1),
            "disk_percent": disk.percent,
            "disk_free_gb": round(disk.free / (1024**3), 1),
            "disk_total_gb": round(disk.total / (1024**3), 1),
        }
    )


@app.get("/api/models")
@login_required
def api_list_models():
    # LM Studio — same fail-soft contract as the other backends below; it being
    # down should never block the rest of the models list from loading.
    try:
        models = lms.list_models()
    except lms.LMStudioError:
        models = []
    for m in models:
        m["backend"] = "lmstudio"
    # Best-effort: Lemonade (Ryzen AI NPU backend) not being installed/running is
    # normal, not an error — it just means that entry doesn't appear in the list.
    lemonade_models = lemonade.list_models()
    global_thinking = (settings_store.get("lemonade_enable_thinking") or "true") == "true"
    model_thinking = _lemonade_model_thinking_overrides()
    for m in lemonade_models:
        m["backend"] = "lemonade"
        m["enable_thinking"] = model_thinking.get(m.get("id"), global_thinking)
    models += lemonade_models
    # Best-effort, same as Lemonade above: runs locally via vllm.service (see
    # vllm_client's docstring), but the same fail-soft contract applies regardless —
    # that service being down for any reason should never break this whole endpoint.
    try:
        vllm_models = vllm.list_models()
    except vllm.VLLMError:
        vllm_models = []
    for m in vllm_models:
        m["backend"] = "vllm"
    models += vllm_models
    # SGLang is normally hosted on a separate multi-GPU machine. Discover the model
    # already served there; an offline/unconfigured endpoint is an empty group.
    try:
        sglang_models = sglang.list_models()
    except sglang.SGLangError:
        sglang_models = []
    for m in sglang_models:
        m["backend"] = "sglang"
    models += sglang_models
    # KoboldCPP — same fail-soft contract; a stopped kobold.service just omits its models.
    try:
        kobold_models = kobold.list_models()
    except Exception:
        kobold_models = []
    for m in kobold_models:
        m["backend"] = "kobold"
    models += kobold_models
    # FastFlowLM (standalone NPU) — same fail-soft contract.
    try:
        flm_models = fastflowlm.list_models()
    except Exception:
        flm_models = []
    for m in flm_models:
        m["backend"] = "fastflowlm"
    models += flm_models
    models.append(comfyui.video_model_info())
    def positive_context(value):
        if isinstance(value, bool):
            return None
        try:
            value = int(value)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    for model in models:
        if model.get("type") in ("image", "video", "transcription", "tts"):
            continue
        model_max = positive_context(model.get("max_context_length"))
        loaded_context = positive_context(model.get("loaded_context_length"))
        configured_context = positive_context(model.get("configured_context_length"))
        if model_max:
            model["context_window_tokens"] = model_max
            model["context_window_source"] = "model_max"
        elif loaded_context:
            model["context_window_tokens"] = loaded_context
            model["context_window_source"] = "loaded_runtime"
        elif configured_context:
            model["context_window_tokens"] = configured_context
            model["context_window_source"] = "configured"
        elif model.get("backend") == "fastflowlm":
            model["context_window_tokens"] = flm_context.context_limit(model.get("id"))
            model["context_window_source"] = "configured"
        else:
            model["context_window_tokens"] = help_data._CTX.get(model.get("backend"), 8192)
            model["context_window_source"] = "backend_default"
    if session.get("role") != "admin":
        # Not a chat target — picking any of these from the dropdown would just produce
        # broken responses (embedding/transcription/TTS models don't serve chat
        # completions at all). Admins still see them (Models panel needs to for
        # load/unload); regular users never do. Filtered server-side, not just hidden
        # client-side. By type rather than a hardcoded id so this covers whichever
        # backend is actually running each one (LM Studio's or Lemonade's) without
        # needing to know its exact id.
        models = [
            m for m in models
            if m.get("type") not in ("embeddings", "transcription", "tts", "image", "video")
        ]
    clients = {'lmstudio': lms, 'lemonade': lemonade, 'fastflowlm': fastflowlm,
               'vllm': vllm, 'kobold': kobold}
    local_backends = {name for name, client in clients.items()
                      if urlsplit(client.base_url()).hostname in ('localhost', '127.0.0.1', '::1')}
    annotated = model_resource_warnings.annotate(models, local_backends)
    try:
        successful_loads = model_metadata_store.successful_model_loads()
    except Exception:
        successful_loads = set()
    with _model_load_status_lock:
        for model in annotated:
            key = (model.get("backend", ""), str(model.get("id", "")).casefold())
            if model.get("state") == "loaded":
                _model_load_status.pop(key, None)
                if key not in successful_loads:
                    try:
                        model_metadata_store.mark_model_load_success(*key)
                        successful_loads.add(key)
                    except Exception as exc:
                        print(f"[models] Could not save observed loaded state for {model.get('id')}: {exc}", flush=True)
                model.pop("resource_warning", None)
            elif key in successful_loads:
                model.pop("resource_warning", None)
            status = _model_load_status.get(key)
            if status:
                model["load_status"] = dict(status)
    return jsonify({"models": annotated})


def _lemonade_model_thinking_overrides() -> dict[str, bool]:
    raw = settings_store.get("lemonade_model_thinking_overrides") or "{}"
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(model_id): value for model_id, value in data.items() if isinstance(model_id, str) and isinstance(value, bool)}


@app.post("/api/models/thinking")
@admin_required
def api_set_model_thinking():
    body = request.get_json(force=True, silent=True) or {}
    model_id = str(body.get("model") or "").strip()
    if not model_id or len(model_id) > 512:
        return jsonify({"error": "model is required"}), 400
    if not isinstance(body.get("enabled"), bool):
        return jsonify({"error": "enabled must be a boolean"}), 400
    overrides = _lemonade_model_thinking_overrides()
    overrides[model_id] = body["enabled"]
    settings_store.set("lemonade_model_thinking_overrides", json.dumps(overrides, separators=(",", ":")))
    return jsonify({"model": model_id, "enable_thinking": body["enabled"]})


_model_test_context = threading.local()
_model_test_registry = model_tests.Registry()
_agent_container_client = agent_container.ContainerOpenCode(
    Path(os.environ.get('HEARTH_AGENT_RUNTIME_DIR', database.DATA_DIR / 'agent-runtime')))
_agent_tasks = agent_tasks.Manager(database.DATA_DIR / 'agent-tasks', client=_agent_container_client,
    prepare_model=lambda job: _prepare_agent_model(job), embed_memory=_embed_agent_project_notes)
_agent_container_client.migrate_legacy_runtime()
_agent_container_client.restore_model_relays(_agent_tasks.list())
_agent_container_client.progress = _agent_tasks.report_progress


def _test_model_inventory():
    with app.test_request_context('/api/models'):
        session['role'] = 'admin'
        return api_list_models.__wrapped__().get_json()['models']


def _test_load_model(target):
    # Reuse the existing serialized load route and its memory/NPU eviction rules.
    # Authorization was checked on the HTTP start endpoint; this is internal work.
    _model_test_context.active = True
    try:
        with app.test_request_context('/api/models/load', method='POST', json={'model': target['id']}):
            response = app.make_response(api_load_model.__wrapped__())
            if response.status_code >= 400:
                raise RuntimeError((response.get_json() or {}).get('error', 'Model load failed'))
        # Do not send a test request until the requested model is actually the
        # loaded target. A backend accepting /load is not proof it switched, and
        # a silent unload failure must not contaminate another model's results.
        if target['backend'] != 'sglang':
            loaded = [m for m in _test_model_inventory()
                      if m.get('state') == 'loaded' and m.get('type') not in _SUPPORT_MODEL_TYPES
                      and m.get('backend') != 'sglang']
            expected = (target['backend'], target['id'])
            observed = [(m.get('backend'), m.get('id')) for m in loaded]
            if observed != [expected]:
                raise RuntimeError(f'Model handoff verification failed: expected only {expected}, found {observed}.')
    finally:
        _model_test_context.active = False


def _test_connection(target):
    backend = target['backend']
    client = {'fastflowlm': fastflowlm, 'lmstudio': lms, 'lemonade': lemonade,
              'vllm': vllm, 'sglang': sglang, 'kobold': kobold}[backend]
    base = client.base_url().rstrip('/')
    local = urlsplit(base)
    proxy_ports = {'fastflowlm': (52625, 52626), 'lmstudio': (1234, 13307), 'lemonade': (13305, 13306)}
    if backend in proxy_ports and local.hostname in ('127.0.0.1', 'localhost', '::1') and local.port == proxy_ports[backend][0]:
        base = 'http://127.0.0.1:' + str(proxy_ports[backend][1])
    model = target['id']
    if backend == 'fastflowlm':
        model = fastflowlm.resolve_model(model)
    elif backend == 'sglang':
        model = sglang._served_id(model)
    key = client.api_key()
    headers = {'X-Hearth-Client': 'model-tests'}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    return base + '/v1/chat/completions', model, headers


def _test_context(target):
    if target['backend'] == 'fastflowlm':
        return flm_context.context_limit(target['id'])
    client = {'lmstudio': lms, 'lemonade': lemonade, 'vllm': vllm, 'sglang': sglang, 'kobold': kobold}[target['backend']]
    for model in client.list_models():
        if model.get('id') == target['id']:
            window = model.get('loaded_context_length')
            if isinstance(window, int) and window > 0:
                return window
    return help_data._CTX.get(target['backend'], 8192)


def _prepare_agent_model(job):
    with _reasoning_model_load_lock:
        model = job['model']
        # Continuing with an already-serving FastFlowLM model is not a new model
        # load. Avoid a needless eviction/reload (and dependence on unrelated
        # backend availability).
        if (model['providerID'] == 'fastflowlm' and not fastflowlm.requests_paused()
                and fastflowlm.current_served_model() == model['id']):
            try:
                response = requests.get(fastflowlm.base_url()+'/v1/models',
                                        headers=fastflowlm._auth_headers(), timeout=3)
                response.raise_for_status()
                return
            except requests.RequestException:
                pass
        # _test_load_model unloads every active reasoning model before loading its
        # target. For an agent continuing with a healthy model, that eviction is
        # unnecessary and can strand the task if a backend's unload call stalls.
        client = {'lemonade': lemonade, 'lmstudio': lms, 'vllm': vllm,
                  'kobold': kobold, 'sglang': sglang}.get(model['providerID'])
        if client:
            try:
                loaded = next((entry for entry in client.list_models()
                               if entry.get('id') == model['id']
                               and entry.get('state') == 'loaded'), None)
                context = (loaded or {}).get('loaded_context_length')
                if loaded and (type(context) is not int or context >= 16384):
                    return
            except Exception:
                # Keep the existing explicit load path as the authoritative fallback
                # when a backend cannot confirm its current model state.
                pass
        _test_load_model({'backend': model['providerID'], 'id': model['id']})


@app.get('/agents')
@agent_workflow_required
def agents_page():
    return render_template('agents.html', username=session.get('username'),
                           page='list', initial_task_id=request.args.get('task', ''))


@app.get('/agents/new')
@agent_workflow_required
def agents_new_page():
    return render_template('agents.html', username=session.get('username'), page='new', initial_task_id='')


@app.get('/agents/schedules')
@agent_workflow_required
def agents_schedules_page():
    return render_template('agents.html', username=session.get('username'), page='schedules', initial_task_id='')


@app.get('/agents/<task_id>')
@agent_workflow_required
def agent_task_page(task_id):
    return render_template('agents.html', username=session.get('username'), page='execution', initial_task_id=task_id)


def _agent_task_visible_to_current_user(job):
    return bool(job) and (
        job.get('owner') == session.get('user_id')
        or session.get('user_id') in job.get('shared_with', [])
    )


def _agent_task_owned_by_current_user(job):
    return bool(job) and job.get('owner') == session.get('user_id')


def _agent_schedules_visible_to_current_user():
    visible_tasks = {job['id']: job for job in _agent_tasks.list()
                     if _agent_task_visible_to_current_user(job)}
    owners = {job['owner'] for job in visible_tasks.values()}
    schedules = []
    for owner_id in owners:
        for schedule in agent_store.list_schedules(owner_id):
            task_id = schedule.get('task_id') or schedule.get('project_task_id')
            if task_id in visible_tasks:
                schedules.append(schedule)
    return schedules


def _agent_schedule_visible_to_current_user(schedule_id):
    return next((schedule for schedule in _agent_schedules_visible_to_current_user()
                 if schedule['id'] == schedule_id), None)


def _public_agent_schedule(schedule):
    return {key: value for key, value in schedule.items() if key != 'owner'}


@app.get('/api/agent-tasks/models')
@agent_workflow_required
def api_agent_models():
    try:
        configured = _agent_tasks.client.models()
        inventory = api_list_models.__wrapped__().get_json()['models']
        # The inventory also contains support models (embeddings, speech, etc.),
        # and their ordering is backend-dependent. Mark every configured model that
        # is actually loaded so the picker can choose the loaded coding model rather
        # than stopping at the first unrelated loaded model.
        loaded = [m for m in inventory if m.get('state') == 'loaded' or m.get('loaded_instances')]
        for model in configured:
            if any(model.get('id') == entry.get('id') and
                   (not entry.get('backend') or model.get('providerID') == entry.get('backend'))
                   for entry in loaded):
                model['loaded'] = True
        return jsonify({'models': configured})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 503


@app.get('/api/agent-tasks')
@agent_workflow_required
def api_agent_tasks_list():
    fields = ('id', 'title', 'assignee', 'project', 'model', 'status', 'cycles', 'max_cycles', 'updated_at', 'error', 'schedule_id', 'scheduled_run_at')
    tasks = []
    for job in _agent_tasks.list():
        if not _agent_task_visible_to_current_user(job):
            continue
        task = {key: job.get(key) for key in fields}
        task['shared'] = not _agent_task_owned_by_current_user(job)
        task['shared_with'] = job.get('shared_with', []) if not task['shared'] else []
        if task['shared']:
            task['project'] = 'Shared project'
        activity = job.get('activity') or []
        task['latest_activity'] = str(activity[-1].get('text', ''))[:180] if activity else ''
        task['latest_activity_at'] = activity[-1].get('time') if activity else None
        tasks.append(task)
    return jsonify({'tasks': tasks})


@app.post('/api/agent-tasks')
@agent_workflow_required
def api_agent_tasks_create():
    if any(j['status'] in model_tests.ACTIVE for j in _model_test_registry.list()):
        return jsonify({'error': 'Wait for model validation to finish before assigning an agent.'}), 409
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({'error': 'An assignment object is required.'}), 400
    try:
        return jsonify(_agent_tasks.create(body, session['user_id'])), 201
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:
        return jsonify({'error': str(exc)}), 503


@app.get('/api/agent-tasks/<task_id>')
@agent_workflow_required
def api_agent_tasks_get(task_id):
    job = _agent_tasks.get(task_id)
    if not _agent_task_visible_to_current_user(job):
        return jsonify({'error': 'Assignment not found.'}), 404
    if not _agent_task_owned_by_current_user(job):
        job.pop('owner', None)
        job['shared'] = True
    return jsonify(job)


@app.get('/api/agent-tasks/<task_id>/live-messages')
@agent_workflow_required
def api_agent_tasks_live_messages(task_id):
    job = _agent_tasks.get(task_id)
    if not _agent_task_visible_to_current_user(job):
        return jsonify({'error': 'Assignment not found.'}), 404
    session_id = job.get('session_id')
    if not session_id:
        return jsonify({'messages': job.get('messages', []), 'streaming': False})
    try:
        return jsonify(_agent_live_payload(job))
    except Exception as exc:
        app.logger.warning('Live agent message fetch failed for %s: %s', task_id, exc)
        return jsonify({'messages': job.get('messages', []), 'streaming': job.get('status') in agent_tasks.ACTIVE,
                        'source': 'saved'}), 200


def _agent_live_payload(job):
    session_id = job.get('session_id')
    source = 'live'
    if not session_id:
        messages = job.get('messages', [])
    else:
        try:
            upstream = _agent_tasks.client.live_messages(session_id)
            messages = []
            for message in reversed(upstream[:50]):
                parts = []
                raw_time = message.get('time') or message.get('createdAt')
                message_time = raw_time
                if isinstance(raw_time, dict):
                    message_time = raw_time.get('created') or raw_time.get('updated')
                for part in message.get('content', []):
                    if part.get('type') == 'text':
                        parts.append({'type': 'text', 'text': part.get('text', '')[-12000:]})
                    elif part.get('type') == 'tool':
                        state = part.get('state') or {}
                        content = state.get('content') or []
                        output = '\n'.join(str(item.get('text', '')) for item in content
                                           if isinstance(item, dict) and item.get('type') == 'text')
                        tool_error = state.get('error')
                        if tool_error is not None and not isinstance(tool_error, str):
                            tool_error = json.dumps(tool_error, sort_keys=True)
                        parts.append({'type': 'tool', 'name': part.get('name'),
                                      'status': state.get('status'),
                                      'time': ((raw_time.get('completed') or raw_time.get('streamed') or raw_time.get('created'))
                                               if isinstance(raw_time, dict) else raw_time),
                                      'input': json.dumps(state.get('input') or {})[:2000],
                                      'output': output[-12000:],
                                      'error': tool_error[-4000:] if tool_error else None})
                messages.append({'id': message.get('id'), 'type': message.get('type'),
                                 'time': message_time, 'content': parts})
        except Exception as exc:
            app.logger.info('Using saved agent activity for %s because its live container is unavailable: %s',
                            job.get('id'), type(exc).__name__)
            messages = job.get('messages', [])
            source = 'saved'
        if not messages and job.get('messages'):
            # A live session can briefly return no messages during reconnects even
            # though earlier assistant output is already persisted with the task.
            messages = job.get('messages', [])
            source = 'saved'
    activity = job.get('activity') or []
    return {
        'id': job['id'], 'messages': messages,
        'streaming': job.get('status') in agent_tasks.ACTIVE,
        'status': job.get('status'), 'activity': activity[-50:], 'source': source,
        # These fields change after the page first loads. Include them in each SSE
        # snapshot so the result pane receives the final response and verification,
        # instead of keeping the initial (often empty) task detail forever.
        'summary': job.get('summary'), 'completion': job.get('completion'),
        'verification': job.get('verification'), 'error': job.get('error'),
        'result_label': job.get('result_label'), 'result_platform': job.get('result_platform', 'general'),
    }


@app.get('/api/agent-tasks/<task_id>/events')
@agent_workflow_required
def api_agent_tasks_events(task_id):
    job = _agent_tasks.get(task_id)
    if not _agent_task_visible_to_current_user(job):
        return jsonify({'error': 'Assignment not found.'}), 404

    @stream_with_context
    def event_stream():
        last_payload = None
        last_heartbeat = time.monotonic()
        deadline = time.monotonic() + 300
        yield 'retry: 1500\n\n'
        while time.monotonic() < deadline:
            current = _agent_tasks.get(task_id)
            if not _agent_task_visible_to_current_user(current):
                yield 'event: access-revoked\ndata: {}\n\n'
                return
            try:
                payload = _agent_live_payload(current)
                encoded = json.dumps(payload, separators=(',', ':'))
                if encoded != last_payload:
                    yield 'data: '+encoded+'\n\n'
                    last_payload = encoded
                elif time.monotonic() - last_heartbeat > 15:
                    yield ': keep-alive\n\n'
                    last_heartbeat = time.monotonic()
            except Exception as exc:
                app.logger.debug('Agent event stream read failed for %s: %s', task_id, exc)
                yield 'event: stream-error\ndata: {}\n\n'
                time.sleep(1)
            if current.get('status') not in agent_tasks.ACTIVE:
                return
            time.sleep(0.5)

    return Response(event_stream(), mimetype='text/event-stream', headers={
        'Cache-Control': 'no-cache, no-transform', 'X-Accel-Buffering': 'no',
        'Connection': 'keep-alive',
    })


@app.delete('/api/agent-tasks/<task_id>')
@agent_workflow_required
def api_agent_tasks_delete(task_id):
    if not _agent_task_visible_to_current_user(_agent_tasks.get(task_id)):
        return jsonify({'error': 'Assignment not found.'}), 404
    try:
        _agent_tasks.delete(task_id)
        return jsonify({'deleted': True})
    except KeyError:
        return jsonify({'error': 'Assignment not found.'}), 404
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 409
    except Exception as exc:
        app.logger.exception('Could not fully remove the agent assignment container')
        return jsonify({'error': f'Assignment container cleanup failed: {exc}'}), 503


@app.get('/api/agent-tasks/<task_id>/download')
@agent_workflow_required
def api_agent_tasks_download(task_id):
    if not _agent_task_visible_to_current_user(_agent_tasks.get(task_id)):
        return jsonify({'error': 'Assignment not found.'}), 404
    try:
        stream, name = _agent_tasks.download(task_id)
        filename = secure_filename(name) or 'completed-project'
        return send_file(stream, mimetype='application/gzip', as_attachment=True,
                         download_name=filename+'.tar.gz', max_age=0)
    except KeyError:
        return jsonify({'error': 'Assignment not found.'}), 404
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 409


@app.get('/api/agent-tasks/<task_id>/preview')
@agent_workflow_required
def api_agent_tasks_preview(task_id):
    job = _agent_tasks.get(task_id)
    if not _agent_task_visible_to_current_user(job):
        return jsonify({'error': 'Assignment not found.'}), 404
    platform = job.get('result_platform', 'general')
    if platform not in ('web', 'report'):
        return jsonify({'error': 'This assignment is not configured for an HTML result.'}), 409
    if platform == 'report' and job.get('status') != 'completed':
        return jsonify({'error': 'The accepted agent result is not available until the assignment completes.'}), 409
    files = agent_store.list_workspace_files(job['owner'], workspace_id=job['id'])
    if platform == 'web':
        entry = next((item for item in files if not item['is_dir'] and
                      str(item['path']).removeprefix('./') in
                      ('index.html', 'index.htm', 'dist/index.html', 'build/index.html')), None)
        if not entry:
            return jsonify({'error': 'No supported HTML entry file was found in the project.'}), 404
        content = bytes(entry['content'])
    else:
        json_files = [item for item in files if not item['is_dir'] and item['path'].lower().endswith('.json')]
        preferred = {'results.json': 0, 'result.json': 1, 'report.json': 2, 'scan.json': 3}
        json_files.sort(key=lambda item: (
            preferred.get(Path(item['path']).name.lower(), 10),
            -float(item.get('updated_at') or 0), item['path'].lower()))
        report = None
        report_name = None
        report_found = False
        for item in json_files:
            if len(item['content']) > 10 * 1024 * 1024:
                continue
            try:
                report = json.loads(bytes(item['content']).decode('utf-8'))
                report_name = item['path']
                report_found = True
                break
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        if not report_found:
            final_text = str(job.get('summary') or job.get('completion') or '').strip()
            if not final_text:
                return jsonify({'error': 'No saved JSON report or accepted final response is available.'}), 404
            try:
                report = json.loads(final_text)
            except json.JSONDecodeError:
                report = {'final_response': final_text}
            report_name = 'Accepted agent final response'
        label = html.escape(str(job.get('result_label') or 'Results'))
        heading = html.escape(report_name or 'Results')
        rendered = json.dumps(report, ensure_ascii=False, indent=2)
        content = ("<!doctype html><html lang='en'><meta charset='utf-8'>"
                   "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                   f"<title>{label}</title><style>body{{font:15px system-ui,sans-serif;margin:24px;color:#202124}}"
                   "h1{font-size:1.35rem}p{color:#555}pre{white-space:pre-wrap;overflow-wrap:anywhere;"
                   "padding:16px;background:#f4f5f7;border-radius:8px}</style>"
                   f"<h1>{label}</h1><p>{heading} · accepted agent result</p>"
                   f"<pre>{html.escape(rendered)}</pre></html>").encode('utf-8')
    response = Response(content, mimetype='text/html')
    response.headers['Content-Security-Policy'] = "default-src 'none'; img-src data: blob:; style-src 'unsafe-inline'; font-src data:; sandbox"
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.post('/api/agent-tasks/<task_id>/control')
@agent_workflow_required
def api_agent_tasks_control(task_id):
    if not _agent_task_visible_to_current_user(_agent_tasks.get(task_id)):
        return jsonify({'error': 'Assignment not found.'}), 404
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({'error': 'A control object is required.'}), 400
    if body.get('action') == 'resume' and any(j['status'] in model_tests.ACTIVE for j in _model_test_registry.list()):
        return jsonify({'error': 'Wait for model validation before resuming an agent.'}), 409
    try:
        return jsonify(_agent_tasks.control(task_id, body.get('action')))
    except KeyError:
        return jsonify({'error': 'Assignment not found.'}), 404
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 409
    except Exception as exc:
        app.logger.exception('Could not stop or remove the agent container')
        return jsonify({'error': f'Agent container cleanup failed: {exc}'}), 503


@app.post('/api/agent-tasks/<task_id>/rerun')
@agent_workflow_required
def api_agent_tasks_rerun(task_id):
    if not _agent_task_visible_to_current_user(_agent_tasks.get(task_id)):
        return jsonify({'error': 'Assignment not found.'}), 404
    if any(j['status'] in model_tests.ACTIVE for j in _model_test_registry.list()):
        return jsonify({'error': 'Wait for model validation to finish before rerunning an agent.'}), 409
    try:
        if not _agent_tasks.thread or not _agent_tasks.thread.is_alive():
            _agent_tasks.start_worker(lambda: not any(
                job['status'] in model_tests.ACTIVE for job in _model_test_registry.list()
            ))
        if not _agent_tasks.thread or not _agent_tasks.thread.is_alive():
            app.logger.error('Rerun requested but the persistent agent supervisor did not start.')
            return jsonify({'error': 'The agent supervisor is not running; the completed task was not queued.'}), 503
        return jsonify(_agent_tasks.rerun(task_id))
    except KeyError:
        return jsonify({'error': 'Assignment not found.'}), 404
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 409
    except Exception as exc:
        app.logger.exception('Could not rerun the completed agent assignment')
        return jsonify({'error': f'Could not rerun assignment: {exc}'}), 503


@app.post('/api/agent-tasks/<task_id>/edit-rerun')
@agent_workflow_required
def api_agent_tasks_edit_rerun(task_id):
    if not _agent_task_visible_to_current_user(_agent_tasks.get(task_id)):
        return jsonify({'error': 'Assignment not found.'}), 404
    if any(j['status'] in model_tests.ACTIVE for j in _model_test_registry.list()):
        return jsonify({'error': 'Wait for model validation to finish before resubmitting an agent.'}), 409
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({'error': 'Updated task details are required.'}), 400
    try:
        if not _agent_tasks.thread or not _agent_tasks.thread.is_alive():
            _agent_tasks.start_worker(lambda: not any(
                job['status'] in model_tests.ACTIVE for job in _model_test_registry.list()
            ))
        if not _agent_tasks.thread or not _agent_tasks.thread.is_alive():
            return jsonify({'error': 'The agent supervisor is not running; the task was not queued.'}), 503
        return jsonify(_agent_tasks.edit_and_rerun(task_id, body))
    except KeyError:
        return jsonify({'error': 'Assignment not found.'}), 404
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 409
    except Exception as exc:
        app.logger.exception('Could not update and resubmit the agent assignment')
        return jsonify({'error': f'Could not update and resubmit assignment: {exc}'}), 503


@app.patch('/api/agent-tasks/<task_id>/settings')
@agent_workflow_required
def api_agent_tasks_settings(task_id):
    if not _agent_task_visible_to_current_user(_agent_tasks.get(task_id)):
        return jsonify({'error': 'Assignment not found.'}), 404
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({'error': 'Updated task details are required.'}), 400
    try:
        return jsonify(_agent_tasks.update_assignment(task_id, body))
    except KeyError:
        return jsonify({'error': 'Assignment not found.'}), 404
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 409
    except Exception as exc:
        app.logger.exception('Could not save agent assignment settings')
        return jsonify({'error': f'Could not save assignment settings: {exc}'}), 503


@app.patch('/api/agent-tasks/<task_id>/model')
@agent_workflow_required
def api_agent_tasks_model(task_id):
    if not _agent_task_visible_to_current_user(_agent_tasks.get(task_id)):
        return jsonify({'error': 'Assignment not found.'}), 404
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(_agent_tasks.change_model(task_id, body.get('model') if isinstance(body, dict) else None))
    except KeyError:
        return jsonify({'error': 'Assignment not found.'}), 404
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 409
    except Exception as exc:
        app.logger.exception('Could not change the agent model')
        return jsonify({'error': f'Could not change agent model: {exc}'}), 503


@app.post('/api/agent-tasks/<task_id>/steer')
@agent_workflow_required
def api_agent_tasks_steer(task_id):
    if not _agent_task_visible_to_current_user(_agent_tasks.get(task_id)):
        return jsonify({'error': 'Assignment not found.'}), 404
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(_agent_tasks.steer(task_id, body.get('text') if isinstance(body, dict) else None))
    except KeyError:
        return jsonify({'error': 'Assignment not found.'}), 404
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 409


@app.post('/api/agent-tasks/<task_id>/reply')
@agent_workflow_required
def api_agent_tasks_reply(task_id):
    if not _agent_task_visible_to_current_user(_agent_tasks.get(task_id)):
        return jsonify({'error': 'Assignment not found.'}), 404
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({'error': 'A reply object is required.'}), 400
    try:
        return jsonify(_agent_tasks.reply(task_id, body.get('kind'), body.get('id'), body.get('value')))
    except KeyError:
        return jsonify({'error': 'Assignment not found.'}), 404
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 409
    except Exception as exc:
        return jsonify({'error': str(exc)}), 502


@app.get('/api/agent-tasks/share-targets')
@agent_workflow_required
def api_agent_share_targets():
    query = request.args.get('q', '').strip()
    ids_value = request.args.get('ids')
    if ids_value is not None:
        user_ids = list(dict.fromkeys(value for value in ids_value.split(',') if value))
        if len(user_ids) > 100:
            return jsonify({'error': 'Resolve up to 100 selected users at a time.'}), 400
        users = users_store.list_agent_share_targets(session['user_id'], user_ids=user_ids)
        return jsonify({'users': users})
    if len(query) < 2:
        return jsonify({'users': [], 'min_query_length': 2})
    users = users_store.list_agent_share_targets(session['user_id'], query=query, limit=20)
    return jsonify({'users': users, 'min_query_length': 2})


@app.put('/api/agent-tasks/<task_id>/shares')
@agent_workflow_required
def api_agent_task_shares(task_id):
    job = _agent_tasks.get(task_id)
    if not _agent_task_visible_to_current_user(job):
        return jsonify({'error': 'Assignment not found.'}), 404
    body = request.get_json(silent=True)
    user_ids = body.get('user_ids') if isinstance(body, dict) else None
    if not isinstance(user_ids, list) or any(not isinstance(user_id, str) for user_id in user_ids):
        return jsonify({'error': 'user_ids must be a list of registered user IDs.'}), 400
    if len(user_ids) > 100 or len(set(user_ids)) != len(user_ids):
        return jsonify({'error': 'Choose up to 100 distinct users.'}), 400
    if not users_store.agent_share_targets_exist(session['user_id'], user_ids):
        return jsonify({'error': 'Assignments can only be shared with registered users who have agent workflow enabled.'}), 400
    usernames = {user['id']: user['username'] for user in users_store.list_agent_share_targets(
        session['user_id'], user_ids=user_ids, limit=100
    )}
    with _agent_tasks.lock:
        current = _agent_tasks.jobs.get(task_id)
        if not _agent_task_visible_to_current_user(current):
            return jsonify({'error': 'Assignment not found.'}), 404
        current['shared_with'] = user_ids
        _agent_tasks.save(current)
        return jsonify({'shared_with': [{'id': user_id, 'username': usernames[user_id]} for user_id in user_ids]})


@app.get('/api/agent-schedules')
@agent_workflow_required
def api_agent_schedules_list():
    return jsonify({'schedules': [_public_agent_schedule(item)
                                  for item in _agent_schedules_visible_to_current_user()]})


@app.post('/api/agent-schedules')
@agent_workflow_required
def api_agent_schedules_create():
    if any(job['status'] in model_tests.ACTIVE for job in _model_test_registry.list()):
        return jsonify({'error': 'Wait for model validation to finish before scheduling an agent.'}), 409
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({'error': 'A schedule configuration is required.'}), 400
    try:
        task = _agent_tasks.get(str(body.get('task_id', '')).strip())
        if not _agent_task_visible_to_current_user(task):
            return jsonify({'error': 'Assignment not found.'}), 404
        return jsonify(_public_agent_schedule(_agent_tasks.create_schedule(body, task['owner']))), 201
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:
        return jsonify({'error': str(exc)}), 503


@app.patch('/api/agent-schedules/<schedule_id>')
@agent_workflow_required
def api_agent_schedules_update(schedule_id):
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({'error': 'A schedule update is required.'}), 400
    visible = _agent_schedule_visible_to_current_user(schedule_id)
    if not visible:
        return jsonify({'error': 'Schedule not found.'}), 404
    if type(body.get('enabled')) is not bool:
        return jsonify({'error': 'enabled must be a boolean.'}), 400
    schedule = _agent_tasks.set_schedule_enabled(schedule_id, visible['owner'], body['enabled'])
    return (jsonify(_public_agent_schedule(schedule)), 200) if schedule else (jsonify({'error': 'Schedule not found.'}), 404)


@app.put('/api/agent-schedules/<schedule_id>')
@agent_workflow_required
def api_agent_schedules_edit(schedule_id):
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({'error': 'A schedule configuration is required.'}), 400
    visible = _agent_schedule_visible_to_current_user(schedule_id)
    if not visible:
        return jsonify({'error': 'Schedule not found.'}), 404
    try:
        schedule = _agent_tasks.update_schedule(schedule_id, visible['owner'], body)
        return (jsonify(_public_agent_schedule(schedule)), 200) if schedule else (jsonify({'error': 'Schedule not found.'}), 404)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400


@app.delete('/api/agent-schedules/<schedule_id>')
@agent_workflow_required
def api_agent_schedules_delete(schedule_id):
    visible = _agent_schedule_visible_to_current_user(schedule_id)
    if not visible or not _agent_tasks.delete_schedule(schedule_id, visible['owner']):
        return jsonify({'error': 'Schedule not found.'}), 404
    return jsonify({'deleted': True})


@app.get('/api/model-tests')
@admin_required
def api_model_tests_list():
    return jsonify({'jobs': [{k: v for k, v in job.items() if k not in ('results', 'repairs')}
                             for job in _model_test_registry.list()]})


@app.post('/api/model-tests')
@admin_required
def api_model_tests_start():
    if _agent_tasks.busy():
        return jsonify({'error': 'Pause or stop assigned agents before running model validation.'}), 409
    body = request.get_json(silent=True) or {}
    requested = body.get('models')
    if not isinstance(requested, list) or not 1 <= len(requested) <= 128:
        return jsonify({'error': 'Select between 1 and 128 installed chat models.'}), 400
    if not isinstance(body.get('auto_repair', False), bool):
        return jsonify({'error': 'auto_repair must be a boolean.'}), 400
    suite = body.get('suite', 'all')
    if suite not in ('all', 'tools', 'iq', 'security'):
        return jsonify({'error': 'Choose a supported model test suite.'}), 400
    inventory = {(m.get('backend'), m.get('id')): m for m in _test_model_inventory()}
    targets = []
    seen = set()
    for item in requested:
        if not isinstance(item, dict) or not isinstance(item.get('backend'), str) or not isinstance(item.get('id'), str):
            return jsonify({'error': 'Invalid model selection.'}), 400
        pair = (item['backend'], item['id'])
        model = inventory.get(pair)
        if (not model or pair in seen or model.get('type') in _SUPPORT_MODEL_TYPES
                or model.get('loadable') is False or pair[0] not in ('fastflowlm', 'lmstudio', 'lemonade', 'vllm', 'sglang', 'kobold')):
            return jsonify({'error': 'Choose unique, installed, loadable chat models.'}), 400
        seen.add(pair)
        targets.append({'backend': pair[0], 'id': pair[1]})
    try:
        job = _model_test_registry.start(targets, body.get('auto_repair', False),
            _test_load_model, _test_connection, _test_context, suite=suite)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 409
    return jsonify(job), 202


@app.get('/api/model-tests/<job_id>')
@admin_required
def api_model_tests_get(job_id):
    job = _model_test_registry.get(job_id)
    if job is None:
        return jsonify({'error': 'Test run not found.'}), 404
    response = jsonify(job)
    if request.args.get('download') == '1':
        response.headers['Content-Disposition'] = 'attachment; filename="model-test-' + job['id'] + '.json"'
    return response


@app.post('/api/model-tests/<job_id>/cancel')
@admin_required
def api_model_tests_cancel(job_id):
    try:
        return jsonify(_model_test_registry.cancel(job_id))
    except KeyError:
        return jsonify({'error': 'Test run not found.'}), 404
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 409


@app.delete('/api/model-tests/<job_id>')
@admin_required
def api_model_tests_delete(job_id):
    try:
        _model_test_registry.delete(job_id)
        return jsonify({'deleted': True})
    except KeyError:
        return jsonify({'error': 'Test run not found.'}), 404
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 409


@app.post('/api/model-tests/reset-profile')
@admin_required
def api_model_tests_reset_profile():
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get('backend'), str) or not isinstance(body.get('model'), str):
        return jsonify({'error': 'backend and model are required.'}), 400
    if any(job['status'] in model_tests.ACTIVE for job in _model_test_registry.list()):
        return jsonify({'error': 'Wait for the active test run to finish before resetting profiles.'}), 409
    if not any(m.get('id') == body['model'] and m.get('backend') == body['backend'] for m in _test_model_inventory()):
        return jsonify({'error': 'Model not found.'}), 404
    model_test_profiles.set_profile(body['backend'], body['model'], None)
    return jsonify({'ok': True})


def _free_npu_except(keep: str, *, npu_lock_held: bool = False) -> None:
    """FastFlowLM and Lemonade both drive the one NPU and can't hold it at once. Before
    activating one, release the other: `keep` is the backend about to use the NPU."""
    if keep != "fastflowlm":
        try:
            fastflowlm.unload_model(
                npu_lock_held=npu_lock_held
            )  # stop flm.service, freeing the NPU
        except Exception:
            pass
    if keep != "lemonade":
        try:
            for m in lemonade.list_models():
                if m.get("state") == "loaded" and lemonade.is_npu_model(m.get("id") or ""):
                    lemonade.unload_model(m["id"])
        except Exception:
            pass


def _lemonade_is_npu(model_key: str) -> bool:
    """True if a Lemonade model is an flm-recipe (NPU) model — those contend with FastFlowLM."""
    try:
        return lemonade.is_npu_model(model_key)
    except Exception:
        return False


def _unload_reasoning_models_before_load(requested: str, *, npu_lock_held: bool = False) -> list[str]:
    """Unload every chat/reasoning model before a new load starts.

    Support models cannot answer chat requests and are intentionally allowed to
    coexist.  The requested model is deliberately *not* exempt: several runtimes
    treat loading an already-loaded model as a second instance or an in-place
    switch, so the unload-before-load ordering must apply to reloads too.
    Inventory failures abort the load because otherwise the invariant cannot be
    guaranteed.
    """
    backends = (
        ("LM Studio", lms.list_models, lms.unload_model),
        ("Lemonade", lemonade.list_models, lemonade.unload_model),
        ("vLLM", vllm.list_models, vllm.unload_model),
        ("KoboldCPP", kobold.list_models, kobold.unload_model),
        ("FastFlowLM", fastflowlm.list_models, fastflowlm.unload_model),
    )
    inventories = []
    for label, list_models, unload_model in backends:
        try:
            models = list_models()
        except Exception as exc:
            raise RuntimeError(f"Could not verify loaded models in {label}: {exc}") from exc
        inventories.append((label, models, unload_model))

    # Loading an embedding or media helper must not evict the active reasoning model.
    if any(
        model.get("id") == requested and model.get("type") in _SUPPORT_MODEL_TYPES
        for _, models, _ in inventories
        for model in models
    ):
        return []

    unloaded: list[str] = []
    for label, models, unload_model in inventories:
        for model in models:
            model_id = model.get("id")
            if (
                model.get("state") == "loaded"
                and model_id
                and model.get("type") not in _SUPPORT_MODEL_TYPES
            ):
                try:
                    if label == "FastFlowLM":
                        # Internal handoffs must not mark a model manually paused or
                        # reacquire an NPU lock already owned by the load endpoint.
                        handle = None if npu_lock_held else npu_request_lock.acquire(timeout=15)
                        try:
                            unload_model(model_id, npu_lock_held=True)
                        finally:
                            npu_request_lock.release(handle)
                    else:
                        unload_model(model_id)
                except Exception as exc:
                    raise RuntimeError(f"Could not unload '{model_id}' from {label}: {exc}") from exc
                unloaded.append(model_id)
    return unloaded


def _claim_selected_npu(backend, model: str):
    """Give the selected backend exclusive NPU ownership for the complete response."""
    # This path is used by chat, quizzes and startup as well as the explicit Load API.
    # Preflight before acquiring the machine-wide lock or stopping another NPU backend;
    # a blocked local engine profile must be side-effect free everywhere.
    if backend is fastflowlm:
        fastflowlm.check_loadable(model)
    handle = npu_request_lock.acquire()
    try:
        if backend is fastflowlm:
            _free_npu_except("fastflowlm", npu_lock_held=True)
            active = (fastflowlm.current_served_model() or "").removeprefix("flm/")
            if active != fastflowlm.resolve_model(model):
                # FastFlowLM otherwise switches weights lazily on the next request.
                # Make that implicit traffic-driven load obey the same unload-first
                # rule as an explicit Models-page load.
                fastflowlm.unload_model(npu_lock_held=True)
            fastflowlm.load_model(model)
        elif backend is lemonade:
            _free_npu_except("lemonade", npu_lock_held=True)
            loaded = any(
                m.get("id") == model and m.get("state") == "loaded"
                for m in lemonade.list_models()
            )
            if not loaded:
                lemonade.load_model(model, context_length=65536)
        return handle
    except Exception:
        npu_request_lock.release(handle)
        raise


def _track_model_load_status(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        body = request.get_json(force=True, silent=True) or {}
        model_id = body.get("model")
        if not isinstance(model_id, str) or not model_id:
            return view(*args, **kwargs)
        backend = next((name for name, client in (
            ("sglang", sglang), ("fastflowlm", fastflowlm), ("vllm", vllm),
            ("kobold", kobold), ("lemonade", lemonade),
        ) if client.owns_model(model_id)), "lmstudio")
        key = (backend, model_id.casefold())
        with _model_load_status_lock:
            _model_load_status[key] = {"state": "loading", "updated_at": time.time()}
        try:
            result = view(*args, **kwargs)
            response = app.make_response(result)
            payload = response.get_json(silent=True) or {}
            with _model_load_status_lock:
                if response.status_code < 400 and payload.get("ok"):
                    _model_load_status.pop(key, None)
                    try:
                        model_metadata_store.mark_model_load_success(backend, model_id)
                    except Exception as exc:
                        print(f"[models] Could not save successful load state for {model_id}: {exc}", flush=True)
                else:
                    try:
                        model_metadata_store.clear_model_load_success(backend, model_id)
                    except Exception as exc:
                        print(f"[models] Could not clear successful load state for {model_id}: {exc}", flush=True)
                    _model_load_status[key] = {
                        "state": "error",
                        "error": str(payload.get("error") or f"Load failed (HTTP {response.status_code})"),
                        "updated_at": time.time(),
                    }
            return result
        except Exception as exc:
            with _model_load_status_lock:
                _model_load_status[key] = {
                    "state": "error", "error": str(exc), "updated_at": time.time(),
                }
            raise
    return wrapped


@app.post("/api/models/load")
@admin_required
@_serialized_reasoning_model_load
@_track_model_load_status
def api_load_model():
    if not getattr(_model_test_context, 'active', False) and _agent_tasks.busy():
        return jsonify({'error': 'Pause or stop assigned agents before changing models.'}), 409
    if not getattr(_model_test_context, 'active', False) and any(j['status'] in model_tests.ACTIVE for j in _model_test_registry.list()):
        return jsonify({'error': 'A model test run is active. Cancel it or wait before changing models.'}), 409
    body = request.get_json(force=True, silent=True) or {}
    model_key = body.get("model")
    if not model_key:
        return jsonify({"error": "'model' is required"}), 400
    if sglang.owns_model(model_key):
        try:
            sglang.verify_model(model_key)
        except sglang.SGLangError as exc:
            return jsonify({"error": str(exc)}), 502
        return jsonify({"ok": True, "unloaded": [], "externally_managed": True})
    if fastflowlm.owns_model(model_key):
        # Reject incomplete engine-development profiles before unloading the working
        # reasoning model. Once kernels/catalog registration exist, the ordinary
        # single-model eviction and NPU handoff below applies unchanged.
        try:
            fastflowlm.check_loadable(model_key)
        except fastflowlm.FastFlowLMError as exc:
            return jsonify({"error": str(exc)}), 422
        npu_handle = npu_request_lock.acquire()
        try:
            # Free the NPU from Lemonade first (mutually exclusive), then activate FLM.
            _free_npu_except("fastflowlm", npu_lock_held=True)
            unloaded = _unload_reasoning_models_before_load(model_key, npu_lock_held=True)
            # Checked after eviction so the released memory counts.
            refusal = resource_guard.check_available_memory()
            if refusal:
                return jsonify({"error": refusal}), 422
            fastflowlm.load_model(model_key)
        except (fastflowlm.FastFlowLMError, RuntimeError) as exc:
            return jsonify({"error": str(exc)}), 502
        finally:
            npu_request_lock.release(npu_handle)
        return jsonify({"ok": True, "unloaded": unloaded})
    if vllm.owns_model(model_key):
        # Pre-flight checks before this ever reaches vllm.service — each guards a real
        # crash-loop incident (Restart=on-failure turns a bad load into an infinite
        # retry that also evicts the working model). check_servable: GGUF-only repos
        # vLLM structurally can't serve (see its docstring). check_safe_to_load:
        # models too big for RAM.
        refusal = (
            vllm.check_servable(model_key)
            or vllm.check_safe_to_load(model_key)
        )
        if refusal:
            return jsonify({"error": refusal}), 422
        # vLLM has no runtime load API of its own — vllm_client.load_model() builds one
        # on top of restarting its user-level systemd unit with a different model.
        try:
            unloaded = _unload_reasoning_models_before_load(model_key)
            refusal = resource_guard.check_available_memory()
            if refusal:
                return jsonify({"error": refusal}), 422
            vllm.load_model(
                model_key,
                max_model_len=body.get("context_length") or None,
            )
        except (vllm.VLLMError, RuntimeError) as exc:
            return jsonify({"error": str(exc)}), 502
        return jsonify({"ok": True, "unloaded": unloaded})
    if kobold.owns_model(model_key):
        # KoboldCPP, like vLLM, has no runtime load API — kobold_client.load_model()
        # rewrites its systemd unit's model path and restarts it.
        refusal = kobold.check_safe_to_load(model_key)
        if refusal:
            return jsonify({"error": refusal}), 422
        try:
            unloaded = _unload_reasoning_models_before_load(model_key)
            refusal = resource_guard.check_available_memory()
            if refusal:
                return jsonify({"error": refusal}), 422
            kobold.load_model(model_key)
        except (kobold.KoboldError, RuntimeError) as exc:
            return jsonify({"error": str(exc)}), 502
        return jsonify({"ok": True, "unloaded": unloaded})
    if lemonade.owns_model(model_key):
        # Pre-flight size check before this ever reaches Lemonade/llama-server - see
        # check_safe_to_load()'s docstring for the real incident (a 211GB GLM-5.2 quant
        # on a 43.7GB-RAM machine) that motivated refusing here instead of finding out
        # via a crashed GPU driver / unresponsive service.
        refusal = lemonade.check_safe_to_load(model_key)
        if refusal:
            return jsonify({"error": refusal}), 422
        # If this is a Lemonade NPU (flm-recipe) model, release the NPU from standalone
        # FastFlowLM first — they can't both hold it.
        use_npu = _lemonade_is_npu(model_key)
        npu_handle = npu_request_lock.acquire() if use_npu else None
        # Lemonade (Ryzen AI NPU backend) manages its own lifecycle over REST.
        #
        # Real incident (2026-07-26): this fallback was still 32768, left over from before
        # the context ceiling was raised to 65536 (help_data.py's _CTX, flm.service's
        # --ctx-len, recipe_options.json's stored ctx_size). An explicit context_length
        # passed to load_model() overrides whatever recipe_options.json has stored, so
        # every load through this app's own UI/API (not just ones with a custom context
        # request) was silently reloading the model at 32768 regardless of the 65536
        # recipe_options.json/help_data.py both advertise. OpenCode/Continue, trusting the
        # advertised 65536, then sent a request that fit under that but exceeded the real
        # loaded model's actual 32768 window - confirmed live in lemond's own log
        # ("StreamingProxy: Backend returned error: 400") right as a mid-session "run it
        # and fix the issues" turn (with a full session's worth of accumulated context)
        # went out, which is what silently broke OpenCode's turn. Must match _CTX["lemonade"]
        # in help_data.py, or this exact drift returns.
        try:
            if use_npu:
                _free_npu_except("lemonade", npu_lock_held=True)
            unloaded = _unload_reasoning_models_before_load(model_key, npu_lock_held=use_npu)
            # Checked after eviction so the released memory counts.
            refusal = resource_guard.check_available_memory()
            if refusal:
                return jsonify({"error": refusal}), 422
            lemonade.load_model(model_key, context_length=body.get("context_length") or 65536)
        except (lemonade.LemonadeError, RuntimeError) as exc:
            return jsonify({"error": str(exc)}), 502
        finally:
            npu_request_lock.release(npu_handle)
        return jsonify({"ok": True, "unloaded": unloaded})
    try:
        # Only one model may be loaded at a time (avoids overloading the machine) —
        # unload whatever else is currently loaded before loading the requested one.
        # The requested model is included in the unload pass: `lms load` does not
        # treat re-loading an already-loaded model as a no-op and can otherwise spin
        # up a second parallel instance.
        # The embedding model (cross-chat memory) is exempt: it's ~80MB, coexists fine
        # alongside any chat model, and switching chat models shouldn't silently kill it.
        unloaded = _unload_reasoning_models_before_load(model_key)

        # A Load action is always a real unload/reload, including for the currently
        # active model. This keeps UI, startup, and API callers on one invariant.
        if model_key:
            # Checked after unloading whatever LM Studio itself had loaded above, so that
            # freed memory counts — same resource_guard used by every other backend's load
            # path (see its docstring for the cross-backend OOM incident this guards against).
            refusal = resource_guard.check_available_memory()
            if refusal:
                return jsonify({"error": refusal}), 422
            # After unloading another model, LM Studio can need a moment to fully
            # release GPU/memory resources before it will accept a new load — an
            # immediate load attempt can fail transiently even though the model is
            # perfectly loadable. Retry a couple of times with a short backoff
            # rather than surfacing a spurious error (and leaving nothing loaded).
            attempts = 3 if unloaded else 1
            last_exc: lms.LMStudioError | None = None
            for attempt in range(attempts):
                try:
                    lms.load_model(
                        model_key,
                        identifier=body.get("identifier") or None,
                        # Default to a much bigger context than LM Studio's stock 8192: agent-mode
                        # tools (Continue, OpenCode) routinely send 8-10k+ tokens of system prompt
                        # and tool definitions before the user's first word, which otherwise fails
                        # with exceed_context_size_error. An explicit `context_length` still overrides.
                        context_length=body.get("context_length") or 32768,
                        ttl=body.get("ttl") or None,
                        # Prefer full offload, reserving runtime headroom when the
                        # model approaches the shared GPU memory budget.
                        gpu=body.get("gpu") or None,
                    )
                    last_exc = None
                    break
                except lms.LMStudioError as exc:
                    last_exc = exc
                    if attempt < attempts - 1:
                        time.sleep(2)
            if last_exc is not None:
                if unloaded:
                    raise lms.LMStudioError(
                        f"Unloaded {', '.join(unloaded)} but failed to load {model_key} "
                        f"after retrying: {last_exc}. No model is currently loaded."
                    )
                raise last_exc
    except (lms.LMStudioError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"ok": True, "unloaded": unloaded})


@app.post("/api/models/unload")
@admin_required
def api_unload_model():
    if not getattr(_model_test_context, 'active', False) and _agent_tasks.busy():
        return jsonify({'error': 'Pause or stop assigned agents before changing models.'}), 409
    if not getattr(_model_test_context, 'active', False) and any(j['status'] in model_tests.ACTIVE for j in _model_test_registry.list()):
        return jsonify({'error': 'A model test run is active. Cancel it or wait before changing models.'}), 409
    body = request.get_json(force=True, silent=True) or {}
    identifier = body.get("identifier")
    if not identifier:
        return jsonify({"error": "'identifier' is required"}), 400
    if sglang.owns_model(identifier):
        return jsonify({"error": "SGLang models are managed by the external SGLang server."}), 409
    if vllm.owns_model(identifier):
        try:
            vllm.unload_model(identifier)
        except vllm.VLLMError as exc:
            return jsonify({"error": str(exc)}), 502
        return jsonify({"ok": True})
    if kobold.owns_model(identifier):
        try:
            kobold.unload_model(identifier)
        except kobold.KoboldError as exc:
            return jsonify({"error": str(exc)}), 502
        return jsonify({"ok": True})
    if fastflowlm.owns_model(identifier):
        try:
            fastflowlm.unload_model(identifier)
        except fastflowlm.FastFlowLMError as exc:
            return jsonify({"error": str(exc)}), 502
        return jsonify({"ok": True})
    if lemonade.owns_model(identifier):
        try:
            lemonade.unload_model(identifier)
        except lemonade.LemonadeError as exc:
            return jsonify({"error": str(exc)}), 502
        return jsonify({"ok": True})
    if comfyui.owns_model(identifier):
        try:
            comfyui.unload_model(identifier)
        except comfyui.ComfyUIError as exc:
            return jsonify({"error": str(exc)}), 502
        return jsonify({"ok": True})
    try:
        lms.unload_model(identifier)
    except lms.LMStudioError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"ok": True})


@app.post("/api/models/delete")
@admin_required
def api_delete_model():
    body = request.get_json(force=True, silent=True) or {}
    identifier = body.get("identifier")
    if not identifier:
        return jsonify({"error": "'identifier' is required"}), 400

    backend = next((name for name, client in (
        ('sglang', sglang), ('vllm', vllm), ('kobold', kobold),
        ('fastflowlm', fastflowlm), ('lemonade', lemonade),
    ) if client.owns_model(identifier)), 'lmstudio')
    if not getattr(_model_test_context, 'active', False):
        if any(
            job.get('status') in agent_tasks.ACTIVE
            and (job.get('model') or {}).get('providerID') == backend
            and (job.get('model') or {}).get('id') == identifier
            for job in _agent_tasks.list()
        ):
            return jsonify({'error': 'This model is assigned to active agent work. Pause or stop that assignment before deleting it.'}), 409
        if any(
            job.get('status') in model_tests.ACTIVE
            and any(target.get('backend') == backend and target.get('id') == identifier
                    for target in job.get('targets', []))
            for job in _model_test_registry.list()
        ):
            return jsonify({'error': 'This model is part of an active model test run. Cancel or wait for that run before deleting it.'}), 409

    if backend == 'sglang':
        return jsonify({"error": "SGLang models cannot be deleted from Hearth; manage the external deployment."}), 409
    if backend == 'vllm':
        # vllm.delete_model() unloads first itself if this is the currently-served
        # model — no separate best-effort unload needed here, unlike the Lemonade
        # branch below (that unload lives inside vllm_client, not app.py, since it
        # also has to check "is this the served model" which only vllm_client tracks).
        try:
            vllm.delete_model(identifier)
        except vllm.VLLMError as exc:
            return jsonify({"error": str(exc)}), 502
        return jsonify({"ok": True})
    if backend == 'kobold':
        try:
            kobold.delete_model(identifier)
        except kobold.KoboldError as exc:
            return jsonify({"error": str(exc)}), 502
        return jsonify({"ok": True})
    if backend == 'fastflowlm':
        try:
            fastflowlm.delete_model(identifier)
        except fastflowlm.FastFlowLMError as exc:
            return jsonify({"error": str(exc)}), 502
        return jsonify({"ok": True})
    if backend == 'lemonade':
        # Frees memory first, disk second — a loaded-but-deleted model would be a
        # confusing dangling state. Best-effort: fine if it was already unloaded.
        try:
            lemonade.unload_model(identifier)
        except lemonade.LemonadeError:
            pass
        try:
            lemonade.delete_model(identifier)
        except lemonade.LemonadeError as exc:
            return jsonify({"error": str(exc)}), 502
        return jsonify({"ok": True})
    try:
        lms.delete_model(identifier)
    except lms.LMStudioError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"ok": True})


@app.post("/api/models/download")
@admin_required
def api_download_model():
    body = request.get_json(force=True, silent=True) or {}
    query = (body.get("query") or "").strip()
    backend = body.get("backend") or "lmstudio"
    if not query:
        return jsonify({"error": "'query' is required"}), 400
    if backend not in ("lmstudio", "lemonade", "vllm", "kobold", "fastflowlm"):
        return jsonify({"error": "'backend' must be 'lmstudio', 'lemonade', 'vllm', 'kobold', or 'fastflowlm'"}), 400

    key = f"{backend}:{query}"
    if backend in ("vllm", "kobold", "fastflowlm"):
        # Both yield structured byte-level progress dicts (kobold.download_model /
        # vllm.download_model) — routed through the same structured worker as Lemonade's
        # HF pulls, not the freeform text-line one.
        _start_download_worker(key, lambda: _download_worker_structured(key, query, backend, "huggingface"))
    else:
        _start_download_worker(key, lambda: _download_worker_freeform(key, query, backend))

    return Response(
        stream_with_context(_relay_download(key)),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


_MODEL_BACKEND_INFO = {
    "lmstudio": ("LM Studio", "GGUF", "CPU / Vulkan GPU"),
    "lemonade": ("Ryzen AI / Lemonade", "GGUF or FLM", "CPU / GPU / NPU by recipe"),
    "vllm": ("vLLM", "Hugging Face weights", "CPU on this system"),
    "kobold": ("KoboldCPP", "GGUF", "CPU / Vulkan GPU"),
    "fastflowlm": ("FastFlowLM", "FLM NPU", "AMD Ryzen AI NPU"),
}


def _hf_repo_from_result(result: dict) -> str | None:
    candidate = str(result.get("checkpoint") or result.get("id") or "")
    candidate = candidate.split(":", 1)[0]
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", candidate):
        return candidate
    return None


def _model_supports_tool_calling(result: dict) -> bool | None:
    """Only explicit capability metadata establishes support, never model names."""
    explicit = result.get("supports_tool_calling")
    if isinstance(explicit, bool):
        return explicit
    tags = []
    for key in ("tags", "labels", "capabilities"):
        values = result.get(key) or []
        if isinstance(values, dict):
            values = [k for k, enabled in values.items() if enabled is True]
        if isinstance(values, str):
            values = [values]
        tags.extend(str(v).lower().replace("_", "-") for v in values)
    if set(tags) & {"tool-calling", "tool-use", "function-calling", "tools"}:
        return True
    return None


def _model_max_context_length(result: dict) -> int | None:
    """Return an explicitly reported model context limit when catalog metadata has one."""
    config = result.get("config") if isinstance(result.get("config"), dict) else {}
    for key in ("max_context_length", "max_context_window", "context_window_tokens",
                "max_position_embeddings", "max_sequence_length", "n_ctx"):
        value = result.get(key, config.get(key))
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def _normalize_model_results(results: list[dict], backend: str) -> list[dict]:
    """Give every backend result the same complete, linkable metadata contract."""
    label, model_format, compatibility = _MODEL_BACKEND_INFO[backend]
    normalized = []
    for raw in results:
        result = dict(raw)
        repo = _hf_repo_from_result(result)
        existing_url = str(result.get("source_url") or "")
        if existing_url.startswith(("https://huggingface.co/", "https://github.com/")):
            source_url = existing_url
        elif repo:
            source_url = f"https://huggingface.co/{repo}"
        elif backend == "fastflowlm":
            source_url = "https://github.com/FastFlowLM/FastFlowLM"
        else:
            # A result without a verifiable source is unsafe to present as downloadable.
            continue
        result.update({
            "backend": backend,
            "backend_label": label,
            "supports_tool_calling": _model_supports_tool_calling(result),
            "max_context_length": _model_max_context_length(result),
            "format": model_format,
            "compatibility": compatibility,
            "source_url": source_url,
            # LM Studio needs a full HF URL to bypass fuzzy catalog resolution.
            "download_query": source_url if backend == "lmstudio" else result.get("id"),
        })
        normalized.append(result)
    return normalized


def _search_models_for_backend(backend: str, query: str) -> list[dict]:
    if backend == "vllm":
        results = vllm.search_huggingface(query) if query else vllm.suggested_models()
        return _normalize_model_results(results, backend)
    if backend in ("lmstudio", "kobold"):
        results = lemonade.search_huggingface(query)
        lemonade.fill_result_sizes(results)
        return _normalize_model_results(results, backend)
    if backend == "fastflowlm":
        return _normalize_model_results(fastflowlm.search_catalog(query, limit=500 if not query else 80), backend)
    if backend == "lemonade":
        results = lemonade.search_catalog(query, limit=500 if not query else 40)
        if query:
            results = lemonade.enrich_with_hf_metadata(results)
            results += lemonade.search_huggingface(query)
        lemonade.fill_result_sizes(results)
        return _normalize_model_results(results, backend)
    raise ValueError(f"Unknown backend: {backend}")


@app.get("/api/models/search")
@admin_required
def api_search_models():
    """Search one backend or every backend with one normalized, linkable result schema."""
    backend = request.args.get("backend", "lemonade")
    query = (request.args.get("q") or "").strip()
    tools_only = request.args.get("tools_only", "").lower() in ("1", "true", "yes")

    def filtered(results):
        return [r for r in results if r.get("supports_tool_calling") is True] if tools_only else results

    valid = tuple(_MODEL_BACKEND_INFO)
    if backend != "all" and backend not in valid:
        return jsonify({"error": f"'backend' must be 'all' or one of: {', '.join(valid)}"}), 400
    try:
        if backend == "all":
            results = []
            backend_errors = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(valid)) as pool:
                futures = {
                    pool.submit(_search_models_for_backend, name, query): name
                    for name in valid
                }
                for future, name in futures.items():
                    try:
                        results.extend(future.result(timeout=30))
                    except Exception:
                        # One unavailable backend must not erase useful results from four others.
                        backend_errors.append(name)
            order = {name: i for i, name in enumerate(valid)}
            results.sort(key=lambda r: (order.get(r["backend"], 99), -(r.get("downloads") or 0)))
            return jsonify({"results": filtered(results), "backend_errors": backend_errors})
        return jsonify({"results": filtered(_search_models_for_backend(backend, query))})
    except (lemonade.LemonadeError, vllm.VLLMError) as exc:
        return jsonify({"error": str(exc)}), 502


@app.post("/api/models/download-catalog")
@admin_required
def api_download_catalog_model():
    """Download a search result (from api_search_models) by id — separate from
    /api/models/download because this path yields structured byte-level progress dicts
    instead of freeform text lines, driving a real progress bar on the Downloads page
    rather than a scrolling log. 'source' picks which pull path: a Lemonade catalog
    entry needs no custom registration (pull_catalog_model), while a Hugging Face
    result isn't in Lemonade's catalog at all and needs the custom-registration shape
    plus an auto-picked quantization file (pull_huggingface_repo)."""
    body = request.get_json(force=True, silent=True) or {}
    model_id = (body.get("model_id") or "").strip()
    source = body.get("source") or "local"
    if not model_id:
        return jsonify({"error": "'model_id' is required"}), 400
    if source not in ("local", "huggingface"):
        return jsonify({"error": "'source' must be 'local' or 'huggingface'"}), 400

    key = f"lemonade:{model_id}"
    _start_download_worker(key, lambda: _download_worker_structured(key, model_id, "lemonade", source))

    return Response(
        stream_with_context(_relay_download(key)),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/models/upload-local")
@admin_required
def api_upload_local_model():
    """Save and register a local GGUF for the configured Lemonade server."""
    # This endpoint intentionally accepts large model files; keep the global form
    # memory cap for the other multipart endpoints, but allow the upload parser to
    # stream this one into its temporary file rather than rejecting at 60 MB.
    request.max_form_memory_size = 100 * 1024**3
    uploaded = request.files.get("model_file")
    if not uploaded or not uploaded.filename:
        return jsonify({"error": "Choose a GGUF model file to upload."}), 400
    filename = secure_filename(uploaded.filename)
    if not filename.lower().endswith(".gguf"):
        return jsonify({"error": "Only .gguf model files are supported for local upload."}), 400
    stem = secure_filename(Path(filename).stem).strip("._-")[:80]
    if not stem:
        return jsonify({"error": "The selected file needs a valid model name."}), 400
    if request.content_length and request.content_length > 100 * 1024**3:
        return jsonify({"error": "Model uploads are limited to 100 GB."}), 413

    models_dir = database.DATA_DIR / "local-models"
    models_dir.mkdir(parents=True, exist_ok=True)
    target = models_dir / f"{uuid.uuid4().hex}-{filename}"
    try:
        uploaded.save(target)
        if target.stat().st_size < 4:
            raise ValueError("The selected file is empty or incomplete.")
        with target.open("rb") as model_file:
            if model_file.read(4) != b"GGUF":
                raise ValueError("The selected file does not have a valid GGUF header.")
        result = lemonade.register_local_gguf(stem, str(target.resolve()))
        return jsonify({
            "ok": True,
            "model_id": result.get("model_name") or f"user.{stem}",
            "filename": filename,
            "size_bytes": target.stat().st_size,
        })
    except ValueError as exc:
        target.unlink(missing_ok=True)
        return jsonify({"error": str(exc)}), 400
    except lemonade.LemonadeError as exc:
        target.unlink(missing_ok=True)
        return jsonify({"error": str(exc)}), 502
    except OSError as exc:
        target.unlink(missing_ok=True)
        return jsonify({"error": f"Could not store the uploaded model: {exc}"}), 500


@app.get("/api/models/downloads/active")
@admin_required
def api_active_downloads():
    """Snapshot of every download currently in progress, regardless of which browser
    tab/request started it — the Downloads page polls this on load and periodically so
    a reload or a second tab shows a download that's actually still running server-side,
    instead of looking like nothing is happening (real user report: this genuinely used
    to be invisible, since progress previously lived only inside the initiating
    request's own SSE stream).

    Also prunes finished (done/error) entries older than _DOWNLOAD_FINISH_GRACE_SECONDS
    — normally the SSE relay that's actually watching a download (_relay_download)
    clears its own entry the moment it sees done/error, but a download can finish with
    zero relays attached (the tab that started it disconnected earlier and never came
    back — see _start_download_worker's docstring for the real incident this covers),
    so cleanup can't only rely on that. Internal-only fields (leading underscore, e.g.
    _full_lines) are stripped before serializing — those exist for _relay_download's own
    bookkeeping, not for the frontend."""
    now = time.time()
    with _active_downloads_lock:
        stale = [
            k for k, v in _active_downloads.items()
            if (v.get("done") or v.get("error")) and now - v.get("updated_at", now) > _DOWNLOAD_FINISH_GRACE_SECONDS
        ]
        for k in stale:
            _active_downloads.pop(k, None)
        downloads = [
            {k: v for k, v in entry.items() if not k.startswith("_")}
            for entry in _active_downloads.values()
        ]
    # Also surface a FastFlowLM `flm pull` running outside this app's own download workers
    # (e.g. a CLI `flm pull --force` re-download) — it isn't in _active_downloads, so read
    # FLM's on-disk progress directly. Skipped if an app-initiated FLM download is already
    # tracked above, to avoid showing it twice.
    if not any(d.get("backend") == "fastflowlm" for d in downloads):
        try:
            ambient = fastflowlm.active_pull_progress()
        except Exception:
            ambient = None
        if ambient:
            downloads.append(ambient)
    return jsonify({"downloads": downloads})


def _clean_partial_download_files(backend: str, model_id: str) -> tuple[int, int]:
    """Delete only temporary files associated with one cancelled model download.

    Completed model files are deliberately never matched. Each candidate remains under
    a known backend cache root and must carry a standard partial-download suffix.
    """
    candidates: list[Path] = []
    repo = model_id.removeprefix("https://huggingface.co/").strip("/")
    owner, separator, name = repo.partition("/")
    if separator:
        if backend == "lmstudio":
            model_dir = Path.home() / ".lmstudio" / "models" / owner / name
            if model_dir.is_dir():
                candidates.extend(model_dir.glob("*.part"))
        if backend in ("vllm", "kobold", "lemonade"):
            cache_dirs = (
                Path.home() / ".cache" / "huggingface" / "hub",
                Path("/var/lib/lemonade/.cache/huggingface/hub"),
            )
            dirname = f"models--{owner}--{name}"
            for cache in cache_dirs:
                repo_dir = cache / dirname
                if repo_dir.is_dir():
                    candidates.extend(repo_dir.rglob("*.incomplete"))
                    candidates.extend(repo_dir.rglob("*.part"))
    removed_files = 0
    removed_bytes = 0
    for path in candidates:
        try:
            size = path.stat().st_size
            path.unlink()
            removed_files += 1
            removed_bytes += size
        except OSError:
            continue
    return removed_files, removed_bytes


@app.post("/api/models/downloads/cancel")
@admin_required
def api_cancel_download():
    """Cancel one tracked downloader and remove only that model's partial artifacts."""
    body = request.get_json(silent=True) or {}
    backend = (body.get("backend") or "").strip()
    model_id = (body.get("model_id") or "").strip()
    if backend not in ("lmstudio", "lemonade", "vllm", "kobold", "fastflowlm") or not model_id:
        return jsonify({"error": "A valid 'backend' and 'model_id' are required."}), 400
    key = f"{backend}:{model_id}"
    with _active_downloads_lock:
        tracked = key in _active_downloads
        _cancelled_downloads.add(key)

    # App-started CLI downloaders are descendants of this WebUI process. Match both
    # the backend's executable and this exact model before terminating anything.
    signatures = {
        "lmstudio": ("lms", "get"),
        "vllm": ("hf", "download"),
        "fastflowlm": ("flm", "pull"),
    }
    stop_requested = 0
    signature = signatures.get(backend)
    terminated: list[psutil.Process] = []
    if signature:
        needles = {model_id, model_id.removeprefix("https://huggingface.co/")}
        for proc in psutil.Process().children(recursive=True):
            try:
                cmd = proc.cmdline()
                text = " ".join(cmd)
                if all(token in text for token in signature) and any(n in text for n in needles):
                    proc.terminate()
                    terminated.append(proc)
                    stopped += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    if terminated:
        _, alive = psutil.wait_procs(terminated, timeout=5)
        for proc in alive:
            try:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    removed_files, removed_bytes = _clean_partial_download_files(backend, model_id)
    with _active_downloads_lock:
        _active_downloads.pop(key, None)
    return jsonify({
        "ok": True,
        "tracked": tracked,
        "processes_stopped": stopped,
        "files_removed": removed_files,
        "bytes_removed": removed_bytes,
    })


CADDYFILE_PATH = Path("/etc/caddy/Caddyfile")


def _read_caddy_info() -> dict | None:
    """Best-effort parse of /etc/caddy/Caddyfile for display in Settings — read-only,
    this app doesn't write to it (editing it needs sudo, same reason the Colibri :8444
    cleanup earlier had to be handed to the user rather than done automatically).
    World-readable (0644 root:root, confirmed directly), so no permission issue reading
    it as the app's own unprivileged user. Returns None if it's missing/unreadable,
    same fail-soft contract as everything else here — Caddy being unconfigured
    shouldn't break the Settings panel."""
    try:
        text = CADDYFILE_PATH.read_text()
    except OSError:
        return None
    # Three distinct per-client tokens live in the Caddyfile now (opencode/vscode/lan - see
    # help_data.parse_caddyfile's docstring), not one shared secret. "lan" is the
    # general-purpose one shown here for the Settings panel's manual "Test connection" buttons.
    token_match = re.search(r'@lan\s+header\s+Authorization\s+"Bearer\s+([A-Za-z0-9]+)"', text)
    ports: dict[str, int] = {}
    for block in re.finditer(r"^(\S[^\n{]*)\{(.*?)^\}", text, re.MULTILINE | re.DOTALL):
        header, body = block.group(1), block.group(2)
        port_match = re.search(r":(\d+)", header)
        proxy_match = re.search(r"reverse_proxy\s+\S+\s+localhost:(\d+)", body)
        if not (port_match and proxy_match):
            continue
        caddy_port, target_port = int(port_match.group(1)), proxy_match.group(1)
        if target_port == "1234":
            ports["lmstudio"] = caddy_port
        elif target_port in ("13305", "13306"):
            # 13305 = Lemonade's own server directly; 13306 = lemonade_guard_shim.py, which
            # Caddy's :8444 block now proxies to instead (see its module docstring) so LAN
            # clients can't trigger the NPU-collision OOM that used to hit flm.service - the
            # shim forwards everything else to 13305 unmodified, so either port means "this
            # Caddy block is the lemonade one" (same dual-port pattern as fastflowlm below).
            ports["lemonade"] = caddy_port
        elif target_port == "8000":
            ports["vllm"] = caddy_port
        elif target_port == "5001":
            ports["kobold"] = caddy_port
        elif target_port in ("52625", "52626"):
            # 52625 = FastFlowLM's own server directly; 52626 = flm_tool_shim.py, which Caddy's
            # :8447 block now proxies to instead (see its module docstring) so LAN clients like
            # OpenCode get real tool_calls — the shim forwards everything else to 52625
            # unmodified, so either port means "this Caddy block is the fastflowlm one."
            ports["fastflowlm"] = caddy_port
    return {
        "bearer_token": token_match.group(1) if token_match else None,
        "ports": ports,
    }


# backend -> the local upstream port each Caddy block reverse-proxies to (see
# /etc/caddy/Caddyfile) - the same 5 targets help_data.py's own port-parsing maps FROM, needed
# here in the other direction to hand an admin a ready-to-paste line for each block. lemonade
# points at lemonade_guard_shim.py (13306), not Lemonade's raw 13305, same reason fastflowlm
# points at flm_tool_shim.py (52626) rather than FastFlowLM's raw 52625 - see either shim's
# module docstring.
_CADDY_UPSTREAM = {
    "lmstudio": 1234, "lemonade": 13306, "vllm": 8000, "kobold": 5001, "fastflowlm": 52626,
}


def _caddy_matcher_name(username: str, user_id: str) -> str:
    # Caddy matcher names are bare identifiers (no spaces/punctuation) - sanitize the username
    # and append a short id suffix so two usernames that sanitize the same way (e.g. "a.b" and
    # "a_b") can't collide into the same matcher.
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", username.lower()).strip("_") or "user"
    return f"user_{safe}_{user_id[:6]}"


def _caddy_user_snippet(username: str, user_id: str, token: str) -> str:
    """A ready-to-paste block for this user's personal LAN token - this app never writes to
    /etc/caddy/Caddyfile itself (root-owned, no sudo from this process - see
    _read_caddy_info's docstring), so the admin has to add this by hand and reload Caddy
    (`sudo systemctl reload caddy`) same as the opencode/vscode/lan tokens already are."""
    matcher = _caddy_matcher_name(username, user_id)
    caddy = _read_caddy_info() or {"ports": {}}
    ports = {**caddy.get("ports", {})}
    lines = [
        f'Add to the `(client_auth)` snippet near the top of {CADDYFILE_PATH}:',
        f'    @{matcher} header Authorization "Bearer {token}"',
        "",
        "Then add one reverse_proxy line to EACH of the 5 port blocks' `route { }`, right",
        "before `respond 401`:",
    ]
    for backend, upstream in _CADDY_UPSTREAM.items():
        ext_port = ports.get(backend, "?")
        lines.append(f"    reverse_proxy @{matcher} localhost:{upstream}   # port {ext_port} ({backend})")
    lines += ["", "Then: sudo caddy validate --config " + str(CADDYFILE_PATH) +
              " --adapter caddyfile && sudo systemctl reload caddy"]
    return "\n".join(lines)


def _test_lmstudio(base_url: str, api_key: str | None) -> tuple[bool, str]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        resp = requests.get(f"{base_url.rstrip('/')}/api/v0/models", headers=headers, timeout=8)
        resp.raise_for_status()
    except requests.RequestException as exc:
        return False, str(exc)
    count = len(resp.json().get("data", []))
    return True, f"Connected — {count} model(s) visible."


def _test_lemonade(base_url: str, api_key: str | None) -> tuple[bool, str]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        resp = requests.get(f"{base_url.rstrip('/')}/v1/health", headers=headers, timeout=8)
        resp.raise_for_status()
    except requests.RequestException as exc:
        return False, str(exc)
    loaded = resp.json().get("model_loaded")
    return True, f"Connected — {'loaded: ' + loaded if loaded else 'no model currently loaded'}."


def _test_ondemand_openai(base_url: str, api_key: str | None, label: str, load_hint: str) -> tuple[bool, str]:
    """Connection test for the on-demand OpenAI-compatible backends (vLLM, KoboldCPP,
    FastFlowLM). Unlike LM Studio/Lemonade — which run always-on servers — these only run
    while one of their models is loaded (Load starts their service, Unload stops it). So a
    plain connection-refused here almost always means "no model of this backend is loaded
    yet", not a misconfigured URL — turn that into a clear, actionable message instead of a
    raw connection-error stack trace."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        resp = requests.get(f"{base_url.rstrip('/')}/v1/models", headers=headers, timeout=8)
        resp.raise_for_status()
    except requests.exceptions.ConnectionError:
        return False, (
            f"{label} isn't running at {base_url} — it starts on demand. {load_hint} "
            "(This is expected when no such model is loaded; the URL is fine.)"
        )
    except requests.RequestException as exc:
        return False, str(exc)
    ids = [m.get("id") for m in resp.json().get("data", [])]
    return True, f"Connected — serving: {', '.join(ids)}." if ids else "Connected — up, no model loaded yet."


def _test_vllm(base_url: str, api_key: str | None) -> tuple[bool, str]:
    return _test_ondemand_openai(
        base_url, api_key, "vLLM",
        "Load a vLLM model from the Models page (it serves one model at a time), then test again.",
    )


def _test_sglang(base_url: str, api_key: str | None) -> tuple[bool, str]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        response = requests.get(f"{base_url.rstrip('/')}/v1/models", headers=headers, timeout=8)
        response.raise_for_status()
        ids = [m.get("id") for m in response.json().get("data", []) if m.get("id")]
    except requests.RequestException as exc:
        return False, f"Could not reach external SGLang at {base_url}: {exc}"
    except (ValueError, AttributeError):
        return False, "SGLang returned an invalid /v1/models response."
    return True, f"Connected — serving: {', '.join(ids)}." if ids else "Connected — no model is currently advertised."


def _test_kobold(base_url: str, api_key: str | None) -> tuple[bool, str]:
    return _test_ondemand_openai(
        base_url, api_key, "KoboldCPP",
        "Load a KoboldCPP model from the Models page, then test again.",
    )


def _test_fastflowlm(base_url: str, api_key: str | None) -> tuple[bool, str]:
    return _test_ondemand_openai(
        base_url, api_key, "FastFlowLM",
        "Load a FastFlowLM (Ryzen AI NPU) model from the Models page, then test again.",
    )


def _test_caddy(port: int, token: str, path: str, host: str = "localhost") -> tuple[bool, str]:
    # verify=False: same self-signed Caddy cert every curl -k test this session already
    # trusted manually — this app has no separate copy of the CA to verify against.
    # Warning suppressed for just this one call, not globally.
    # `host` defaults to localhost (the original same-host smoke test); the Tailscale test
    # targets below pass the Tailscale IP instead, to confirm Caddy actually has a site block
    # matching that address (see the Help panel's Tailscale section / lemonade_guard_shim.py
    # era Caddyfile changes) rather than just that Caddy is up at all.
    import urllib3
    import warnings

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
            resp = requests.get(
                f"https://{host}:{port}{path}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=8,
                verify=False,
            )
        # 502/503 from Caddy means the proxy itself is fine (TLS + auth passed) but the
        # backend it forwards to isn't running — the common case being an on-demand
        # backend (vLLM/KoboldCPP/FastFlowLM) with no model loaded. Say that plainly
        # rather than surfacing a bare "502 Bad Gateway".
        if resp.status_code in (502, 503):
            return False, (
                f"Caddy on {host}:{port} is reachable and authenticated, but the backend behind "
                f"it isn't running (HTTP {resp.status_code}). If it's an on-demand backend "
                "(vLLM/KoboldCPP/FastFlowLM), load one of its models from the Models page first."
            )
        resp.raise_for_status()
    except requests.RequestException as exc:
        return False, str(exc)
    return True, f"Reachable via Caddy on {host}:{port}, authenticated OK (HTTP {resp.status_code})."


def _effective_tailscale_ip() -> str | None:
    """The Tailscale IP Settings/Help actually use — a manual override (Settings page) wins,
    else live-detected via `tailscale` CLI (help_data.tailscale_ip()). Kept separate from
    help_data.build()'s own copy of this logic so a plain connectivity test doesn't need to
    build the whole Help payload (models list, Continue/OpenCode configs, etc.) just to get
    an IP."""
    return settings_store.get("tailscale_ip_override") or help_data.tailscale_ip()


@app.post("/api/settings/test")
@admin_required
def api_test_settings():
    body = request.get_json(force=True, silent=True) or {}
    target = body.get("target")
    if target == "lmstudio":
        ok, message = _test_lmstudio(
            (body.get("base_url") or "").strip() or lms.base_url(),
            body.get("api_key") or lms.api_key(),
        )
    elif target == "lemonade":
        ok, message = _test_lemonade(
            (body.get("base_url") or "").strip() or lemonade.base_url(),
            body.get("api_key") or lemonade.api_key(),
        )
    elif target == "vllm":
        ok, message = _test_vllm(
            (body.get("base_url") or "").strip() or vllm.base_url(),
            body.get("api_key") or vllm.api_key(),
        )
    elif target == "sglang":
        ok, message = _test_sglang(
            (body.get("base_url") or "").strip() or sglang.base_url(),
            body.get("api_key") or sglang.api_key(),
        )
    elif target == "kobold":
        ok, message = _test_kobold(
            (body.get("base_url") or "").strip() or kobold.base_url(),
            body.get("api_key") or kobold.api_key(),
        )
    elif target == "fastflowlm":
        ok, message = _test_fastflowlm(
            (body.get("base_url") or "").strip() or fastflowlm.base_url(),
            body.get("api_key") or fastflowlm.api_key(),
        )
    elif target in ("caddy_lmstudio", "caddy_lemonade", "caddy_vllm", "caddy_kobold", "caddy_fastflowlm"):
        caddy = _read_caddy_info()
        if not caddy or not caddy.get("bearer_token"):
            return jsonify({"ok": False, "message": "Caddyfile not found/readable, or no bearer token in it."})
        port_key = target[len("caddy_"):]
        port = caddy["ports"].get(port_key)
        if not port:
            return jsonify({"ok": False, "message": f"No {port_key} proxy block found in Caddyfile."})
        path = "/v1/health" if port_key == "lemonade" else "/v1/models"
        ok, message = _test_caddy(port, caddy["bearer_token"], path)
    elif target in (
        "tailscale_lmstudio", "tailscale_lemonade", "tailscale_vllm", "tailscale_kobold", "tailscale_fastflowlm",
    ):
        ts_ip = _effective_tailscale_ip()
        if not ts_ip:
            return jsonify({
                "ok": False,
                "message": "No Tailscale IP detected on this machine, and no manual override set below.",
            })
        caddy = _read_caddy_info()
        if not caddy or not caddy.get("bearer_token"):
            return jsonify({"ok": False, "message": "Caddyfile not found/readable, or no bearer token in it."})
        port_key = target[len("tailscale_"):]
        port = caddy["ports"].get(port_key)
        if not port:
            return jsonify({"ok": False, "message": f"No {port_key} proxy block found in Caddyfile."})
        path = "/v1/health" if port_key == "lemonade" else "/v1/models"
        ok, message = _test_caddy(port, caddy["bearer_token"], path, host=ts_ip)
    else:
        return jsonify({
            "error": "'target' must be lmstudio, lemonade, vllm, sglang, kobold, fastflowlm, "
                     "or a caddy_*/tailscale_* variant",
        }), 400
    return jsonify({"ok": ok, "message": message})


@app.get("/api/settings")
@admin_required
def api_get_settings():
    # Deliberately shows the actual values, not just "is a key set" — this is an
    # admin-only panel already, and the Caddy bearer token below is shown in
    # plaintext to every logged-in user elsewhere in this app (Help panel), so
    # hiding these from admins specifically added friction without a real security
    # boundary behind it.
    return jsonify(_settings_snapshot())


def _settings_snapshot() -> dict:
    """Current connection settings for every backend, shared by the GET and POST handlers
    so they never drift out of sync (a real risk before, when both hand-built the dict)."""
    compaction = help_data.compaction_settings()
    context_settings = help_data.opencode_context_settings()
    return {
        "lmstudio_base_url": lms.base_url(),
        "lmstudio_api_key": lms.api_key() or "",
        "lemonade_base_url": lemonade.base_url(),
        "lemonade_api_key": lemonade.api_key() or "",
        "vllm_base_url": vllm.base_url(),
        "vllm_api_key": vllm.api_key() or "",
        "sglang_base_url": sglang.base_url(),
        "sglang_api_key": sglang.api_key() or "",
        "kobold_base_url": kobold.base_url(),
        "kobold_api_key": kobold.api_key() or "",
        # GPU offload (Vulkan) + speculative decoding tuning (2026-07-27) - see
        # kobold_client._write_service_env()'s docstring for why these are global settings
        # rather than per-model options, and kobold.service's ExecStart for how an unset/
        # empty value here cleanly omits the corresponding flag rather than erroring.
        "kobold_gpu_layers": settings_store.get("kobold_gpu_layers") or "",
        "kobold_draft_model": settings_store.get("kobold_draft_model") or "",
        "kobold_draft_amount": settings_store.get("kobold_draft_amount") or "",
        "fastflowlm_base_url": fastflowlm.base_url(),
        "fastflowlm_api_key": fastflowlm.api_key() or "",
        "lan_default_model": settings_store.get("lan_default_model") or "",
        # Admin-editable chat request limits (Settings panel) - see _chat_limits() for the
        # live-override/default behavior and _CHAT_LIMIT_BOUNDS for the accepted ranges.
        **_chat_limits(),
        # Admin-editable OpenCode compaction settings, baked into the Help page's generated
        # config - see help_data.compaction_settings(). A null buffer means "omit it".
        "opencode_compaction_keep_tokens": compaction["keep_tokens"],
        "opencode_compaction_buffer": compaction["buffer"],
        # OpenCode context advertising: whether each model's real loaded context is used, and
        # an optional fallback for models whose real context isn't reported (null = per-backend
        # default). See help_data.opencode_context_settings().
        "opencode_advertise_loaded_context": context_settings["advertise_loaded"],
        "opencode_context_fallback": context_settings["fallback"],
        "web_search_enabled": (settings_store.get("web_search_enabled") or "true") == "true",
        "web_search_mode": settings_store.get("web_search_mode") or "automatic",
        "web_search_max_results": int(settings_store.get("web_search_max_results") or "6"),
        "web_search_max_pages": int(settings_store.get("web_search_max_pages") or "4"),
        "web_search_direct_urls": (settings_store.get("web_search_direct_urls") or "true") == "true",
        "web_search_browser_enabled": (settings_store.get("web_search_browser_enabled") or "true") == "true",
        "caddy": _read_caddy_info(),
        # Tailscale: "enabled" gates the Help panel's Tailscale section entirely (default on,
        # since the section already no-ops itself when no IP is detected either way); the
        # override lets an admin type in the IP by hand if this app's own process can't shell
        # out to the `tailscale` CLI successfully. No "prefer Tailscale" switch — the Help
        # page's LAN config (Step 2) always stays LAN, and Tailscale gets its own always-
        # present config pair alongside it (see help_data.build()'s docstring) rather than
        # replacing anything, since some users want the LAN config kept even with Tailscale
        # enabled.
        "tailscale_enabled": (settings_store.get("tailscale_enabled") or "true") == "true",
        "tailscale_ip_override": settings_store.get("tailscale_ip_override") or "",
        "tailscale_detected_ip": help_data.tailscale_ip() or "",
        # Reasoning/thinking mode for Lemonade models that support it (Qwen3-style
        # chat_template_kwargs.enable_thinking). Applied per-request, not at load time -
        # confirmed live that Lemonade's own /v1/load recipe_options override is silently
        # ignored (accepted with 200, but the relaunched server's actual chat-template
        # default was unchanged) - so this setting drives the request-level field instead,
        # both for Hearth's own chat (see the webui-chat worker in api_chat_stream) and for
        # OpenCode's direct-to-Lemonade traffic (see build_opencode_config's extraBody).
        "lemonade_enable_thinking": (settings_store.get("lemonade_enable_thinking") or "true") == "true",
    }


@app.post("/api/settings")
@admin_required
def api_update_settings():
    body = request.get_json(force=True, silent=True) or {}

    # Only keys the panel actually sends are written; a missing key leaves that setting
    # untouched, an empty string clears it and restores the built-in backend default.
    for key in (
        "lmstudio_base_url", "lmstudio_api_key",
        "lemonade_base_url", "lemonade_api_key",
        "vllm_base_url", "vllm_api_key",
        "sglang_base_url", "sglang_api_key",
        "kobold_base_url", "kobold_api_key",
        "fastflowlm_base_url", "fastflowlm_api_key",
        "lan_default_model", "tailscale_ip_override",
        "kobold_gpu_layers", "kobold_draft_model", "kobold_draft_amount",
    ):
        if key in body:
            settings_store.set(key, (body.get(key) or "").strip())

    # Checkbox-backed boolean arrives as JSON true/false, not a string — stored as "true"/
    # "false" text (settings_store is a plain string key/value store) so _settings_snapshot's
    # `(... or "true") == "true"` check stays a simple or-else-default check.
    if "tailscale_enabled" in body:
        settings_store.set("tailscale_enabled", "true" if body.get("tailscale_enabled") else "false")
    if "lemonade_enable_thinking" in body:
        settings_store.set("lemonade_enable_thinking", "true" if body.get("lemonade_enable_thinking") else "false")

    for key in (
        "web_search_enabled", "web_search_direct_urls", "web_search_browser_enabled",
    ):
        if key in body:
            settings_store.set(key, "true" if body.get(key) else "false")
    if "web_search_mode" in body:
        mode = str(body.get("web_search_mode") or "")
        if mode not in ("automatic", "explicit"):
            return jsonify({"error": "web_search_mode must be 'automatic' or 'explicit'"}), 400
        settings_store.set("web_search_mode", mode)
    for key, minimum, maximum in (
        ("web_search_max_results", 1, 10),
        ("web_search_max_pages", 0, 5),
    ):
        if key in body:
            try:
                value = int(body.get(key))
            except (TypeError, ValueError):
                return jsonify({"error": f"{key} must be a whole number"}), 400
            if not minimum <= value <= maximum:
                return jsonify({"error": f"{key} must be between {minimum} and {maximum}"}), 400
            settings_store.set(key, str(value))

    # Chat request limits. Parsed and cross-checked into `proposed` before anything is
    # written, so an invalid combination (or a bad number) can't leave the store half-updated.
    proposed = _chat_limits()
    pending: dict[str, str | None] = {}
    for key, (minimum, maximum) in _CHAT_LIMIT_BOUNDS.items():
        if key not in body:
            continue
        raw = body.get(key)
        if raw is None or raw == "":
            # Empty clears the override, reverting to the built-in default.
            proposed[key] = _CHAT_LIMIT_DEFAULTS[key]
            pending[key] = None
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return jsonify({"error": f"{key} must be a whole number"}), 400
        if not minimum <= value <= maximum:
            return jsonify({"error": f"{key} must be between {minimum} and {maximum}"}), 400
        proposed[key] = value
        pending[key] = str(value)
    if proposed["chat_default_max_tokens"] > proposed["chat_max_max_tokens"]:
        return jsonify({
            "error": "Default max tokens cannot exceed the max tokens cap",
        }), 400
    for key, value in pending.items():
        settings_store.set(key, value)

    # OpenCode compaction settings (baked into the Help page's generated config). Parsed
    # before writing so a bad buffer can't leave keep.tokens half-updated; an empty value
    # clears the override (an unset buffer makes the generated config omit it).
    compaction_pending: dict[str, str | None] = {}
    for key, (minimum, maximum) in _OPENCODE_COMPACTION_BOUNDS.items():
        if key not in body:
            continue
        raw = body.get(key)
        if raw is None or raw == "":
            compaction_pending[key] = None
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return jsonify({"error": f"{key} must be a whole number"}), 400
        if not minimum <= value <= maximum:
            return jsonify({"error": f"{key} must be between {minimum} and {maximum}"}), 400
        compaction_pending[key] = str(value)
    for key, value in compaction_pending.items():
        settings_store.set(key, value)

    # OpenCode context advertising: a boolean toggle plus an optional fallback context (see
    # help_data.opencode_context_settings and the Settings panel's "OpenCode context" card).
    if "opencode_advertise_loaded_context" in body:
        settings_store.set(
            "opencode_advertise_loaded_context",
            "true" if body.get("opencode_advertise_loaded_context") else "false",
        )
    if "opencode_context_fallback" in body:
        raw = body.get("opencode_context_fallback")
        if raw is None or raw == "":
            settings_store.set("opencode_context_fallback", None)
        else:
            minimum, maximum = _OPENCODE_CONTEXT_FALLBACK_BOUNDS
            try:
                value = int(raw)
            except (TypeError, ValueError):
                return jsonify({"error": "opencode_context_fallback must be a whole number"}), 400
            if not minimum <= value <= maximum:
                return jsonify({
                    "error": f"opencode_context_fallback must be between {minimum} and {maximum}",
                }), 400
            settings_store.set("opencode_context_fallback", str(value))

    return jsonify(_settings_snapshot())


@app.get("/api/users")
@admin_required
def api_list_users():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    return jsonify(users_store.list_users(page=page, per_page=per_page))


@app.post("/api/users")
@admin_required
def api_create_user():
    body = request.get_json(force=True, silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    role = body.get("role") or "user"

    if not username:
        return jsonify({"error": "'username' is required"}), 400
    if len(password) < 8:
        return jsonify({"error": "password must be at least 8 characters"}), 400
    if role not in users_store.VALID_ROLES:
        return jsonify({"error": f"role must be one of {sorted(users_store.VALID_ROLES)}"}), 400
    agent_access = body.get("agentic_workflow_enabled")
    if "agentic_workflow_enabled" in body and type(agent_access) is not bool:
        return jsonify({"error": "agentic_workflow_enabled must be a boolean"}), 400
    if users_store.username_exists(username):
        return jsonify({"error": f"username '{username}' is already taken"}), 409

    user = users_store.create_user(username, password, role, agent_access)
    # caddy_snippet shown once here (and re-fetchable later via GET .../caddy-token) since this
    # app can't add it to the Caddyfile itself - see _caddy_user_snippet's docstring.
    snippet = _caddy_user_snippet(username, user["id"], user["caddy_token"])
    return jsonify({"user": user, "caddy_snippet": snippet}), 201


def _request_stop_for_user_agents(user_id):
    requested = 0
    for job in _agent_tasks.list():
        if job.get("owner") != user_id or job.get("status") not in agent_tasks.ACTIVE:
            continue
        try:
            _agent_tasks.control(job["id"], "stop")
            requested += 1
        except (KeyError, ValueError):
            pass  # It may have completed between the snapshot and stop request.
    return requested


@app.patch("/api/users/<user_id>/agentic-workflow")
@admin_required
def api_set_user_agentic_workflow(user_id):
    body = request.get_json(force=True, silent=True) or {}
    enabled = body.get("enabled")
    if type(enabled) is not bool:
        return jsonify({"error": "enabled must be a boolean"}), 400
    if users_store.get_user_by_id(user_id) is None:
        return jsonify({"error": "not found"}), 404
    if not users_store.set_agentic_workflow_enabled(user_id, enabled):
        return jsonify({"error": "not found"}), 404
    stop_requested = _request_stop_for_user_agents(user_id) if not enabled else 0
    return jsonify({"enabled": enabled, "stop_requested_assignments": stop_requested})


@app.delete("/api/users/<user_id>")
@admin_required
def api_delete_user(user_id):
    if user_id == session.get("user_id"):
        return jsonify({"error": "you can't delete your own account while logged in as it"}), 400
    target = users_store.get_user_by_id(user_id)
    if target is None:
        return jsonify({"error": "not found"}), 404
    if target["role"] == "admin" and users_store.count_admins() <= 1:
        return jsonify({"error": "can't delete the last remaining admin account"}), 400
    try:
        _agent_tasks.delete_owner(user_id)
    except Exception as exc:
        app.logger.exception('Could not remove the user’s agent assignments and containers')
        return jsonify({'error': f'Could not remove the user’s agent data: {exc}'}), 503
    for conv in chat_store.list_conversations(user_id):
        _delete_generated_images(conv["id"], user_id)
    users_store.delete_user(user_id)
    chat_store.delete_all_conversations_for_user(user_id)
    memory_store.delete_user_chunks(user_id)
    return jsonify({"ok": True})


@app.post("/api/users/<user_id>/reset-password")
@admin_required
def api_reset_password(user_id):
    # The secure equivalent of "recover a forgotten password" — an admin sets a new
    # one, since the old one was never retrievable from its hash in the first place
    # (true for any properly-hashed password store, not a limitation specific to
    # this app). No "current password" check here — an admin resetting someone
    # else's password is exactly the scenario this exists for.
    body = request.get_json(force=True, silent=True) or {}
    new_password = body.get("new_password") or ""
    if len(new_password) < 8:
        return jsonify({"error": "password must be at least 8 characters"}), 400
    target = users_store.get_user_by_id(user_id)
    if target is None:
        return jsonify({"error": "not found"}), 404
    users_store.set_password(user_id, new_password)
    return jsonify({"ok": True})


@app.get("/api/users/<user_id>/caddy-token")
@admin_required
def api_get_caddy_token(user_id):
    """Re-fetch a user's existing LAN token + paste-ready Caddyfile snippet - shown once at
    creation time (the create-user response), this is for looking it up again later without
    regenerating it (e.g. setting up a second device, or the admin didn't copy it the first
    time)."""
    target = users_store.get_user_by_id(user_id)
    if target is None:
        return jsonify({"error": "not found"}), 404
    token = users_store.get_caddy_token(user_id)
    snippet = _caddy_user_snippet(target["username"], user_id, token)
    return jsonify({"caddy_token": token, "caddy_snippet": snippet})


@app.post("/api/users/<user_id>/regenerate-caddy-token")
@admin_required
def api_regenerate_caddy_token(user_id):
    """Issues a new token for this user, invalidating the old one (once the admin actually
    copies the new snippet into the Caddyfile and reloads Caddy - this app can't do that part
    itself, see _caddy_user_snippet). For a compromised/leaked personal token, or just
    rotating credentials."""
    target = users_store.get_user_by_id(user_id)
    if target is None:
        return jsonify({"error": "not found"}), 404
    token = users_store.regenerate_caddy_token(user_id)
    snippet = _caddy_user_snippet(target["username"], user_id, token)
    return jsonify({"caddy_token": token, "caddy_snippet": snippet})


@app.get("/api/conversations")
@login_required
def api_list_conversations():
    return jsonify({"conversations": chat_store.list_conversations(session["user_id"])})


@app.get("/api/conversations/<conversation_id>")
@login_required
def api_get_conversation(conversation_id):
    conv = chat_store.get_conversation(conversation_id, session["user_id"])
    if conv is None:
        return jsonify({"error": "not found"}), 404
    job = chat_jobs.registry.get(session["user_id"], conversation_id)
    conv["generation"] = job.snapshot() if job else None
    return jsonify(conv)


@app.post("/api/conversations/<conversation_id>/generation/stop")
@login_required
def api_stop_chat_generation(conversation_id):
    if chat_store.get_conversation(conversation_id, session["user_id"]) is None:
        return jsonify({"error": "not found"}), 404
    job = chat_jobs.registry.get(session["user_id"], conversation_id)
    if job and job.status == "running":
        job.cancel.set()
    return jsonify({"ok": True})


_IMAGE_MARKDOWN_RE = re.compile(r"!\[[^\]]*\]\(/api/images/([a-zA-Z0-9_.-]+)\)")
_VIDEO_MARKDOWN_RE = re.compile(r"!\[video:[^\]]*\]\(/api/videos/([a-zA-Z0-9_.-]+)\)")


def _delete_generated_images(conversation_id: str, user_id: str) -> None:
    """Generated images live on disk, not in the DB — deleting a conversation should
    take them with it, or they'd accumulate forever."""
    conv = chat_store.get_conversation(conversation_id, user_id)
    if conv is None:
        return
    for message in conv["messages"]:
        for filename in _IMAGE_MARKDOWN_RE.findall(message["content"]):
            (IMAGES_DIR / filename).unlink(missing_ok=True)
        for filename in _VIDEO_MARKDOWN_RE.findall(message["content"]):
            (VIDEOS_DIR / filename).unlink(missing_ok=True)


def _last_reply_was_image(messages: list[dict]) -> bool:
    """True if the most recent turn before the newest (last) message in `messages` was
    an assistant reply containing a generated image. Skips over any system message(s) at
    the tail; the newest message itself (the incoming user turn) is always excluded since
    it's what we're deciding how to route, not what came before it."""
    for m in reversed(messages[:-1]):
        role = m.get("role")
        if role == "system":
            continue
        return role == "assistant" and bool(_IMAGE_MARKDOWN_RE.search(m.get("content") or ""))
    return False


@app.delete("/api/conversations/<conversation_id>")
@login_required
def api_delete_conversation(conversation_id):
    _delete_generated_images(conversation_id, session["user_id"])
    chat_store.delete_conversation(conversation_id, session["user_id"])
    memory_store.delete_conversation_chunks(conversation_id)
    return jsonify({"ok": True})


@app.post("/api/conversations/<conversation_id>/truncate")
@login_required
def api_truncate_conversation(conversation_id):
    """Rewind a conversation to its first `keep` messages — the server side of the chat
    window's edit / regenerate / retry actions (see chat_store.truncate_messages).
    The client rewinds its own view to the same point, then re-runs /api/chat from
    there."""
    body = request.get_json(force=True, silent=True) or {}
    keep = body.get("keep")
    if not isinstance(keep, int) or keep < 0:
        return jsonify({"error": "'keep' must be a non-negative integer"}), 400
    ok = chat_store.truncate_messages(conversation_id, session["user_id"], keep)
    if not ok:
        return jsonify({"error": "conversation not found"}), 404
    return jsonify({"ok": True})


@app.post("/api/voice/transcribe")
@login_required
def api_voice_transcribe():
    """Mic input: browser records audio (MediaRecorder), posts the blob here as
    multipart/form-data, gets back the transcribed text to drop into the composer.
    Turn-based by design, not a streaming/live-transcription endpoint - see the voice
    feature's README section for why continuous/interruptible voice mode isn't
    realistic on this hardware (a local STT->LLM->TTS pipeline has multi-second
    per-turn latency no matter how each stage is optimized)."""
    audio_file = request.files.get("audio")
    if not audio_file:
        return jsonify({"error": "'audio' file is required"}), 400
    audio_bytes = audio_file.read()
    if not audio_bytes:
        return jsonify({"error": "empty audio"}), 400
    try:
        text = fastflowlm.transcribe_audio(audio_bytes)
    except fastflowlm.FastFlowLMError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"text": text})


@app.post("/api/voice/translate")
@login_required
def api_voice_translate():
    """Turn-based local speech translation using Gemma 4 E4B on the Ryzen AI NPU."""
    audio_file = request.files.get("audio")
    target_language = (request.form.get("target_language") or "English").strip()
    if not audio_file:
        return jsonify({"error": "'audio' file is required"}), 400
    if target_language not in fastflowlm.VOICE_TRANSLATION_LANGUAGES:
        return jsonify({"error": "unsupported target language"}), 400
    audio_bytes = audio_file.read(15 * 1024 * 1024 + 1)
    if not audio_bytes:
        return jsonify({"error": "empty audio"}), 400
    if len(audio_bytes) > 15 * 1024 * 1024:
        return jsonify({"error": "audio recording exceeds the 15 MB limit"}), 413
    try:
        transcript = lemonade.transcribe_audio(
            audio_bytes, filename=audio_file.filename or "translation.webm"
        )
        if not transcript:
            return jsonify({"error": "no speech was recognized"}), 422
        text = fastflowlm.translate_text(transcript, target_language)
    except (lemonade.LemonadeError, fastflowlm.FastFlowLMError) as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"text": text, "transcript": transcript, "target_language": target_language,
                    "model": "Gemma 4 E4B (Ryzen AI NPU)"})


@app.post("/api/image/translate")
@login_required
def api_image_translate():
    """OCR + translate any text visible in an uploaded image, using Gemma 4 E4B's vision
    input on the Ryzen AI NPU - same model as voice translation."""
    image_file = request.files.get("image")
    target_language = (request.form.get("target_language") or "English").strip()
    if not image_file:
        return jsonify({"error": "'image' file is required"}), 400
    if target_language not in fastflowlm.VOICE_TRANSLATION_LANGUAGES:
        return jsonify({"error": "unsupported target language"}), 400
    image_bytes = image_file.read(15 * 1024 * 1024 + 1)
    if not image_bytes:
        return jsonify({"error": "empty image"}), 400
    if len(image_bytes) > 15 * 1024 * 1024:
        return jsonify({"error": "image exceeds the 15 MB limit"}), 413
    mime_type = image_file.mimetype or "image/png"
    try:
        text = fastflowlm.translate_image_text(image_bytes, mime_type, target_language)
    except fastflowlm.FastFlowLMError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"text": text, "target_language": target_language,
                    "model": "Gemma 4 E4B (Ryzen AI NPU)"})


@app.post("/api/video/translate")
@login_required
def api_video_translate():
    """Translate a video's spoken audio track: extract audio (ffmpeg, any container),
    transcribe it (Whisper via Lemonade), then translate the transcript with Gemma 4 E4B -
    same text-translation pipeline /api/voice/translate uses, just fed from a video file
    instead of a live mic recording."""
    video_file = request.files.get("video")
    target_language = (request.form.get("target_language") or "English").strip()
    if not video_file:
        return jsonify({"error": "'video' file is required"}), 400
    if target_language not in fastflowlm.VOICE_TRANSLATION_LANGUAGES:
        return jsonify({"error": "unsupported target language"}), 400
    video_bytes = video_file.read(50 * 1024 * 1024 + 1)
    if not video_bytes:
        return jsonify({"error": "empty video"}), 400
    if len(video_bytes) > 50 * 1024 * 1024:
        return jsonify({"error": "video exceeds the 50 MB limit"}), 413
    try:
        transcript = lemonade.transcribe_audio(
            video_bytes, filename=video_file.filename or "translation.mp4"
        )
        if not transcript:
            return jsonify({"error": "no speech was recognized"}), 422
        text = fastflowlm.translate_text(transcript, target_language)
    except (lemonade.LemonadeError, fastflowlm.FastFlowLMError) as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"text": text, "transcript": transcript, "target_language": target_language,
                    "model": "Gemma 4 E4B (Ryzen AI NPU)"})


@app.post("/api/voice/speak")
@login_required
def api_voice_speak():
    """Spoken responses: given assistant reply text, returns synthesized speech (MP3
    bytes, played directly by the browser's <audio> element - no base64/JSON wrapping,
    same as what Lemonade's own /v1/audio/speech hands back)."""
    body = request.get_json(force=True, silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "'text' is required"}), 400
    try:
        audio_bytes = lemonade.synthesize_speech(text, voice=body.get("voice") or lemonade.DEFAULT_TTS_VOICE)
    except lemonade.LemonadeError as exc:
        return jsonify({"error": str(exc)}), 502
    return Response(audio_bytes, mimetype="audio/mpeg")


@app.get("/api/images/<filename>")
@login_required
def api_get_image(filename):
    return send_from_directory(IMAGES_DIR, filename)


@app.get("/api/videos/<filename>")
@login_required
def api_get_video(filename):
    response = send_from_directory(VIDEOS_DIR, filename, conditional=True)
    response.headers["Cache-Control"] = "private, max-age=86400"
    return response


@app.post("/api/images/generate")
@login_required
def api_generate_image():
    body = request.get_json(force=True, silent=True) or {}
    prompt = (body.get("prompt") or "").strip()
    conversation_id = body.get("conversation_id")
    if not prompt:
        return jsonify({"error": "'prompt' is required"}), 400
    if not conversation_id:
        return jsonify({"error": "'conversation_id' is required"}), 400

    user_id = session["user_id"]
    chat_store.ensure_conversation(conversation_id, user_id, "sd-turbo (ComfyUI)", prompt)
    chat_store.add_message(conversation_id, "user", prompt)

    start_time = time.monotonic()
    image_model = lemonade.loaded_image_model()
    try:
        result = (
            lemonade.generate_image(
                prompt,
                model=image_model,
                steps=body.get("steps") or 4,
                width=body.get("width") or 512,
                height=body.get("height") or 512,
            )
            if image_model
            else comfyui.generate_image(
                prompt,
                negative_prompt=(body.get("negative_prompt") or "").strip(),
                steps=body.get("steps") or 2,
                width=body.get("width") or 512,
                height=body.get("height") or 512,
                seed=body.get("seed") or None,
            )
        )
    except (lemonade.LemonadeError, comfyui.ComfyUIError) as exc:
        return jsonify({"error": str(exc)}), 502
    duration = time.monotonic() - start_time

    filename = f"{uuid.uuid4().hex}.png"
    (IMAGES_DIR / filename).write_bytes(result["image_bytes"])

    content = f"![{prompt}](/api/images/{filename})"
    chat_store.add_message(
        conversation_id,
        "assistant",
        content,
        duration=duration,
        model=image_model or "sd-turbo (ComfyUI)",
        backend="lemonade" if image_model else "comfyui",
    )
    return jsonify({
        "ok": True,
        "content": content,
        "duration": duration,
        "seed": result.get("seed"),
        "model": image_model or "sd-turbo (ComfyUI)",
    })


def _extract_chat_document(filename, raw):
    """Extract text from supported document formats for conversation context."""
    ext = Path(filename).suffix.lower()
    if ext in {".txt", ".csv"}:
        return raw.decode("utf-8-sig", errors="replace")
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ValueError("PDF support requires pypdf") from exc
        return "\n\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(raw)).pages)
    if ext in {".docx", ".pptx", ".xlsx"}:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if ext == ".docx":
                selected = ["word/document.xml"]
            elif ext == ".pptx":
                selected = sorted(n for n in archive.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml"))
            else:
                selected = [n for n in archive.namelist() if n == "xl/sharedStrings.xml" or (n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))]
            values = []
            for name in selected:
                root = ET.fromstring(archive.read(name))
                values.extend(node.text.strip() for node in root.iter() if node.tag.endswith("}t") and node.text and node.text.strip())
            return "\n".join(values)
    raise ValueError("Supported formats: PDF, DOCX, PPTX, XLSX, TXT, CSV")


@app.post("/api/chat/start")
@login_required
def api_start_chat():
    # Multipart requests cannot be parsed with get_json(force=True).  Parse the
    # envelope from the form field first; otherwise Flask may reject every chat
    # turn that includes a document before the attachment branch is reached.
    if request.files:
        try:
            body = json.loads(request.form.get("payload", "{}"))
            documents = request.files.getlist("documents")
            if not documents or len(documents) > 5:
                return jsonify({"error": "Attach between 1 and 5 documents per message."}), 400
            parts = []
            for uploaded in documents:
                name = secure_filename(uploaded.filename or "document")
                raw = uploaded.read(15 * 1024 * 1024 + 1)
                if len(raw) > 15 * 1024 * 1024:
                    return jsonify({"error": f"{name} exceeds the 15 MB per-file limit."}), 413
                parts.append(f"[Attached document: {name}]\n{_extract_chat_document(name, raw)}")
            messages = body.get("messages")
            if not isinstance(messages, list) or not messages or not isinstance(messages[-1], dict) or messages[-1].get("role") != "user":
                return jsonify({"error": "A user message is required with document attachments."}), 400
            user = messages[-1]
            user["content"] = (user.get("content", "") + "\n\n" if user.get("content") else "") + "\n\n".join(parts)
            if len(user["content"]) > 2_000_000:
                return jsonify({"error": "Extracted document text exceeds the chat message limit."}), 413
        except (ValueError, KeyError, zipfile.BadZipFile) as exc:
            return jsonify({"error": f"Could not read attachment: {exc}"}), 400
    else:
        body = request.get_json(force=True, silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "request must be an object"}), 400
    messages, _, error = _validate_chat_request(body)
    if error:
        return jsonify({"error": error}), 400
    conversation_id = body.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        return jsonify({"error": "'conversation_id' is required"}), 400
    user_text = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    security = user_text.strip().lower() in {"/security-scan", "/security scan"}
    if security and session.get("role") != "admin":
        return jsonify({"error": "admin access required"}), 403
    if not body.get("model") and not security and not comfyui.looks_like_video_request(user_text):
        return jsonify({"error": "'model' is required"}), 400
    user_id = session["user_id"]
    # Serialize registration with persistence so repeat clicks cannot start two turns.
    with chat_jobs.registry.lock:
        old = chat_jobs.registry.get(user_id, conversation_id)
        if old and old.status == "running":
            return jsonify({"error": "This conversation already has a response in progress."}), 409
        try:
            chat_store.ensure_conversation(conversation_id, user_id, body.get("model"), user_text)
        except database.IntegrityError:
            # A conversation owned by another account must never be attached or modified.
            if chat_store.get_conversation(conversation_id, user_id) is None:
                return jsonify({"error": "not found"}), 404
            raise
        conv = chat_store.get_conversation(conversation_id, user_id)

        @copy_current_request_context
        def run(job):
            response = app.make_response(api_chat())
            try:
                if response.status_code >= 400:
                    data = response.get_json(silent=True) or {}
                    raise RuntimeError(data.get("error") or f"HTTP {response.status_code}")
                job.consume(response)
            finally:
                response.close()

        job = chat_jobs.registry.start(
            user_id, conversation_id, body.get("model"), user_text, len(conv["messages"]), run,
            preserved_reply=body.get("preserved_reply") if isinstance(body.get("preserved_reply"), str) else None,
        )
        if any(m["role"] == "user" for m in messages):
            chat_store.add_message(conversation_id, "user", user_text)
        job.user_saved = True
    return jsonify({"conversation_id": conversation_id, "generation": job.snapshot()}), 202


@app.post("/api/chat")
@login_required
def api_chat():
    body = request.get_json(force=True, silent=True) or {}
    messages, max_tokens, validation_error = _validate_chat_request(body)
    max_tool_rounds = _chat_limits()["chat_max_tool_rounds"]
    model = body.get("model")
    conversation_id = body.get("conversation_id")
    if validation_error:
        return jsonify({"error": validation_error}), 400
    if not conversation_id:
        return jsonify({"error": "'conversation_id' is required"}), 400

    user_id = session["user_id"]
    job = chat_jobs.registry.get(user_id, conversation_id)
    if request.path == "/api/chat" and job and job.status == "running":
        return jsonify({"error": "This conversation already has a response in progress."}), 409
    if request.path != "/api/chat/start":
        job = None
    cancel_event = job.cancel if job else threading.Event()
    client_ip = request.remote_addr  # captured now; the worker thread has no request context
    last_user_message = next(
        (m.get("content") for m in reversed(messages) if m.get("role") == "user"), None
    )
    is_security_scan = (last_user_message or "").strip().lower() in {
        "/security-scan", "/security scan"
    }
    video_request = comfyui.looks_like_video_request(last_user_message)
    if not model and not is_security_scan and not video_request:
        return jsonify({"error": "'model' is required"}), 400
    chat_store.ensure_conversation(
        conversation_id, user_id,
        model or ("Wan 2.1 T2V 1.3B" if video_request else "system-security-scan"),
        last_user_message,
    )
    if last_user_message is not None and not (job and job.user_saved):
        chat_store.add_message(conversation_id, "user", last_user_message)

    # Admin-only, fixed security command. This is intentionally handled before any
    # model routing: the model receives no shell capability, and ordinary chat text
    # can never be interpreted as a command. The scanner itself has a fixed read-only
    # allowlist, timeouts, and output caps (see security_scan.py).
    if is_security_scan:
        if session.get("role") != "admin":
            return jsonify({"error": "admin access required"}), 403
        report = security_scan.run_scan(BASE_DIR)
        chat_store.add_message(
            conversation_id,
            "assistant",
            report,
            model="system-security-scan",
            backend="local",
        )

        def stream_security_report():
            payload = {"choices": [{"delta": {"content": report}}]}
            yield f"data: {json.dumps(payload)}\n\n"
            yield "data: [DONE]\n\n"

        return Response(
            stream_with_context(stream_security_report()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Auto-detected so the user doesn't have to remember the separate "Generate image"
    # button - "draw me a cat" typed into the normal chat box and sent should just work.
    # Checked before the system-message/memory-recall block below since none of that
    # (language hint, cross-chat memory, web search) is relevant to an image request;
    # context_messages keeps the pre-injection conversation for prompt-enhancement context.
    image_request = not video_request and comfyui.looks_like_image_request(last_user_message)
    is_revision = False
    if (
        not image_request
        and comfyui.looks_like_image_followup(last_user_message)
        and _last_reply_was_image(messages)
    ):
        # "that's not clear" / "the face is wrong" said right after a generated image is
        # an image revision request, not a text question - see the README debugging case
        # study for why this exists: without this gate, that same feedback used to fall
        # through to the normal chat model, which had no way to actually act on it and
        # would sometimes hallucinate a fake ![...](/api/images/...) link imitating the
        # real ones earlier in the conversation (a broken, non-existent file, 404s in the
        # browser). looks_like_image_followup() alone is deliberately too broad to trust
        # on its own ("that's wrong" also happens in ordinary chat) - _last_reply_was_image()
        # is what keeps this scoped to genuine image-iteration conversations.
        image_request = True
        is_revision = True
    context_messages = messages
    browse_screenshots: list[dict] = []

    if not image_request and not video_request:
        # Bilingual models (e.g. GLM) can default to Chinese on short/ambiguous input (a
        # bare "hi") since nothing here otherwise tells them what language to answer in.
        # Only add this (and the cross-chat memory recall below) if the client didn't
        # already supply its own system message.
        has_system_message = any(m.get("role") == "system" for m in messages)
        system_parts: list[str] = []
        if not has_system_message:
            system_parts.extend([
                "Respond in the same language the user writes in. If that's unclear, default to English.",
                # Without this, a model that's seen real ![...](/api/images/...) links
                # earlier in the conversation (from this app's own image-generation
                # feature) will sometimes imitate that markdown pattern in its own text
                # reply, inventing a filename that doesn't exist on disk - see the image
                # generation README section's debugging case study for a confirmed
                # real-world instance of exactly this.
                "You cannot generate, create, edit, or output images or videos yourself - those happen "
                "through separate dedicated pipelines outside your control. Never invent media URLs or "
                "claim to have produced media. Ask the user to request it directly instead.",
            ])
        # Cross-chat RAG is an application capability, not a fallback system prompt.
        # Caller-supplied system messages must not disable retrieval (the old nesting
        # did exactly that for API clients and restored conversations).
        memory_note = _recall_memory(user_id, conversation_id, last_user_message)
        if memory_note:
            system_parts.append(memory_note)
        # Web access is independent of whether the caller supplied a system prompt.
        # Previously this entire block lived under `if not has_system_message`, so
        # API clients (and restored chats containing a system turn) silently skipped
        # live weather/news/search and prompted the model to claim it had no internet.
        web_enabled = (settings_store.get("web_search_enabled") or "true") == "true"
        web_mode = settings_store.get("web_search_mode") or "automatic"
        should_search = (
            web_enabled
            and web_search.should_search_web(last_user_message)
            and (
                web_mode == "automatic"
                or (web_mode == "explicit" and web_search.is_explicit_search_request(last_user_message))
            )
        )
        if should_search:
            research_query = web_search.contextualize_search_query(
                last_user_message, messages
            )
            # Apply a remembered current city before the live weather lookup. RAG
            # context alone arrives too late: without this, "How's the weather?"
            # searches without a location even though the user explicitly saved one.
            if (
                web_search.looks_like_weather_query(last_user_message)
                and not web_search.extract_weather_location(research_query)
            ):
                saved_location = memory_store.latest_location(user_id)
                if saved_location:
                    research_query = f"{last_user_message} in {saved_location['location']}"
            search_note, browse_screenshots = _web_search_context(research_query)
            if not search_note:
                search_note = (
                    "Hearth attempted a live web lookup for this request, but no verified "
                    "live result was available. Do not claim that you inherently lack web "
                    "access. State only that this particular live lookup failed, and ask "
                    "for a more specific topic or location if that would help."
                )
            if search_note:
                system_parts.append(search_note)
        if system_parts:
            # Put Hearth's live context after caller-provided system instructions so
            # the freshest operational fact (the lookup already happened) is closest
            # to the conversation and cannot be overridden by a generic "no internet"
            # capability disclaimer in an earlier prompt.
            system_messages = [m for m in messages if m.get("role") == "system"]
            conversation_messages = [m for m in messages if m.get("role") != "system"]
            system_content = "\n\n".join(
                [m.get("content", "") for m in system_messages] + system_parts
            )
            messages = [{"role": "system", "content": system_content}] + conversation_messages

    # Several local chat templates accept a system message only at the start, and
    # some reject more than one. Restored/API conversations can contain system turns
    # anywhere, so fold them into one leading instruction before routing to a backend.
    system_messages = [m for m in messages if m.get("role") == "system"]
    if system_messages:
        system_content = "\n\n".join(m.get("content", "") for m in system_messages)
        conversation_messages = [m for m in messages if m.get("role") != "system"]
        messages = [{"role": "system", "content": system_content}] + conversation_messages

    if lemonade.owns_model(model):
        backend, backend_error, backend_name = lemonade, lemonade.LemonadeError, "lemonade"
    elif vllm.owns_model(model):
        backend, backend_error, backend_name = vllm, vllm.VLLMError, "vllm"
    elif sglang.owns_model(model):
        backend, backend_error, backend_name = sglang, sglang.SGLangError, "sglang"
    elif kobold.owns_model(model):
        backend, backend_error, backend_name = kobold, kobold.KoboldError, "kobold"
    elif fastflowlm.owns_model(model):
        backend, backend_error, backend_name = fastflowlm, fastflowlm.FastFlowLMError, "fastflowlm"
    else:
        backend, backend_error, backend_name = lms, lms.LMStudioError, "lmstudio"

    def generate():
        if video_request:
            yield from _stream_video_reply(
                context_messages, last_user_message, model, backend, conversation_id
            )
            return
        if image_request:
            yield from _stream_image_reply(
                context_messages, last_user_message, model, backend, conversation_id, is_revision=is_revision
            )
            return

        # Stream consumption and the DB save both happen in a background thread,
        # decoupled from whether the client is still reading - confirmed live this was
        # a real, not theoretical, data-loss bug. A genuine ~96s generation (a
        # search+scrape-augmented query, longer response) completed fully server-side
        # (confirmed via Lemonade's own telemetry: 803 output tokens) and was even
        # fully spoken aloud through the voice feature, but the reply was never saved
        # and vanished from history entirely. Root cause: the old code's save lived in
        # this same generator's `finally` block, *after* a `yield` (the trailing "tops"
        # SSE chunk) - if the client has already disconnected by the time that yield
        # runs (very plausible for a 90+ second request: tab backgrounded, network
        # hiccup, laptop sleep), the write fails and raises right there, skipping every
        # line after it in that same finally block, chat_store.add_message() included.
        # Driving the actual Lemonade stream from an independent thread means a dropped
        # client connection can only ever lose the *client's* view of the reply -
        # never the save, since the worker isn't driven by the client pulling on this
        # generator at all, only by its own `for line in backend.chat_stream(...)` loop.
        result_queue: queue.Queue = queue.Queue()
        _SENTINEL = object()

        def worker():
            assistant_parts: list[str] = []
            reasoning_parts: list[str] = []
            usage: dict | None = None
            output_limited = False
            tool_round_limited = False
            start_time = time.monotonic()
            # fastflowlm is NPU-only; lemonade multiplexes several recipes (llamacpp, flm,
            # whispercpp, kokoro, ...) so only its flm-recipe models actually run on the NPU -
            # see lemonade_client.is_npu_model()'s docstring for why this can't be guessed
            # from the model id (e.g. "...-Coder-NPU" in a name has turned out to still be a
            # plain llamacpp/GGUF model, not an NPU one, confirmed live 2026-07-26).
            is_npu = backend is fastflowlm or (backend is lemonade and lemonade.is_npu_model(model))
            live_id = live_requests.start(
                source="webui-chat", backend=backend_name, model=model,
                max_tokens=max_tokens, is_npu=is_npu,
            )
            # MCP tools (browser automation, persistent Python REPL - see mcp_tools_client.py)
            # are available on every chat backend. Each backend has a guarded tool-call path
            # where needed; withholding the tool schema makes the model claim it cannot browse
            # even though Hearth's Playwright MCP server is installed and configured.
            use_tools = backend in (kobold, fastflowlm, sglang, lemonade, lms)
            tools_list = None
            if use_tools:
                try:
                    available_tools = mcp_tools_client.ensure_started()
                    tools_list = mcp_tools_client.select_chat_tools(available_tools) or None
                except Exception as exc:
                    print(f"[webui-chat] MCP tools unavailable, continuing without: {exc}", flush=True)
                    tools_list = None
            # FastFlowLM+tools routes through flm_tool_shim.py (see fastflowlm_client.chat_stream's
            # docstring) instead of the raw engine - the shim does its OWN NPU-lock acquisition
            # and model loading as a separate process, so claiming the lock here too would
            # deadlock (both block on the same machine-wide flock, and this thread wouldn't
            # release it until the shim's HTTP response arrives, which never comes because the
            # shim itself is stuck waiting on that same lock). Skip our own claim for this one
            # case; every other is_npu path is unaffected.
            use_flm_shim = backend is fastflowlm and tools_list
            npu_handle = None
            try:
                if is_npu and not use_flm_shim:
                    npu_handle = _claim_selected_npu(backend, model)
                extra_kwargs = {}
                if backend is lemonade:
                    overrides = _lemonade_model_thinking_overrides()
                    thinking = overrides.get(model, (settings_store.get("lemonade_enable_thinking") or "true") == "true")
                    # Newer Lemonade servers map the top-level control to llama.cpp's
                    # native reasoning setting. Keep chat_template_kwargs for runtimes
                    # that honor the Qwen chat-template switch directly.
                    extra_kwargs["enable_thinking"] = thinking
                    extra_kwargs["chat_template_kwargs"] = {"enable_thinking": thinking}
                if tools_list:
                    extra_kwargs["tools"] = tools_list

                loop_messages = list(messages)
                if tools_list:
                    tool_instruction = (
                        "For online lookups, use the offered Playwright tools and inspect their returned evidence. "
                        "After page-changing actions, Hearth waits and inspects every open tab, including popups. "
                        "Use tabs, snapshots, find, click, and back as needed to investigate; do not stop at a "
                        "loading message or claim a page was checked without its returned snapshot."
                    )
                    first_system = next(
                        (m for m in loop_messages if m.get("role") == "system"), None
                    )
                    if first_system is not None:
                        existing_content = first_system.get("content") or ""
                        first_system["content"] = f"{existing_content}\n\n{tool_instruction}"
                    else:
                        loop_messages.insert(0, {"role": "system", "content": tool_instruction})
                tool_round = 0
                while True:
                    if cancel_event.is_set():
                        break
                    round_tool_calls: dict[int, dict] = {}
                    finish_reason = None
                    backend_lines = backend.chat_stream(
                        loop_messages,
                        model,
                        temperature=body.get("temperature", 0.7),
                        max_tokens=max_tokens,
                        **extra_kwargs,
                    )
                    backend_lines = chat_jobs.cancelable_lines(backend_lines, cancel_event)
                    # Small quantized LM Studio and Lemonade/llama.cpp models can collapse
                    # into a run of one punctuation token (confirmed live with endless '/'
                    # from nullsumlabs-qwen3.8-9b-coder). Buffer those backends' rounds so
                    # the junk never reaches the browser or history.
                    if backend in (lms, lemonade):
                        backend_lines = generation_guard.guard_sse_stream(backend_lines)
                    for line in backend_lines:
                        result_queue.put(f"{line}\n\n")
                        if line.startswith("data:"):
                            payload = line[len("data:"):].strip()
                            if payload and payload != "[DONE]":
                                try:
                                    obj = json.loads(payload)
                                except json.JSONDecodeError:
                                    obj = None
                                if obj:
                                    try:
                                        choice = obj["choices"][0]
                                        delta_obj = choice["delta"]
                                    except (KeyError, IndexError):
                                        choice, delta_obj = {}, {}
                                    finish_reason = choice.get("finish_reason") or finish_reason
                                    if finish_reason == "length":
                                        output_limited = True
                                    delta = delta_obj.get("content")
                                    if delta:
                                        assistant_parts.append(delta)
                                        live_requests.update(live_id, delta_chars=len(delta))
                                    reasoning_delta = delta_obj.get("reasoning_content")
                                    if reasoning_delta:
                                        reasoning_parts.append(reasoning_delta)
                                        live_requests.update(live_id, delta_chars=len(reasoning_delta))
                                    # Streamed incrementally like content deltas: `name` normally
                                    # arrives whole in the first chunk for a given tool call,
                                    # `arguments` arrives piecemeal across many chunks and must
                                    # be concatenated, same as any other OpenAI-shaped tool-call
                                    # stream (this is exactly what flm_tool_shim.py's own
                                    # degeneracy/repair logic reconstructs before ever showing
                                    # it to OpenCode - same accumulation, different consumer).
                                    for tc_delta in delta_obj.get("tool_calls") or []:
                                        idx = tc_delta.get("index", 0)
                                        entry = round_tool_calls.setdefault(
                                            idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                                        )
                                        if tc_delta.get("id"):
                                            entry["id"] = tc_delta["id"]
                                        fn_delta = tc_delta.get("function") or {}
                                        if fn_delta.get("name"):
                                            entry["function"]["name"] += fn_delta["name"]
                                        if fn_delta.get("arguments"):
                                            entry["function"]["arguments"] += fn_delta["arguments"]
                                    if obj.get("usage"):
                                        usage = obj["usage"]
                    if cancel_event.is_set() or finish_reason != "tool_calls" or not round_tool_calls:
                        break
                    if tool_round >= max_tool_rounds:
                        tool_round_limited = True
                        break
                    tool_round += 1
                    tool_calls_list = [round_tool_calls[i] for i in sorted(round_tool_calls)]
                    loop_messages.append({"role": "assistant", "content": None, "tool_calls": tool_calls_list})
                    for tc in tool_calls_list:
                        if cancel_event.is_set():
                            break
                        tc_name = tc["function"]["name"]
                        # Visible in the chat bubble via the same content-delta channel the
                        # model's own text uses - no frontend change needed to show it.
                        marker = f"\n\n🔧 *Calling `{tc_name}`...*\n"
                        assistant_parts.append(marker)
                        result_queue.put(
                            f"data: {json.dumps({'choices': [{'delta': {'content': marker}}]})}\n\n"
                        )
                        tool_result = mcp_tools_client.call_tool(tc_name, tc["function"]["arguments"])
                        if backend is fastflowlm:
                            result_limit = (
                                mcp_tools_client.CHAT_BROWSER_INSPECTION_LIMIT
                                if tc_name in {
                                    "playwright__browser_navigate", "playwright__browser_navigate_back",
                                    "playwright__browser_click", "playwright__browser_type",
                                    "playwright__browser_fill_form", "playwright__browser_select_option",
                                    "playwright__browser_wait_for", "playwright__browser_tabs",
                                }
                                else mcp_tools_client.CHAT_TOOL_RESULT_LIMIT
                            )
                            tool_result = mcp_tools_client.compact_chat_tool_result(tool_result, result_limit)
                        loop_messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id") or f"call_{tool_round}_{tc_name}",
                            "content": tool_result,
                        })
            except backend_error as exc:
                live_requests.finish(live_id, status="error")
                result_queue.put(f"data: {json.dumps({'error': str(exc)})}\n\n")
            finally:
              # Everything in this tail runs inside one more try/finally whose only
              # job is guaranteeing the sentinel gets queued: any unexpected exception
              # here (confirmed live 2026-07-23 — a malformed Lemonade embeddings
              # response raised a bare KeyError out of _remember_exchange) would
              # otherwise kill the thread pre-sentinel and leave the client's SSE
              # generator blocked on the queue forever, a hung chat with no error.
              try:
                assistant_text = "".join(assistant_parts)
                reasoning_text = "".join(reasoning_parts)
                if output_limited:
                    assistant_text += (
                        "\n\n> ⚠️ Generation reached the output-token limit and may be incomplete. "
                        "Ask the model to continue from the last complete line."
                    )
                if tool_round_limited:
                    assistant_text += (
                        f"\n\n> ⚠️ Reached the tool-call round limit ({max_tool_rounds}) while the model was still "
                        "requesting more tool calls. Ask it to continue or narrow the task."
                    )
                if assistant_text and browse_screenshots:
                    # Appended server-side, never by the model (the system prompt
                    # explicitly forbids the model writing image markdown — see the
                    # anti-imitation note where system_parts is built). Uses the same
                    # ![](/api/images/...) shape as generated images so the frontend
                    # renders it with zero changes and _delete_generated_images'
                    # existing regex cleans these files up on conversation delete too.
                    shot_lines = ["", "", "**Pages browsed (screenshots):**", ""]
                    for shot in browse_screenshots:
                        # Brackets/parens stripped: they'd terminate the ![...](...)
                        # markdown early and break the image render.
                        label = re.sub(r"[\[\]()]", "", (shot.get("title") or shot.get("href") or "page")).strip()[:120]
                        shot_lines.append(f"![{label}](/api/images/{shot['file']})")
                        # Content images pulled from the page itself (og:image /
                        # largest rendered <img>s — see page_scraper._download_images),
                        # e.g. the actual product photo on a retailer page, not just
                        # our screenshot of the page.
                        for img in shot.get("images") or []:
                            shot_lines.append(f"![image from {label}](/api/images/{img})")
                        if shot.get("href"):
                            shot_lines.append(f"*{shot['href']}*")
                        shot_lines.append("")
                    shots_markdown = "\n".join(shot_lines)
                    assistant_text += shots_markdown
                    # Trailing chunk after [DONE], same pattern as 'tops' below — the
                    # client appends this to the streamed text so the screenshots show
                    # up live, not only after a reload pulls the saved message.
                    result_queue.put(
                        f"data: {json.dumps({'screenshots_markdown': shots_markdown})}\n\n"
                    )
                if assistant_text or reasoning_text:
                    duration = time.monotonic() - start_time
                    npu_percent = tops_used = tops_max = None
                    if backend is lemonade:
                        # Sampled after the fact, not averaged over the response —
                        # neither backend reports per-request compute the way they
                        # report per-request tokens, so this is an instantaneous
                        # GET /v1/system-stats reading taken right as generation
                        # finishes, the closest real signal available. LM Studio has no
                        # NPU/TOPS concept at all, hence the backend-is-lemonade guard.
                        npu_percent = lemonade.npu_utilization_percent()
                        tops_max = lemonade.npu_tops_max()
                        if npu_percent is not None and tops_max is not None:
                            tops_used = round(npu_percent / 100 * tops_max, 1)
                        # Queued for the client same as before (a trailing chunk after
                        # [DONE], picked up if the client's still connected) - but
                        # queuing never raises the way a direct yield-to-a-dead-socket
                        # would, so this can no longer take the DB save down with it.
                        result_queue.put(
                            f"data: {json.dumps({'tops': {'npu_percent': npu_percent, 'tops_used': tops_used, 'tops_max': tops_max}})}\n\n"
                        )
                    chat_store.add_message(
                        conversation_id,
                        "assistant",
                        assistant_text,
                        duration=duration,
                        model=model,
                        backend=backend_name,
                        prompt_tokens=(usage or {}).get("prompt_tokens"),
                        completion_tokens=(usage or {}).get("completion_tokens"),
                        total_tokens=(usage or {}).get("total_tokens"),
                        reasoning=reasoning_text or None,
                        npu_percent=npu_percent,
                        tops_used=tops_used,
                        tops_max=tops_max,
                    )
                    # Performance-metrics row for the admin metric board (fail-soft inside).
                    _ct = (usage or {}).get("completion_tokens")
                    metrics_store.record(
                        source="webui-chat",
                        backend=backend_name,
                        model=model,
                        request_type="chat_completion",
                        ask_category=("web-search" if browse_screenshots else "chat"),
                        duration_ms=round(duration * 1000, 1),
                        prompt_tokens=(usage or {}).get("prompt_tokens"),
                        completion_tokens=_ct,
                        total_tokens=(usage or {}).get("total_tokens"),
                        tokens_per_sec=round(_ct / duration, 1) if (_ct and duration > 0) else None,
                        tops_used=tops_used,
                        npu_percent=npu_percent,
                        status=200,
                        client_ip=client_ip,
                        user_id=user_id,
                        is_npu=is_npu,
                    )
                    _remember_exchange(user_id, conversation_id, last_user_message, assistant_text)
              finally:
                live_requests.finish(live_id, status="done", total_tokens=(usage or {}).get("completion_tokens"))
                npu_request_lock.release(npu_handle)
                result_queue.put(_SENTINEL)

        threading.Thread(target=worker, daemon=True).start()
        while True:
            item = result_queue.get()
            if item is _SENTINEL:
                break
            yield item

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _local_ipv4_addresses() -> list[str]:
    """Best-effort discovery of this machine's LAN IPv4 addresses."""
    import subprocess

    try:
        out = subprocess.run(
            ["hostname", "-I"], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception:
        out = ""
    if not out.strip():
        # `hostname -I` is not available on macOS. Use the default route to discover
        # the interface address there, without sending any traffic.
        try:
            import socket
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.connect(("192.0.2.1", 80))
                out = probe.getsockname()[0]
            finally:
                probe.close()
        except Exception:
            return []
    addrs = []
    for tok in out.split():
        try:
            ip = ipaddress.ip_address(tok)
        except ValueError:
            continue
        if ip.version == 4 and not ip.is_loopback:
            addrs.append(tok)
    return addrs


def ensure_self_signed_cert() -> None:
    if CERT_FILE.exists() and KEY_FILE.exists():
        return
    CERT_DIR.mkdir(exist_ok=True)

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ai-webui.local")])
    now = datetime.datetime.now(datetime.timezone.utc)

    san_entries = [
        x509.DNSName("localhost"),
        x509.DNSName("ai-webui.local"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    for ip_str in _local_ipv4_addresses():
        san_entries.append(x509.IPAddress(ipaddress.ip_address(ip_str)))

    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(
            x509.SubjectAlternativeName(san_entries),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    KEY_FILE.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    print(f"Generated self-signed certificate at {CERT_FILE}")


def _warm_up_rag_and_voice_models() -> None:
    """Pre-loads the embedding (RAG/cross-chat memory), STT, and TTS models on Lemonade at
    startup, rather than waiting for the first request that needs them. Without this, a fresh
    ai-webui.service restart left RAG/voice cold until first use — usually fine (they lazy-load
    on demand via _ensure_embedding_model_loaded()/_ensure_voice_model_loaded()), but the first
    real request after any restart would eat that load latency, and if Lemonade itself was still
    coming up when the request landed, that request could fail outright rather than just being
    slow. Runs in a background thread so a slow/unreachable Lemonade never delays this process
    binding its own port. Each call is independently fail-soft (LemonadeError only, never
    raises) — matches the pattern already used everywhere else these models are touched, so one
    missing/broken model doesn't block the other two from warming up."""
    if settings_store.get("support_models_keep_loaded", "true") == "false":
        return
    for label, fn in (
        ("embedding (RAG)", lambda: _ensure_embedding_model_loaded()),
        ("STT (voice input)", lambda: lemonade._ensure_voice_model_loaded(lemonade.STT_MODEL_ID)),
        ("TTS (voice output)", lambda: lemonade._ensure_voice_model_loaded(lemonade.TTS_MODEL_ID)),
    ):
        try:
            fn()
        except lemonade.LemonadeError as exc:
            print(f"Warm-up: couldn't load {label} model: {exc}")


def _keep_support_models_loaded() -> None:
    """Keep the default RAG and voice models resident for the life of the web app.

    Lemonade may evict an idle model even after the startup warm-up succeeds. Rechecking once a
    minute is cheap (``is_model_loaded`` only reads ``/v1/health``) and also restores the defaults
    automatically if Lemonade is restarted after this service. The ensure helpers remain
    independently fail-soft, so a temporarily unavailable backend cannot stop the web UI or
    prevent the other support models from loading.
    """
    while True:
        _warm_up_rag_and_voice_models()
        time.sleep(60)


def _restore_default_model() -> None:
    """Activate Settings' backend/model default after Hearth starts.

    The setting was previously used only when generating client configuration, so a service
    restart could leave a different model active. Startup now applies the same selection to
    the actual backend. NPU defaults use the normal exclusive-ownership handoff.
    """
    configured = (settings_store.get("lan_default_model") or "").strip()
    backend_name, separator, model = configured.partition("/")
    if not separator or not model:
        print("Startup model: Default LAN model is Auto; leaving backend state unchanged.", flush=True)
        return

    clients = {
        "lmstudio": lms,
        "lemonade": lemonade,
        "vllm": vllm,
        "sglang": sglang,
        "kobold": kobold,
        "fastflowlm": fastflowlm,
    }
    backend = clients.get(backend_name)
    if backend is fastflowlm and fastflowlm.requests_paused():
        print("Startup model: preserving manual FastFlowLM unload.", flush=True)
        return
    if backend is None:
        print(f"Startup model: unknown backend in saved default: {backend_name}", flush=True)
        return

    try:
        # Validate before changing NPU ownership. A removed/stale default previously
        # stopped the currently working FastFlow service first, then failed to load the
        # missing Lemonade model, leaving chat with no usable LLM after every restart.
        # Keep the existing backend untouched when the configured model no longer exists.
        if not backend.owns_model(model):
            print(
                f"Startup model: saved default is unavailable: {configured}; "
                "leaving backend state unchanged.",
                flush=True,
            )
            return
        if backend is fastflowlm:
            fastflowlm.check_loadable(model)
        if backend is sglang:
            # A remote SGLang deployment does not consume this machine's resources;
            # selecting it must not evict a useful local model during app startup.
            sglang.verify_model(model)
            print(f"Startup model: verified {backend_name}/{model}", flush=True)
            return
        with _reasoning_model_load_lock:
            # Startup used to bypass /api/models/load entirely. That left any vLLM or
            # Kobold service surviving from the previous session loaded alongside the
            # restored default, despite the one-reasoning-model rule.
            _unload_reasoning_models_before_load(model)
            is_npu = backend is fastflowlm or (
                backend is lemonade and lemonade.is_npu_model(model)
            )
            if is_npu:
                handle = _claim_selected_npu(backend, model)
                npu_request_lock.release(handle)
            elif backend is vllm:
                vllm.load_model(model)
            elif backend is kobold:
                kobold.load_model(model)
            elif backend is lemonade:
                lemonade.load_model(model, context_length=65536)
            else:
                if not any(
                    entry.get("id") == model and entry.get("state") == "loaded"
                    for entry in lms.list_models()
                ):
                    lms.load_model(model, context_length=32768)
        print(f"Startup model: restored {backend_name}/{model}", flush=True)
    except Exception as exc:
        # Startup must remain fail-soft: the web UI should still come online when an
        # optional backend/model is missing, with a precise journal entry for diagnosis.
        print(f"Startup model: couldn't restore {configured}: {exc}", flush=True)


def _warm_up_startup_models() -> None:
    if _agent_tasks.busy():
        print("Startup: preserving the backend for persistent agent assignments.", flush=True)
        return
    # Run sequentially so support-model warm-up and the selected LLM do not compete for
    # Lemonade's model manager during a cold boot.
    threading.Thread(target=_keep_support_models_loaded, daemon=True).start()
    _restore_default_model()


if __name__ == "__main__":
    ensure_self_signed_cert()

    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(certfile=str(CERT_FILE), keyfile=str(KEY_FILE))

    host = os.environ.get("HOST", "0.0.0.0")
    port = APP_PORT

    print(f"LM Studio backend: {lms.base_url()}")
    print(f"Lemonade (Ryzen AI NPU) backend: {lemonade.base_url()}")
    print(f"Serving on https://{host}:{port}  (self-signed cert, browser will warn once)")
    for ip in _local_ipv4_addresses():
        print(f"LAN access: https://{ip}:{port}")
    ts_dns = help_data.tailscale_dns_name()
    ts_url = help_data.tailscale_app_url(ts_dns, port)
    if ts_url:
        print(f"Tailscale access: {ts_url}")
    threading.Thread(target=_warm_up_startup_models, daemon=True).start()
    _agent_tasks.start_worker(lambda: not any(j["status"] in model_tests.ACTIVE for j in _model_test_registry.list()))
    app.run(host=host, port=port, ssl_context=ssl_context, threaded=True)
