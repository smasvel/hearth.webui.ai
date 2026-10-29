"""Tiny reverse-proxy shim between Caddy's :8444 LAN endpoint and Lemonade's own server
(127.0.0.1:13305) — mirrors flm_tool_shim.py's role, but for the opposite direction of the
same NPU-exclusivity conflict.

Real incident (2026-07-26): Lemonade auto-loads a model on demand when a chat request names
one that isn't loaded yet (the same JIT-style behavior LM Studio has). When that model is an
flm-recipe (NPU) one, Lemonade launches its own independent flm-real process — completely
separate from the standalone flm.service used by OpenCode/Continue's tool-calling path
(flm_tool_shim.py, Caddy's :8447) — and the two together can exceed this machine's RAM.
Confirmed live: manually triggering Lemonade's own load API for its copy of
qwen3.6-moe-35b-a3b got flm.service OOM-killed by the kernel within seconds, breaking
whatever request flm.service was serving at the time.

flm_tool_shim.py already guards the OTHER direction — it evicts Lemonade's NPU model before
forwarding a request to FastFlowLM, because it's the one process genuinely inline for every
real FastFlowLM request. This shim closes the reverse direction the same way: sitting inline
for every real Lemonade request, it evicts the standalone flm.service *before* forwarding a
chat request for an flm-recipe model to Lemonade. Every other request (any non-NPU Lemonade
model — embeddings, whisper, kokoro, llamacpp-recipe chat models — or any non-chat endpoint)
is proxied through completely unmodified; those never touch the NPU at all, so there's nothing
to guard.

Runs as its own systemd --user unit (lemonade-guard-shim.service) on 127.0.0.1:13306. Caddy's
:8444 block points its `reverse_proxy` here instead of Lemonade's raw 13305 so LAN clients get
the guard; this app's own lemonade_client.py talks to Lemonade directly on 13305 and is
unaffected either way — same relationship fastflowlm_client.py has with flm_tool_shim.py, and
for the same reason (app.py's own load path already runs _free_npu_except() itself).
"""
from __future__ import annotations

import json
import queue
import re
import threading
import time

import requests
import model_test_profiles
import question_tool_compat
import exact_file_content
import tool_argument_json
import exact_tool_contract
import agent_request_policy
import granite_tool_compat
from flask import Flask, Response, request

import fastflowlm_client as fastflowlm
import live_report
import npu_request_lock
import generation_guard
import lemonade_templates
from flm_tool_shim import _repair_streamed_textual_tool_calls, _repair_textual_tool_calls

LEMONADE_BASE = "http://127.0.0.1:13305"
LISTEN_PORT = 13306
_UTF8_RETRY_LIMIT = 2

app = Flask(__name__)
_NPU_REQUEST_LOCK = threading.Lock()


def _normalize_tool_choice(body: dict) -> dict:
    """Translate OpenAI named-function forcing to required-only form for Lemonade."""
    choice = body.get("tool_choice")
    if not isinstance(choice, dict):
        return body
    function = choice.get("function")
    name = function.get("name") if isinstance(function, dict) else None
    if choice.get("type") != "function" or not isinstance(name, str) or not name:
        raise ValueError("tool_choice must name an offered function")
    selected = [tool for tool in body.get("tools", [])
                if tool.get("type") == "function" and (tool.get("function") or {}).get("name") == name]
    if len(selected) != 1:
        raise ValueError("tool_choice must name exactly one offered function")
    return {**body, "tools": selected, "tool_choice": "required"}

_HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "content-encoding", "content-length"}


def _forward_headers(headers) -> dict:
    # Hearth uses this label for local metrics attribution only. Do not expose it
    # to the model server, which may reject or interpret unknown request headers.
    return {k: v for k, v in headers.items()
            if k.lower() not in _HOP_BY_HOP
            and k.lower() not in {"host", "x-hearth-agent-name", "x-hearth-agent-relay"}}


def _source_from_headers(headers) -> str:
    """Prefer a named Hearth agent, then the client label configured for other callers."""
    agent_name = (headers.get("X-Hearth-Agent-Name") or headers.get("x-hearth-agent-name") or "").strip()
    if agent_name and len(agent_name) <= 120 and not any(ord(char) < 32 for char in agent_name):
        return agent_name
    client = (headers.get("X-Hearth-Client") or "").lower()
    return client if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", client) else "lan"


def _client_ip(headers, fallback: str | None) -> str | None:
    forwarded = headers.get("X-Forwarded-For") or ""
    return forwarded.split(",", 1)[0].strip() or fallback


def _request_trace(body: dict) -> str:
    """Readable prompt trace only; deliberately omit headers, credentials and tool schemas."""
    rows = []
    for message in body.get("messages") or []:
        role = str(message.get("role") or "unknown")
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(
                str(part.get("text") or f"[{part.get('type', 'content')}]")
                for part in content if isinstance(part, dict)
            )
        if content:
            rows.append(f"{role}: {content}")
        if message.get("tool_calls"):
            rows.append(f"{role}: [tool calls: {len(message['tool_calls'])}]")
    return "\n".join(rows)[-4000:]


def _split_live_reasoning(obj: dict) -> tuple[dict | None, dict]:
    """Stream reasoning now while keeping content/tool calls buffered for validation.

    Mirrors flm_tool_shim._split_live_reasoning. Reasoning models (GLM-4.7-Flash,
    Qwen3-thinking) can spend minutes in the think phase before emitting any answer, so a
    client that only sees chunks after the whole response is buffered (OpenCode, whose
    @ai-sdk/openai-compatible provider renders ``delta.reasoning_content`` as a first-class
    reasoning stream part) looks completely hung for that whole stretch. Reasoning is never
    part of a tool call, so it can be forwarded the moment it arrives; only the content /
    tool-call deltas actually need the whole-response repair + degeneracy guard below.
    Returning a stripped ``obj`` keeps replay from displaying the reasoning twice.
    """
    choices = obj.get("choices") or []
    if not choices or not (choices[0].get("delta") or {}).get("reasoning_content"):
        return None, obj
    choice = choices[0]
    delta = choice["delta"]
    live = {k: v for k, v in obj.items() if k not in {"choices", "usage"}}
    live["choices"] = [{**choice, "delta": {"reasoning_content": delta["reasoning_content"]},
                        "finish_reason": None}]
    buffered_delta = {k: v for k, v in delta.items() if k != "reasoning_content"}
    buffered = {**obj, "choices": [{**choice, "delta": buffered_delta}, *choices[1:]]}
    return live, buffered


def _prepare_agent_request(body: dict) -> dict:
    return agent_request_policy.prepare(body)


def _tool_schemas(request_body: dict | None) -> dict[str, dict]:
    schemas = {}
    for tool in ((request_body or {}).get("tools") or []):
        function = tool.get("function") or {}
        name = function.get("name")
        if isinstance(name, str):
            schemas[name.lower()] = function.get("parameters") or {}
    return schemas


def _clean_tool_key(key: str) -> str:
    """Undo the quote suffix Qwen occasionally samples on camelCase schema keys."""
    return key.strip().strip("\"'")


def _clean_path(value):
    if not isinstance(value, str):
        return value
    # Do not alter legitimate spaces in paths; only remove the sampled wrapper/suffix quotes.
    return value.strip().strip("\"'")


def _looks_degenerate_file_content(value) -> bool:
    """Conservative detector for the observed long Unicode/punctuation model collapse."""
    if not isinstance(value, str) or len(value) < 500:
        return False
    tail = value[-1500:]
    non_ascii = sum(ord(char) > 127 for char in tail) / len(tail)
    punctuation = sum(not char.isalnum() and not char.isspace() for char in tail) / len(tail)
    repeated_runs = len(re.findall(r"(?:[):=.;\"']{4,})", tail))
    return non_ascii > 0.15 and punctuation > 0.18 and repeated_runs >= 2


def _repair_tool_arguments(obj: dict, request_body: dict | None = None) -> bool:
    """Repair common sampled schema aliases before OpenCode validates a tool call.

    If a required file path is genuinely absent, do not guess where the user wanted to
    write. Replace the call with a harmless bash result that tells the agent to retry using
    the exact schema. This keeps the agent loop alive instead of terminating at SchemaError.
    """
    try:
        tool_calls = obj["choices"][0]["delta"].get("tool_calls") or []
    except (KeyError, IndexError, TypeError):
        return False
    repaired = False
    schemas = _tool_schemas(request_body)
    available_tools = set(schemas)
    recent_history = "\n".join(
        str(message.get("content") or "")
        for message in (request_body or {}).get("messages", [])[-12:]
    )
    repeated_replace_failure = len(re.findall(
        r"(?:replace_in_file|SEARCH block|exact match).{0,180}"
        r"(?:fail|not found|did not match|exact matching)",
        recent_history, re.I | re.S,
    )) >= 2 or bool(re.search(
        r"failed twice.{0,300}(?:write_to_file|write to file)",
        recent_history, re.I | re.S,
    ))
    for tool_call in tool_calls:
        function = tool_call.get("function") or {}
        name = function.get("name")
        if not isinstance(name, str):
            continue
        clean_name = name.strip().strip("\"'").lower()
        if clean_name != name.lower() and clean_name in available_tools:
            function["name"] = clean_name
            repaired = True
        name = clean_name
        tool_kind = (
            "edit" if name in ("edit", "replace_in_file", "replace") else
            "write" if name in ("write", "write_to_file", "create_file") else
            "read" if name in ("read", "read_file") else
            "patch" if name in ("patch", "apply_patch") else
            name
        )
        raw_args = function.get("arguments")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(args, dict):
            continue

        normalized = {}
        for key, value in args.items():
            clean_key = _clean_tool_key(str(key))
            # Observed malformed form: {"filePath=/absolute/path": ""}.
            match = re.fullmatch(r"(?i)filePath\s*=\s*(.+)", clean_key)
            if match and not value:
                normalized["filePath"] = _clean_path(match.group(1))
            else:
                normalized[clean_key] = value
        args = normalized

        aliases = {
            "filePath": ("path", "file_path", "filepath", "filename", "file",
                         "target", "targetPath"),
            "content": ("text", "data", "body"),
            "oldString": ("old_string", "oldText", "search", "find"),
            "newString": ("new_string", "newText", "replace", "replacement"),
            "command": ("cmd", "script", "input"),
        }
        properties = (schemas.get(name) or {}).get("properties") or {}
        for canonical, candidates in aliases.items():
            if canonical not in properties or canonical in args:
                continue
            for alias in candidates:
                if alias in args and args[alias] not in (None, ""):
                    args[canonical] = args.pop(alias)
                    repaired = True
                    break
        if "filePath" in args:
            cleaned_path = _clean_path(args["filePath"])
            if cleaned_path != args["filePath"]:
                repaired = True
            args["filePath"] = cleaned_path

        required = set((schemas.get(name) or {}).get("required") or [])
        missing = [key for key in required if args.get(key) in (None, "")]
        corrupt_write = tool_kind in ("write", "edit", "patch") and _looks_degenerate_file_content(
            args.get("content") or args.get("newString")
        )
        if (missing or corrupt_write) and tool_kind in ("read", "write", "edit", "patch"):
            reason = (
                f"Hearth blocked malformed {name} tool arguments"
                + (f" missing required key(s): {', '.join(sorted(missing))}" if missing else
                   " because the file content was corrupted")
                + ". Retry the structured tool with the exact schema keys and an absolute path."
            )
            if "bash" in available_tools:
                function["name"] = "bash"
                function["arguments"] = json.dumps(
                    {"command": f"printf '%s\\n' {json.dumps(reason)}"},
                    separators=(",", ":"),
                )
                repaired = True
            continue

        if tool_kind == "write" and repeated_replace_failure:
            read_name = next(
                (candidate for candidate in ("read", "read_file") if candidate in available_tools),
                None,
            )
            reason = (
                "Hearth blocked a whole-file write fallback after repeated exact-match "
                "failures. Re-read the existing file, then use a smaller exact replacement "
                "or structured patch; preserve unrelated content."
            )
            if read_name and args.get("filePath"):
                function["name"] = read_name
                function["arguments"] = json.dumps(
                    {"filePath": args["filePath"]}, separators=(",", ":")
                )
            elif "bash" in available_tools:
                function["name"] = "bash"
                function["arguments"] = json.dumps(
                    {"command": f"printf '%s\\n' {json.dumps(reason)}"},
                    separators=(",", ":"),
                )
            repaired = True
            continue

        if name not in ("bash", "shell"):
            serialized = json.dumps(args, separators=(",", ":"))
            if serialized != raw_args:
                function["arguments"] = serialized
                repaired = True
            continue

        command = args.get("command")
        has_file_tool = any(
            token in name for name in available_tools
            for token in ("write", "edit", "patch")
        )
        # A nested python/shell string is exactly how the observed response collapsed into
        # repeated punctuation and would have overwritten the user's file with garbage.
        # Keep the agent loop alive with a harmless command result that tells the model to
        # retry through OpenCode's structured file tool.
        embedded_file_write = bool(
            isinstance(command, str) and (
                re.search(r"\bpython(?:3)?\s+-c\b", command)
                and re.search(r"\bopen\s*\([^)]*['\"]w['\"]", command)
                and re.search(r"\.write\s*\(", command)
            )
        )
        suspicious_collapse = bool(
            isinstance(command, str) and len(command) > 300
            and re.search(r"(?:[):=.;\"']{4,}){3,}", command)
        )
        if command and (suspicious_collapse or (has_file_tool and embedded_file_write)):
            reason = (
                "Hearth blocked a malformed bash file-write command. "
                "Retry with the dedicated write/edit tool and verify the file afterward."
            )
            args["command"] = f"printf '%s\\n' {json.dumps(reason)}"
            repaired = True
        serialized = json.dumps(args, separators=(",", ":"))
        if serialized != raw_args:
            function["arguments"] = serialized
            repaired = True
    return repaired


def _record_chat_metric(*, started: float, source: str, model: str, is_npu: bool,
                        status: int, usage: dict | None, client_ip: str | None) -> None:
    usage = usage or {}
    duration_ms = round((time.monotonic() - started) * 1000, 1)
    completion = usage.get("completion_tokens")
    tps = usage.get("decoding_speed_tps")
    if tps is None and isinstance(completion, (int, float)) and duration_ms > 0:
        tps = round(completion / (duration_ms / 1000), 3)
    live_report.record_metric(
        source=source, backend="lemonade", model=model,
        request_type="chat_completion",
        ask_category="code" if source in ("opencode", "vscode", "vscode_continue") else "chat",
        duration_ms=duration_ms, prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=completion, total_tokens=usage.get("total_tokens"),
        tokens_per_sec=tps, status=status, client_ip=client_ip, is_npu=is_npu,
    )


def _is_npu_model(model_id: str | None) -> bool:
    """True if `model_id` is an flm-recipe (NPU) model in Lemonade's own catalog — the only
    kind that contends with FastFlowLM for the NPU. Fails closed toward "not NPU" on any
    lookup error: this must never block a Lemonade request that would otherwise be fine, and
    a lookup failure here just means the eviction below is skipped, not that the request is."""
    if not model_id:
        return False
    try:
        resp = requests.get(f"{LEMONADE_BASE}/v1/models", params={"show_all": "true"}, timeout=4)
        resp.raise_for_status()
        for m in resp.json().get("data", []):
            if m.get("id") == model_id:
                return m.get("recipe") == "flm"
    except requests.RequestException:
        pass
    return False


def _upstream_error_detail(resp) -> str | None:
    """Short backend error for the developer log; never expose the request body."""
    if resp.ok:
        return None
    try:
        payload = resp.json()
        err = payload.get("error", payload)
        if isinstance(err, dict):
            return str(err.get("message") or err.get("type") or payload)[:300]
        return str(err)[:300]
    except (ValueError, AttributeError):
        return (resp.text or f"HTTP {resp.status_code}")[:300]


def _is_invalid_utf8_failure(resp) -> bool:
    """FastFlowLM occasionally samples a byte sequence its C++ JSON serializer refuses.
    Lemonade then returns HTTP 500 after generation with `invalid UTF-8 byte`. It is a
    generation-specific failure, not corrupt input or dead NPU hardware, so a new sample is
    the only recovery available before the backend itself is fixed."""
    return resp.status_code == 500 and "invalid utf-8 byte" in (_upstream_error_detail(resp) or "").lower()


def _retry_sampling(body: dict, retry_no: int) -> dict:
    """Change the sample enough that an UTF-8 retry cannot deterministically reproduce the
    same invalid token sequence. Preserve every semantic request field."""
    retry = dict(body)
    requested_temp = body.get("temperature")
    try:
        requested_temp = float(requested_temp)
    except (TypeError, ValueError):
        requested_temp = 0.7
    retry["temperature"] = min(0.35 if retry_no == 1 else 0.15, max(0.15, requested_temp))
    try:
        penalty = float(body.get("frequency_penalty") or 0)
    except (TypeError, ValueError):
        penalty = 0
    retry["frequency_penalty"] = min(1.0, penalty + (0.15 * retry_no))
    return retry


def _post_chat_with_utf8_recovery(body: dict, headers: dict, streamed: bool):
    """POST once normally; retry only FastFlowLM's known invalid-UTF-8 generation error.
    Returns `(response, retry_count)`. Other failures are never hidden or retried."""
    body = granite_tool_compat.transport(body)
    attempt_body = body
    for attempt in range(_UTF8_RETRY_LIMIT + 1):
        resp = requests.post(
            f"{LEMONADE_BASE}/v1/chat/completions", json=attempt_body,
            headers=headers, stream=streamed, timeout=(10, 900),
        )
        if not _is_invalid_utf8_failure(resp) or attempt == _UTF8_RETRY_LIMIT:
            return resp, attempt
        resp.close()
        attempt_body = _retry_sampling(body, attempt + 1)
    raise AssertionError("unreachable")


def _free_fastflowlm_npu() -> None:
    """Stop the standalone flm.service, freeing the NPU — mirrors app.py's
    _free_npu_except() and flm_tool_shim.py's _free_lemonade_npu(), for this shim's own
    direction of the same conflict. Reuses fastflowlm_client's own unload (same systemctl
    stop, same polkit rule already in place for it) rather than duplicating that logic.
    Fails silently: flm.service simply not running is the common case, not an error, and
    this must never block a Lemonade request that would otherwise be perfectly fine."""
    try:
        fastflowlm.unload_model(npu_lock_held=True)
    except fastflowlm.FastFlowLMError:
        pass


def _unload_before_implicit_load(model_id: str) -> None:
    """Make Lemonade's traffic-driven model selection explicitly unload-first.

    OpenCode and other LAN clients call the chat endpoint directly, so Lemonade would
    otherwise auto-load a requested model without going through app.py's load route.
    Leave an already-loaded requested model alone (no load will occur), but before a
    missing model can be auto-loaded, unload every loaded reasoning model. Support
    models remain resident.
    """
    try:
        health = requests.get(f"{LEMONADE_BASE}/v1/health", timeout=5)
        health.raise_for_status()
        models = health.json().get("all_models_loaded", [])
        for entry in models:
            if entry.get("model_name") == model_id and entry.get("loaded", True):
                options = lemonade_templates.load_options(model_id, entry.get("recipe_options"))
                if options and options != entry.get("recipe_options"):
                    resp = requests.post(f"{LEMONADE_BASE}/v1/load", json={"model_name": model_id, **options}, timeout=300)
                    resp.raise_for_status()
                return
        for entry in models:
            loaded_id = entry.get("model_name")
            if (
                entry.get("loaded", True)
                and loaded_id
                and entry.get("type") not in {"embedding", "embeddings", "transcription", "tts", "image", "video"}
            ):
                resp = requests.post(
                    f"{LEMONADE_BASE}/v1/unload",
                    json={"model_name": loaded_id}, timeout=60,
                )
                resp.raise_for_status()
        options = lemonade_templates.load_options(model_id)
        if options:
            resp = requests.post(f"{LEMONADE_BASE}/v1/load", json={"model_name": model_id, **options}, timeout=300)
            resp.raise_for_status()
    except requests.RequestException as exc:
        # Unlike the cross-NPU cleanup, this ordering guarantee must not fail open:
        # forwarding after an unload failure could load a second reasoning model.
        raise RuntimeError(f"Could not unload the active Lemonade model before loading '{model_id}': {exc}") from exc


_UPSTREAM_IDLE_KEEPALIVE_SECONDS = 10.0
_STREAM_END = object()


class _KeepaliveLineReader:
    """Iterate Lemonade's SSE body without leaving the client silent for minutes.

    Real incident (2026-10-22): OpenCode aborted GLM-4.7-Flash turns at exactly 300s
    (undici's default headers/body timeout) and immediately retried. Two shim behaviours
    combined to cause it: while a tools request buffers content/tool-calls for repair, no
    bytes are written until the whole response is assembled, and prompt ingestion of a
    large OpenCode context can itself take minutes before Lemonade emits its first SSE
    line. The client never saw a byte, so it retried; the retry queued behind the still-
    generating first request (llama.cpp runs --parallel 1), which made the model look hung
    and produced duplicate requests in Caddy/OpenCode logs.

    A background thread drains the upstream response so the request thread can yield an
    SSE comment (ignored by SSE parsers) every `_UPSTREAM_IDLE_KEEPALIVE_SECONDS`. It also
    makes a disconnected client visible within ~10s instead of only when the buffered
    response is finally written, so the upstream generation is closed promptly and stops
    blocking the next retry. `maxsize=1` keeps at most one line of read-ahead.
    """

    def __init__(self, upstream):
        self._upstream = upstream
        self._lines: queue.Queue = queue.Queue(maxsize=1)
        self._stopped = threading.Event()
        self._thread = threading.Thread(
            target=self._read, daemon=True, name="lemonade-sse-reader"
        )
        self._thread.start()

    def _put(self, item) -> bool:
        while not self._stopped.is_set():
            try:
                self._lines.put(item, timeout=0.5)
                return True
            except queue.Full:
                continue
        return False

    def _read(self) -> None:
        try:
            # A 1024-byte buffer can hold back many tiny SSE token events. Read
            # incrementally so thinking and answer deltas reach clients promptly.
            for line in self._upstream.iter_lines(chunk_size=1):
                if not self._put(line):
                    return
        except BaseException as exc:  # re-raised on the request thread
            self._put(exc)
        finally:
            self._put(_STREAM_END)

    def __iter__(self):
        return self

    def __next__(self):
        while not self._stopped.is_set():
            try:
                item = self._lines.get(timeout=_UPSTREAM_IDLE_KEEPALIVE_SECONDS)
            except queue.Empty:
                return None  # caller sends an SSE keepalive comment
            if item is _STREAM_END:
                break
            if isinstance(item, BaseException):
                raise item
            return item
        raise StopIteration

    def close(self) -> None:
        self._stopped.set()
        try:
            self._upstream.close()
        except Exception:
            pass


@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    started = time.monotonic()
    body = request.get_json(force=True, silent=True) or {}
    body = model_test_profiles.apply(body, "lemonade")
    body = _prepare_agent_request(body)
    if body.get("stream"):
        body["stream_options"] = {**(body.get("stream_options") or {}), "include_usage": True}
    client_body = body
    try:
        body = _normalize_tool_choice(body)
    except ValueError as exc:
        return {"error": {"message": str(exc), "type": "invalid_request_error", "param": "tool_choice"}}, 400
    source = _source_from_headers(request.headers)
    model = body.get("model") or "unknown"
    client_ip = _client_ip(request.headers, request.remote_addr)
    # Computed once and reused below for the live-feed's is_npu tag too, rather than a second
    # /v1/models?show_all=true round-trip - same answer either way, just avoid asking twice.
    is_npu = _is_npu_model(body.get("model"))
    npu_lock_held = False
    cross_npu_lock = None
    if is_npu:
        # Lemonade has one FLM/NPU LLM slot. Concurrent clients requesting different NPU
        # models otherwise evict each other while generation is still streaming, producing
        # intermittent "No model loaded" errors. Hold this for the complete upstream stream,
        # not merely the load/POST operation.
        _NPU_REQUEST_LOCK.acquire()
        npu_lock_held = True
        cross_npu_lock = npu_request_lock.acquire()
        _free_fastflowlm_npu()
    if body.get("model"):
        try:
            _unload_before_implicit_load(model)
        except RuntimeError as exc:
            if npu_lock_held:
                _NPU_REQUEST_LOCK.release()
                npu_request_lock.release(cross_npu_lock)
            _record_chat_metric(started=started, source=source, model=model, is_npu=is_npu,
                                status=503, usage=None, client_ip=client_ip)
            return {"error": {"message": str(exc), "type": "model_unload_failed"}}, 503, {
                "X-Hearth-Metrics-Recorded": "1"}
    streamed = bool(body.get("stream"))
    # Reports this request into the metrics page's live-activity feed (see live_report.py's
    # docstring) - this shim is the only thing inline for real OpenCode/Continue Lemonade
    # traffic, so it's the only place that can see it streaming in real time.
    live_id = None if request.headers.get("X-Hearth-Agent-Relay") == "1" else live_report.start(
        source=source, backend="lemonade",
        model=model, max_tokens=body.get("max_tokens"), is_npu=is_npu,
        request_content=_request_trace(body),
    )
    try:
        upstream, utf8_retries = _post_chat_with_utf8_recovery(
            body, _forward_headers(request.headers), streamed,
        )
    except requests.RequestException as exc:
        # Without this, a dead/unreachable Lemonade would leave this request stuck showing
        # "streaming" in the live feed forever - finish() is only ever called below, which
        # never runs if this raises.
        live_report.finish(live_id, status="error")
        if npu_lock_held:
            _NPU_REQUEST_LOCK.release()
            npu_request_lock.release(cross_npu_lock)
        status = 504 if isinstance(exc, requests.Timeout) else 502
        _record_chat_metric(started=started, source=source, model=model, is_npu=is_npu,
                            status=status, usage=None, client_ip=client_ip)
        return {"error": {"message": "Lemonade upstream request failed.",
                          "type": "upstream_error"}}, status, {"X-Hearth-Metrics-Recorded": "1"}
    if streamed:
        # Only a request that actually declares tools can produce tool calls, and only tool
        # calls need the whole-response repair + degeneracy guard below (they assemble across
        # chunks). Everything else streams straight through, exactly like flm_tool_shim.py's
        # own tee(). Buffering every Lemonade response used to make OpenCode show nothing at
        # all until the model finished - catastrophic for a thinking model like GLM-4.7-Flash
        # that can reason for minutes before its first answer token (2026-10-18 fix). Even in
        # the buffered tool case, reasoning_content is forwarded live (see
        # _split_live_reasoning) so the client can show the think phase progressing.
        buffer_tool_output = any(
            (tool.get("function") or {}).get("name") for tool in body.get("tools") or []
        )

        def tee():
            usage = {}
            outgoing = []
            pending_chars = 0
            pending_content = []
            reported_finish = False
            metric_recorded = False
            failure_status = 502
            reader = _KeepaliveLineReader(upstream)
            try:
                for raw_line in reader:
                    if raw_line is None:
                        # SSE comment: ignored by clients, but keeps OpenCode's undici
                        # body/header timeout from firing while prompt ingestion or
                        # buffered tool-output repair is still in progress.
                        yield b": keepalive\n\n"
                        continue
                    if not raw_line.startswith(b"data: "):
                        out_line = raw_line + b"\n" if raw_line else b"\n"
                        if buffer_tool_output:
                            # Lemonade/llama.cpp emits an SSE comment (": ping") every
                            # second while it ingests a prompt. Buffering those along with
                            # tool output left OpenCode with zero bytes for minutes, so
                            # undici aborted at 300s and retried (2026-10-22). Comments
                            # carry no tool-call state: forward them live and omit them
                            # from the buffered replay so they are not duplicated.
                            if raw_line.startswith(b":"):
                                yield out_line
                            else:
                                outgoing.append(out_line)
                                if not raw_line:
                                    yield out_line
                        else:
                            yield out_line
                        continue
                    payload = raw_line[len(b"data: "):].strip()
                    if payload == b"[DONE]":
                        out_line = raw_line + b"\n"
                        if buffer_tool_output:
                            outgoing.append(out_line)
                        else:
                            yield out_line
                        continue
                    try:
                        obj = json.loads(payload)
                    except (TypeError, json.JSONDecodeError):
                        out_line = raw_line + b"\n"
                        if buffer_tool_output:
                            outgoing.append(out_line)
                        else:
                            yield out_line
                        continue
                    if obj.get("usage"):
                        usage = obj["usage"]
                    try:
                        delta = obj["choices"][0]["delta"]
                    except (KeyError, IndexError, TypeError):
                        out_line = raw_line + b"\n"
                        if buffer_tool_output:
                            outgoing.append(out_line)
                        else:
                            yield out_line
                        continue
                    if buffer_tool_output:
                        live_reasoning, obj = _split_live_reasoning(obj)
                        if live_reasoning is not None:
                            yield b"data: " + json.dumps(
                                live_reasoning, separators=(",", ":")
                            ).encode() + b"\n\n"
                            # Rebuild from the reasoning-stripped object so the buffered
                            # replay below can't send the same reasoning a second time.
                            raw_line = b"data: " + json.dumps(obj, separators=(",", ":")).encode()
                    if _repair_tool_arguments(obj, body):
                        raw_line = b"data: " + json.dumps(obj, separators=(",", ":")).encode()
                    out_line = raw_line + b"\n"
                    if buffer_tool_output:
                        outgoing.append(out_line)
                    else:
                        yield out_line
                    content_piece = delta.get("content") or ""
                    if content_piece:
                        pending_chars += len(content_piece)
                        pending_content.append(content_piece)
                    for tc in delta.get("tool_calls") or []:
                        args = (tc.get("function") or {}).get("arguments")
                        if args:
                            pending_chars += len(args)
                            pending_content.append(f"[tool arguments] {args}")
                    # Throttled: one HTTP round-trip per ~20 characters, not per SSE chunk.
                    if pending_chars >= 20:
                        live_report.update(
                            live_id, delta_chars=pending_chars,
                            delta_content="".join(pending_content),
                        )
                        pending_chars = 0
                        pending_content = []
                if pending_chars:
                    live_report.update(
                        live_id, delta_chars=pending_chars,
                        delta_content="".join(pending_content),
                    )
                live_report.finish(
                    live_id, status="done" if upstream.ok else "error",
                    total_tokens=usage.get("completion_tokens"), status_code=upstream.status_code,
                )
                _record_chat_metric(
                    started=started, source=source, model=model, is_npu=is_npu,
                    status=upstream.status_code, usage=usage, client_ip=client_ip,
                )
                metric_recorded = True
                if buffer_tool_output:
                    repaired = granite_tool_compat.repair_sse(outgoing, body)
                    repaired = tool_argument_json.repair_sse(repaired)
                    repaired = exact_tool_contract.repair_sse(repaired, body)
                    repaired = question_tool_compat.repair_sse(repaired, body)
                    repaired = exact_file_content.repair_sse(repaired, body)
                    # Some Lemonade-served models emit their trained <tool_call> markup
                    # as ordinary content. Convert only complete calls to offered tools.
                    textual_calls = _repair_streamed_textual_tool_calls(repaired, body)
                    if textual_calls is not None:
                        repaired = textual_calls.splitlines(keepends=True)
                    repaired = exact_tool_contract.enforce_named_choice_sse(repaired, client_body)
                    yield from generation_guard.guard_sse_bytes(repaired)
                reported_finish = True
            except GeneratorExit:
                failure_status = 499
                raise
            finally:
                # Stop the reader and close the upstream connection. On a client disconnect
                # this now happens within one keepalive interval instead of only when the
                # buffered response is finally written, so the next retry is not queued
                # behind a generation nobody is listening to.
                reader.close()
                # Guarantees the live-feed entry never gets stuck showing "streaming" forever
                # if the client disconnects mid-stream or Lemonade drops the connection - see
                # flm_tool_shim.py's identical guard for the full reasoning. Idempotent, so
                # this is a no-op in the normal-completion case.
                if not reported_finish:
                    live_report.finish(live_id, status="error", total_tokens=usage.get("completion_tokens"))
                if not metric_recorded:
                    _record_chat_metric(
                        started=started, source=source, model=model, is_npu=is_npu,
                        status=failure_status, usage=usage, client_ip=client_ip,
                    )
                if npu_lock_held:
                    _NPU_REQUEST_LOCK.release()
                    npu_request_lock.release(cross_npu_lock)
        response = Response(
            tee(),
            status=upstream.status_code,
            content_type=upstream.headers.get("content-type", "text/event-stream"),
        )
        response.headers["X-Hearth-NPU-Retries"] = str(utf8_retries)
        response.headers["X-Hearth-Metrics-Recorded"] = "1"
        return response
    usage = {}
    guarded_completion = False
    try:
        upstream_json = upstream.json()
        guarded_completion = granite_tool_compat.repair_completion(upstream_json, body)
        choices = upstream_json.get("choices") or []
        if body.get("tools") and choices:
            message = choices[0].get("message") or {}
            if not message.get("tool_calls"):
                clean, calls = _repair_textual_tool_calls(message.get("content") or "", body)
                if calls:
                    message["content"] = clean or None
                    message["tool_calls"] = calls
                    choices[0]["finish_reason"] = "tool_calls"
                    guarded_completion = True
        guarded_completion = tool_argument_json.repair_completion(upstream_json) or guarded_completion
        guarded_completion = exact_tool_contract.repair_completion(upstream_json, body) or guarded_completion
        guarded_completion = exact_tool_contract.enforce_named_choice_completion(
            upstream_json, client_body) or guarded_completion
        guarded_completion = question_tool_compat.repair_completion(upstream_json, body) or guarded_completion
        guarded_completion = exact_file_content.repair_completion(upstream_json, body) or guarded_completion
        guarded_completion = generation_guard.guard_completion_payload(upstream_json) or guarded_completion
        usage = upstream_json.get("usage", {})
        response_text = (
            ((upstream_json.get("choices") or [{}])[0].get("message") or {}).get("content")
            or ""
        )
        if response_text:
            live_report.update(
                live_id, delta_chars=len(response_text), delta_content=response_text,
            )
    except (ValueError, AttributeError):
        pass
    live_report.finish(
        live_id, status="done" if upstream.ok else "error", total_tokens=usage.get("completion_tokens"),
        status_code=upstream.status_code, error_detail=_upstream_error_detail(upstream),
    )
    _record_chat_metric(
        started=started, source=source, model=model, is_npu=is_npu,
        status=upstream.status_code, usage=usage, client_ip=client_ip,
    )
    response = Response(
        (json.dumps(upstream_json).encode("utf-8") if guarded_completion else upstream.content),
        status=upstream.status_code,
        content_type=upstream.headers.get("content-type", "application/json"),
    )
    response.headers["X-Hearth-NPU-Retries"] = str(utf8_retries)
    response.headers["X-Hearth-Metrics-Recorded"] = "1"
    if npu_lock_held:
        _NPU_REQUEST_LOCK.release()
        npu_request_lock.release(cross_npu_lock)
    return response


@app.route("/<path:path>", methods=["GET", "POST"])
def passthrough(path):
    """Everything else (/v1/models, /v1/health, /v1/embeddings, etc.) — unmodified reverse
    proxy. None of these can trigger an NPU model load on their own."""
    upstream = requests.request(
        request.method, f"{LEMONADE_BASE}/{path}", params=request.args,
        data=request.get_data(), headers=_forward_headers(request.headers), timeout=30,
    )
    return Response(
        upstream.content, status=upstream.status_code,
        content_type=upstream.headers.get("content-type", "application/octet-stream"),
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=LISTEN_PORT, threaded=True)
