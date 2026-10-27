"""Tiny reverse-proxy shim between Caddy's :8443 LAN endpoint and LM Studio's own server
(127.0.0.1:1234) — reports live streaming activity into the metrics page's live-activity feed,
the same way lemonade_guard_shim.py/flm_tool_shim.py do for Lemonade/FastFlowLM traffic.

Real gap this closes (2026-08-23): api_metrics_live()'s own docstring in app.py documents that
LM Studio/vLLM/KoboldCPP LAN traffic "still can't appear here — no shim sits inline for those,
so nothing on this app's side ever sees their streaming bodies." Requests DID already land
correctly in the historical Recent Requests table via caddy_metrics.py tailing Caddy's access
log — that only records after the fact, from the log line written once the response completes.
This shim adds the missing real-time half for LM Studio specifically, by actually sitting
inline for every request instead of tailing a log after the fact.

Tool requests also normalize textual tool calls and repair supported argument encoding
errors. Their streams are buffered so leaked tool markup can be replaced before clients
receive it. Requests without tools continue streaming immediately. No NPU eviction occurs.

Runs as its own systemd --user unit (lmstudio-shim.service) on 127.0.0.1:13307. Caddy's :8443
block points its `reverse_proxy` here instead of LM Studio's raw 1234 so LAN clients get live
reporting; this app's own lmstudio_client.py talks to LM Studio directly on 1234 and is
unaffected either way — same relationship fastflowlm_client.py has with flm_tool_shim.py.
"""
from __future__ import annotations

import json
import re
import time

import requests
import model_test_profiles
import agent_request_policy
import question_tool_compat
import exact_file_content
import tool_argument_json
import exact_tool_contract
from flm_tool_shim import _repair_textual_tool_calls, _repair_streamed_textual_tool_calls
from flask import Flask, Response, request

import live_report

LMSTUDIO_BASE = "http://127.0.0.1:1234"
LISTEN_PORT = 13307

app = Flask(__name__)


def normalize_tool_choice(body):
    """Translate OpenAI named-function forcing to LM Studio's required-only form."""
    choice = body.get("tool_choice")
    if not isinstance(choice, dict):
        return body
    function = choice.get("function")
    if not isinstance(function, dict):
        raise ValueError("tool_choice must name an offered function")
    name = function.get("name")
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


def _upstream_error_detail(resp) -> str | None:
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


def _record_chat_metric(*, started: float, source: str, model: str,
                        status: int, usage: dict | None, client_ip: str | None) -> None:
    usage = usage or {}
    duration_ms = round((time.monotonic() - started) * 1000, 1)
    completion = usage.get("completion_tokens")
    tps = None
    if isinstance(completion, (int, float)) and duration_ms > 0:
        tps = round(completion / (duration_ms / 1000), 3)
    live_report.record_metric(
        source=source, backend="lmstudio", model=model,
        request_type="chat_completion", ask_category="chat",
        duration_ms=duration_ms, prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=completion, total_tokens=usage.get("total_tokens"),
        tokens_per_sec=tps, status=status, client_ip=client_ip, is_npu=False,
    )


@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    started = time.monotonic()
    body = request.get_json(force=True, silent=True) or {}
    body = model_test_profiles.apply(body, "lmstudio")
    body = agent_request_policy.prepare(body, compact_history=False, qwen_penalty=False)
    try:
        body = normalize_tool_choice(body)
    except ValueError as exc:
        return {"error": {"message": str(exc), "type": "invalid_request_error", "param": "tool_choice"}}, 400
    source = _source_from_headers(request.headers)
    model = body.get("model") or "unknown"
    client_ip = _client_ip(request.headers, request.remote_addr)
    streamed = bool(body.get("stream"))
    live_id = None if request.headers.get("X-Hearth-Agent-Relay") == "1" else live_report.start(
        source=source, backend="lmstudio",
        model=model, max_tokens=body.get("max_tokens"), is_npu=False,
        request_content=_request_trace(body),
    )
    try:
        upstream = requests.post(
            f"{LMSTUDIO_BASE}/v1/chat/completions", json=body,
            headers=_forward_headers(request.headers), stream=streamed, timeout=(10, 900),
        )
    except requests.RequestException:
        live_report.finish(live_id, status="error")
        raise

    if streamed:
        def tee():
            usage = {}
            pending_chars = 0
            pending_content = []
            reported_finish = False
            pending_tools = [] if body.get('tools') and body.get('tool_choice') != 'none' else None
            try:
                for raw_line in upstream.iter_lines(chunk_size=1):
                    if raw_line is None:
                        continue
                    wire_line = raw_line + b"\n" if raw_line else b"\n"
                    # Hold tool fragments until complete arguments can be repaired.
                    # Ordinary text continues streaming immediately.
                    try:
                        event = json.loads(raw_line[6:]) if raw_line.startswith(b"data: ") else {}
                        has_tools = any((c.get('delta') or {}).get('tool_calls') for c in event.get('choices', []))
                    except (ValueError, TypeError):
                        has_tools = False
                    if has_tools and pending_tools is None:
                        pending_tools = []
                    if pending_tools is None:
                        yield wire_line
                    else:
                        pending_tools.append(wire_line)
                    if not raw_line.startswith(b"data: "):
                        continue
                    payload = raw_line[len(b"data: "):].strip()
                    if payload == b"[DONE]":
                        continue
                    try:
                        obj = json.loads(payload)
                        delta = obj["choices"][0]["delta"]
                    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
                        continue
                    if obj.get("usage"):
                        usage = obj["usage"]
                    content_piece = delta.get("content") or ""
                    if content_piece:
                        pending_chars += len(content_piece)
                        pending_content.append(content_piece)
                    for tc in delta.get("tool_calls") or []:
                        args = (tc.get("function") or {}).get("arguments")
                        if args:
                            pending_chars += len(args)
                            pending_content.append(f"[tool arguments] {args}")
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
                if pending_tools is not None:
                    textual = _repair_streamed_textual_tool_calls(pending_tools, body)
                    repaired = textual.splitlines(keepends=True) if textual else pending_tools
                    repaired = tool_argument_json.repair_sse(repaired)
                    repaired = exact_tool_contract.repair_sse(repaired, body)
                    repaired = question_tool_compat.repair_sse(repaired, body)
                    yield from exact_file_content.repair_sse(repaired, body)
                live_report.finish(
                    live_id, status="done" if upstream.ok else "error",
                    total_tokens=usage.get("completion_tokens"), status_code=upstream.status_code,
                )
                _record_chat_metric(
                    started=started, source=source, model=model,
                    status=upstream.status_code, usage=usage, client_ip=client_ip,
                )
                reported_finish = True
            finally:
                if not reported_finish:
                    try:
                        upstream.close()
                    except Exception:
                        pass
                    live_report.finish(live_id, status="error", total_tokens=usage.get("completion_tokens"))
        response = Response(
            tee(),
            status=upstream.status_code,
            content_type=upstream.headers.get("content-type", "text/event-stream"),
        )
        response.headers["X-Hearth-Metrics-Recorded"] = "1"
        return response

    usage = {}
    repaired_completion = False
    try:
        upstream_json = upstream.json()
        for choice in upstream_json.get('choices') or []:
            message = choice.get('message') or {}
            if not message.get('tool_calls'):
                clean, calls = _repair_textual_tool_calls(message.get('content') or '', body)
                if calls:
                    message.update(content=clean or None, tool_calls=calls)
                    choice['finish_reason'] = 'tool_calls'
                    repaired_completion = True
        repaired_completion = tool_argument_json.repair_completion(upstream_json) or repaired_completion
        repaired_completion = exact_tool_contract.repair_completion(upstream_json, body) or repaired_completion
        repaired_completion = question_tool_compat.repair_completion(upstream_json, body) or repaired_completion
        repaired_completion = exact_file_content.repair_completion(upstream_json, body) or repaired_completion
        usage = upstream_json.get("usage", {})
        response_text = (
            ((upstream_json.get("choices") or [{}])[0].get("message") or {}).get("content")
            or ""
        )
        if response_text:
            live_report.update(live_id, delta_chars=len(response_text), delta_content=response_text)
    except (ValueError, AttributeError):
        pass
    live_report.finish(
        live_id, status="done" if upstream.ok else "error", total_tokens=usage.get("completion_tokens"),
        status_code=upstream.status_code, error_detail=_upstream_error_detail(upstream),
    )
    _record_chat_metric(
        started=started, source=source, model=model,
        status=upstream.status_code, usage=usage, client_ip=client_ip,
    )
    response = Response(
        json.dumps(upstream_json).encode('utf-8') if repaired_completion else upstream.content, status=upstream.status_code,
        content_type=upstream.headers.get("content-type", "application/json"),
    )
    response.headers["X-Hearth-Metrics-Recorded"] = "1"
    return response


@app.route("/<path:path>", methods=["GET", "POST"])
def passthrough(path):
    """Everything else (/v1/models, /api/v0/*, /v1/embeddings, etc.) — unmodified proxy."""
    upstream = requests.request(
        request.method, f"{LMSTUDIO_BASE}/{path}", params=request.args,
        data=request.get_data(), headers=_forward_headers(request.headers), timeout=30,
    )
    return Response(
        upstream.content, status=upstream.status_code,
        content_type=upstream.headers.get("content-type", "application/octet-stream"),
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=LISTEN_PORT, threaded=True)
