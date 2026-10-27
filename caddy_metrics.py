"""Background tailer of Caddy's JSON access log — turns every LAN request (OpenCode,
VSCode/Continue, or any other client hitting the Caddy proxy ports) into a metrics row.

The webui's own chat never goes through Caddy (Flask talks to the backends directly), so
this only sees external LAN traffic — no double counting with app.py's webui-chat rows.
Token/TOPS aren't visible at the proxy layer, so those stay NULL for these rows; response
time, request type, source, backend, status and size all come straight from the log entry.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

import requests

import metrics_store

LOG_PATH = "/var/log/caddy/access.jsonl"

_PORT_BACKEND = {
    "8443": "lmstudio", "8444": "lemonade", "8445": "vllm",
    "8446": "kobold", "8447": "fastflowlm",
}
_started = False
_model_hint_cache: dict[str, tuple[float, str | None]] = {}
_MODEL_HINT_TTL = 10.0


def _single_model_hint(backend: str) -> str | None:
    """Model id for single-model servers whose Caddy log cannot include JSON request bodies."""
    endpoint = {
        "vllm": "http://127.0.0.1:8000/v1/models",
        "kobold": "http://127.0.0.1:5001/v1/models",
    }.get(backend)
    if endpoint is None:
        return None
    now = time.monotonic()
    cached = _model_hint_cache.get(backend)
    if cached and now - cached[0] < _MODEL_HINT_TTL:
        return cached[1]
    model = None
    try:
        data = requests.get(endpoint, timeout=2).json().get("data") or []
        if len(data) == 1:
            model = data[0].get("id")
    except (requests.RequestException, ValueError, AttributeError):
        pass
    _model_hint_cache[backend] = (now, model)
    return model


def _source_from_ua(ua: str) -> str:
    u = (ua or "").lower()
    if "opencode" in u:
        return "opencode"
    if "continue" in u or "vscode" in u or "code-" in u or "vs code" in u:
        return "vscode"
    return "lan"


def _source_from_headers(headers: dict, ua: str) -> str:
    """Prefer the explicit X-Hearth-Client label (set in the generated OpenCode/Continue
    configs, see help_data.py) over guessing from User-Agent. Added 2026-07-26: Caddy redacts
    the Authorization header from its own access log by default (confirmed live - a real bearer
    token sent by the client shows up as an empty list here), so even though each client now
    gets its own dedicated token (see the Caddyfile's `client_auth` snippet), that credential
    can't itself be read back out of the log to attribute a row - a non-secret custom header the
    client sends directly isn't on Caddy's redaction list and comes through untouched, so that's
    what carries the label instead. Falls back to the original UA-sniffing for anything that
    doesn't send it (older configs, ad-hoc curl/other LAN tools)."""
    agent_list = headers.get("X-Hearth-Agent-Name") or headers.get("x-hearth-agent-name")
    if agent_list:
        agent_name = agent_list[0] if isinstance(agent_list, list) else str(agent_list)
        agent_name = agent_name.strip()
        if agent_name and len(agent_name) <= 120 and not any(ord(char) < 32 for char in agent_name):
            return agent_name
    label_list = headers.get("X-Hearth-Client") or headers.get("x-hearth-client")
    if label_list:
        label = label_list[0] if isinstance(label_list, list) else str(label_list)
        # Client-defined source labels drive both the live feed and historical metrics.
        # Keep them constrained to a short slug so an arbitrary header cannot inject
        # unwieldy values into the dashboard/filter UI.
        label = label.strip().lower()
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", label):
            return label
    return _source_from_ua(ua)


def _classify(uri: str, source: str) -> tuple[str, str]:
    """(request_type, ask_category) from the request path."""
    path = (uri or "").split("?", 1)[0]
    if path.endswith("/chat/completions"):
        return "chat_completion", ("code" if source in ("opencode", "vscode", "vscode_continue") else "chat")
    if path.endswith("/completions"):
        return "completion", ("code" if source in ("opencode", "vscode", "vscode_continue") else "chat")
    if path.endswith("/embeddings"):
        return "embeddings", "embed"
    if path.endswith("/models") or path.endswith("/tags"):
        return "models", "poll"
    return path or "other", "other"


def _handle(entry: dict) -> None:
    response_headers = entry.get("resp_headers") or {}
    recorded = response_headers.get("X-Hearth-Metrics-Recorded") or response_headers.get("x-hearth-metrics-recorded")
    if recorded:
        # Lemonade/FastFlowLM shims saw the JSON body and already submitted a richer row
        # (model + tokens + NPU truth). Do not add a second, body-blind Caddy row.
        return
    req = entry.get("request") or {}
    host = req.get("host") or ""
    port = host.rsplit(":", 1)[-1] if ":" in host else ""
    backend = _PORT_BACKEND.get(port)
    if backend is None:
        return  # not one of our proxy ports
    headers = req.get("headers") or {}
    ua_list = headers.get("User-Agent") or headers.get("user-agent") or [""]
    ua = ua_list[0] if isinstance(ua_list, list) else str(ua_list)
    source = _source_from_headers(headers, ua)
    request_type, ask_category = _classify(req.get("uri", ""), source)
    dur = entry.get("duration")
    metrics_store.record(
        ts=entry.get("ts") if isinstance(entry.get("ts"), (int, float)) else None,
        source=source,
        backend=backend,
        # vLLM and Kobold each serve exactly one model per process, so their current /models
        # value is an unambiguous substitute for the body Caddy cannot log.
        model=_single_model_hint(backend),
        request_type=request_type,
        ask_category=ask_category,
        duration_ms=round(dur * 1000, 1) if isinstance(dur, (int, float)) else None,
        status=entry.get("status"),
        client_ip=req.get("remote_ip") or req.get("client_ip"),
        user_id=None,
        # fastflowlm is NPU-only regardless of which model was requested, so this can be
        # answered with confidence despite never seeing the request body. lemonade multiplexes
        # several recipes and only some of its models are actually NPU (flm-recipe) ones - with
        # no model id visible at this layer, that one stays NULL/unknown rather than a guessed
        # True or False (see metrics_store.py's _MIGRATION_COLUMNS comment).
        is_npu=True if backend == "fastflowlm" else None,
    )


def _tail_loop() -> None:
    pos = 0
    inode = None
    buf = ""
    while True:
        try:
            st = os.stat(LOG_PATH)
            if inode != st.st_ino or st.st_size < pos:
                first = inode is None
                # First run: start at the current END (don't re-ingest history on restart).
                # A roll/truncate (inode changed after being set): read the new file from 0.
                pos = st.st_size if first else 0
                inode = st.st_ino
                buf = ""
            if st.st_size > pos:
                with open(LOG_PATH, "r", errors="replace") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
                buf += chunk
                lines = buf.split("\n")
                buf = lines.pop()  # keep any partial last line
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        _handle(json.loads(line))
                    except (ValueError, KeyError, TypeError):
                        continue
        except FileNotFoundError:
            pos, inode, buf = 0, None, ""
        except OSError:
            pass
        time.sleep(2)


def start_tailer() -> None:
    """Start the background tailer once (idempotent)."""
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_tail_loop, daemon=True, name="caddy-metrics-tailer").start()
