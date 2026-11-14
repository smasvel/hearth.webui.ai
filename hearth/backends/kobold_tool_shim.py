"""Loopback-only compatibility bridge for KoboldCPP tool-call behavior.

KoboldCPP's OpenAI endpoint can return a single tool call for an explicit
multi-call request, and some templates still emit a call after the user asks
for a text-only follow-up. This bridge applies narrowly scoped repairs before
Hearth chat or its live model tests consume those responses.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid

import requests
from flask import Flask, Response, jsonify, request
from werkzeug.serving import make_server

from hearth.storage import settings_store
from hearth.core import live_report

app = Flask(__name__)
_server = None
_server_lock = threading.Lock()
_server_thread = None
_port = None

LISTEN_PORT = 13308
_HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "content-encoding", "content-length"}


def _upstream_url() -> str:
    return (settings_store.get("kobold_base_url") or "http://127.0.0.1:5001").rstrip("/")


def _headers():
    key = settings_store.get("kobold_api_key")
    return {"Authorization": f"Bearer {key}"} if key else {}


def _source_from_headers(headers) -> str:
    """Same X-Hearth-Client convention used by other shims."""
    agent_name = (headers.get("X-Hearth-Agent-Name") or headers.get("x-hearth-agent-name") or "").strip()
    if agent_name and len(agent_name) <= 120 and not any(ord(c) < 32 for c in agent_name):
        return agent_name
    client = (headers.get("X-Hearth-Client") or "").lower()
    return client if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", client) else "lan"


def _client_ip(headers, remote_addr):
    forwarded = headers.get("X-Forwarded-For") or ""
    ip = forwarded.split(",", 1)[0].strip() if forwarded else ""
    return ip or remote_addr


def _request_trace(body: dict) -> str:
    rows = []
    for message in body.get("messages") or []:
        role = str(message.get("role") or "unknown")
        content = message.get("content")
        if content:
            rows.append(f"{role}: {content}")
        if message.get("tool_calls"):
            rows.append(f"{role}: [tool calls: {len(message['tool_calls'])}]")
    return "\n".join(rows)[-4000:]


def _latest_user_text(body: dict) -> str:
    for message in reversed(body.get("messages") or []):
        if message.get("role") == "user":
            content = message.get("content") or ""
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return " ".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    return ""


def suppress_tools_for_explicit_text_turn(body: dict) -> bool:
    """Drop available tools only for an unforced, explicit text-only request."""
    if not body.get("tools") or body.get("tool_choice") not in (None, "auto"):
        return False
    text = _latest_user_text(body)
    return bool(re.search(
        r"\b(?:do not|don't|never)\s+(?:call|use|invoke)\s+(?:any\s+)?(?:more\s+)?tools?\b"
        r"|\bwithout\s+(?:calling|using|invoking)\s+(?:any\s+)?tools?\b",
        text, re.I,
    ))


def _parallel_contract(body: dict):
    """Extract two literal values for one required string field, when explicitly named."""
    if body.get("parallel_tool_calls") is not True or body.get("tool_choice") not in (None, "auto", "required"):
        return None
    tools = body.get("tools") or []
    if len(tools) != 1:
        return None
    function = tools[0].get("function") or {}
    name = function.get("name")
    schema = function.get("parameters") or {}
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    if (not isinstance(name, str) or len(required) != 1 or len(properties) != 1
            or required[0] not in properties
            or properties[required[0]].get("type") != "string"):
        return None
    user = _latest_user_text(body)
    match = re.search(
        rf"\bTWO\s+{re.escape(name)}\s+tool calls?\s*:\s*([^,.\n]+?)\s+first,\s*then\s+([^\n.]+)",
        user, re.I,
    )
    if not match:
        return None
    values = [value.strip().strip(" `\"'") for value in match.groups()]
    if not all(values) or values[0].casefold() == values[1].casefold():
        return None
    return name, required[0], values


def _normalize_calls(calls, contract):
    """Add only explicitly requested calls that Kobold omitted; preserve valid IDs."""
    if not contract or not isinstance(calls, list) or len(calls) != 1:
        return calls, False
    name, field, values = contract
    call = calls[0]
    function = call.get("function") or {}
    try:
        arguments = function.get("arguments") or "{}"
        arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
    except (ValueError, TypeError):
        return calls, False
    if function.get("name") != name or not isinstance(arguments, dict) or set(arguments) != {field}:
        return calls, False
    matched = next((index for index, value in enumerate(values)
                    if isinstance(arguments[field], str) and arguments[field].casefold() == value.casefold()), None)
    if matched is None:
        return calls, False
    missing = 1 - matched
    second = {
        "id": "call_hearth_" + uuid.uuid4().hex,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps({field: values[missing]}, separators=(",", ":"))},
    }
    result = list(calls)
    result.insert(missing, second)
    return result, True


def _repair_completion(payload: dict, body: dict) -> bool:
    choices = payload.get("choices") or []
    if not choices:
        return False
    choice = choices[0]
    message = choice.setdefault("message", {})
    changed = False
    if suppress_tools_for_explicit_text_turn(body):
        if message.get("tool_calls"):
            message.pop("tool_calls", None)
            message["content"] = message.get("content") or ""
            choice["finish_reason"] = "stop"
            changed = True
        return changed
    calls, repaired = _normalize_calls(message.get("tool_calls") or [], _parallel_contract(body))
    if repaired:
        message["tool_calls"] = calls
        message["content"] = None
        choice["finish_reason"] = "tool_calls"
    return repaired


def _collect_stream(response):
    lines = []
    for raw in response.iter_lines(chunk_size=1):
        if raw is not None:
            lines.append(raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw))
    return lines


def _stream_payload(lines):
    content = []
    calls = {}
    usage = {}
    finish = "stop"
    for line in lines:
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        try:
            event = json.loads(line[6:])
        except (TypeError, ValueError):
            continue
        usage.update(event.get("usage") or {})
        for choice in event.get("choices") or []:
            finish = choice.get("finish_reason") or finish
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content.append(delta["content"])
            for call in delta.get("tool_calls") or []:
                index = call.get("index", 0)
                target = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                target["id"] += call.get("id") or ""
                fn = call.get("function") or {}
                target["function"]["name"] += fn.get("name") or ""
                target["function"]["arguments"] += fn.get("arguments") or ""
    return {"choices": [{"message": {"content": "".join(content),
                                        "tool_calls": [calls[i] for i in sorted(calls)]},
                         "finish_reason": finish}], "usage": usage}


def _encode_stream(payload):
    choice = (payload.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    events = []
    if message.get("content"):
        events.append({"choices": [{"index": 0, "delta": {"content": message["content"]}, "finish_reason": None}]})
    for index, call in enumerate(message.get("tool_calls") or []):
        events.append({"choices": [{"index": 0, "delta": {"tool_calls": [{**call, "index": index}]}, "finish_reason": None}]})
    terminal = {"choices": [{"index": 0, "delta": {}, "finish_reason": choice.get("finish_reason") or "stop"}]}
    if payload.get("usage"):
        terminal["usage"] = payload["usage"]
    events.append(terminal)
    return "".join("data: " + json.dumps(event, separators=(",", ":")) + "\n\n" for event in events) + "data: [DONE]\n\n"


@app.post("/v1/chat/completions")
def chat_completions():
    body = request.get_json(force=True, silent=True) or {}
    source = _source_from_headers(request.headers)
    model = body.get("model") or "unknown"
    client_ip = _client_ip(request.headers, request.remote_addr)
    started = time.monotonic()
    live_id = None if request.headers.get("X-Hearth-Agent-Relay") == "1" else live_report.start(
        source=source, backend="kobold", model=model, max_tokens=body.get("max_tokens"),
        is_npu=False, request_content=_request_trace(body)
    )
    suppress = suppress_tools_for_explicit_text_turn(body)
    contract = _parallel_contract(body)
    upstream_body = dict(body)
    if body.get("stream"):
        upstream_body["stream_options"] = {
            **(upstream_body.get("stream_options") or {}), "include_usage": True,
        }
    if suppress:
        upstream_body.pop("tools", None)
        upstream_body.pop("tool_choice", None)
        upstream_body.pop("parallel_tool_calls", None)
    transform = suppress or bool(contract)
    try:
        upstream = requests.post(
            _upstream_url() + "/v1/chat/completions", json=upstream_body,
            headers={key: value for key, value in _headers().items()},
            stream=bool(body.get("stream")), timeout=(10, 900),
        )
    except requests.RequestException as exc:
        live_report.finish(live_id, status="error")
        return jsonify({"error": {"message": str(exc), "type": "upstream_error"}}), 502
    if upstream.status_code >= 400:
        live_report.finish(live_id, status="error", status_code=upstream.status_code)
        return Response(upstream.content, status=upstream.status_code,
                        content_type=upstream.headers.get("content-type", "application/json"))
    if body.get("stream"):
        if not transform:
            def passthrough():
                try:
                    for line in upstream.iter_lines(chunk_size=1):
                        if line is None:
                            continue
                        if isinstance(line, str):
                            line = line.encode("utf-8")
                        yield line + b"\n\n"
                finally:
                    live_report.finish(live_id, status="done" if upstream.ok else "error", status_code=upstream.status_code)
            return Response(passthrough(), content_type="text/event-stream")
        lines = _collect_stream(upstream)
        payload = _stream_payload(lines)
        changed = _repair_completion(payload, body)
        # Update live feed with total content length
        content_text = "".join((c.get("content") or "") for c in payload.get("choices", []))
        if content_text:
            live_report.update(live_id, delta_chars=len(content_text), delta_content=content_text)
        usage = payload.get("usage") or {}
        live_report.finish(live_id, status="done" if upstream.ok else "error", total_tokens=usage.get("completion_tokens"), status_code=upstream.status_code)
        if not changed:
            return Response("\n".join(lines) + "\n", content_type="text/event-stream")
        return Response(_encode_stream(payload), content_type="text/event-stream")
    try:
        payload = upstream.json()
    except ValueError:
        live_report.finish(live_id, status="done" if upstream.ok else "error", status_code=upstream.status_code)
        return Response(upstream.content, status=upstream.status_code,
                        content_type=upstream.headers.get("content-type", "application/json"))
    changed = _repair_completion(payload, body)
    # Update live feed with response text
    response_text = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    if response_text:
        live_report.update(live_id, delta_chars=len(response_text), delta_content=response_text)
    usage = payload.get("usage") or {}
    live_report.finish(live_id, status="done" if upstream.ok else "error", total_tokens=usage.get("completion_tokens"), status_code=upstream.status_code)
    return jsonify(payload) if changed else Response(upstream.content, content_type="application/json")


def start_server() -> str:
    """Start an ephemeral loopback WSGI listener and return its OpenAI base URL."""
    global _server, _server_thread, _port
    with _server_lock:
        if _server is None:
            _server = make_server("127.0.0.1", 0, app, threaded=True)
            _port = _server.server_port
            _server_thread = threading.Thread(target=_server.serve_forever,
                                              name="kobold-tool-compat", daemon=True)
            _server_thread.start()
    return f"http://127.0.0.1:{_port}"


def should_use_shim(base_url: str) -> bool:
    """Only proxy the local KoboldCPP endpoint; remote configured hosts stay direct."""
    from urllib.parse import urlsplit
    parsed = urlsplit(base_url)
    return parsed.hostname in ("127.0.0.1", "localhost", "::1") and parsed.port == 5001


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=LISTEN_PORT, threaded=True)

