"""OpenAI-compatible tool-call repair proxy for small LM Studio models."""
from __future__ import annotations

import json
import re
import time

import requests
import model_test_profiles
from flask import Flask, Response, request, stream_with_context

from flm_tool_shim import (
    _inject_tools_block,
    _repair_streamed_textual_tool_calls,
    _repair_textual_tool_calls,
)

# Upstream is lmstudio_shim.py (13307), not LM Studio (1234) directly: that shim is the one
# that reports live-activity/metrics rows (see its own docstring) - chaining through it here
# means every OpenCode request gets both tool-call repair (this shim) and metrics visibility
# (the other one) without duplicating the metrics/live_report code in two places. lmstudio_
# shim.py already forwards untouched to 1234 itself, so this adds one more hop, not a
# behavior change for LM Studio.
UPSTREAM = "http://127.0.0.1:13307"
PORT = 1235
app = Flask(__name__)

# Real failure observed live (2026-09-05): asked to write-then-read a file in the working
# directory, this 1.2B model twice invented an unrelated absolute path instead of the one
# implied by the conversation - once the sandbox root '/', once '/home/YOUR_USERNAME/python-repl-
# projects/', which only appears as an example path inside the python-repl MCP tool's own
# description text, not anywhere in the actual request. The model is bleeding a path mentioned
# in a tool's *description* into its *arguments* for a different tool. This reminder is
# deliberately generic (no hardcoded path) so it doesn't just patch over that one example -
# any tool description containing an example path is a similar risk for a model this size.
_PATH_HALLUCINATION_WARNING = (
    "\n\nWhen a tool argument is a file or directory path, use only a path that was "
    "explicitly given to you in the user's message or an earlier tool result in this "
    "conversation. Do not reuse, adapt, or get influenced by any path mentioned only inside "
    "a tool's own name, description, or parameter documentation above - those are unrelated "
    "examples, not real targets. If no path was given and one is required, ask for it or use "
    "a relative filename with no directory instead of guessing an absolute path."
)


def _has_path_argument(tools: list[dict]) -> bool:
    for tool in tools or []:
        properties = ((tool.get("function") or {}).get("parameters") or {}).get("properties") or {}
        if any("path" in str(key).lower() or "director" in str(key).lower() for key in properties):
            return True
    return False


_WORKDIR_RE = re.compile(r"Working directory:\s*(\S+)", re.I)


def _extract_working_directory(messages: list[dict]) -> str | None:
    """OpenCode's own system prompt already states 'Working directory: /abs/path' - confirmed
    live by capturing a real request body. The correct answer is already in context; the model
    just isn't reliably attending to it, buried early among long tool descriptions. Re-stating
    it doesn't add new information, it makes existing information easier to use."""
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            match = _WORKDIR_RE.search(content)
            if match:
                return match.group(1)
    return None


def _inject_path_warning(body: dict) -> dict:
    tools = body.get("tools")
    if not tools or not _has_path_argument(tools):
        return body
    messages = list(body.get("messages") or [])
    if messages and messages[0].get("role") == "system":
        messages[0] = {**messages[0], "content": (messages[0].get("content") or "") + _PATH_HALLUCINATION_WARNING}
    else:
        messages.insert(0, {"role": "system", "content": _PATH_HALLUCINATION_WARNING.strip()})

    # Placed at the END of the message list (highest recency), not merged into the system
    # message at position 0 like the warning above - a small model attends far more reliably
    # to the last few hundred tokens of context than to instructions buried under a long list
    # of tool descriptions. Confirmed live: the workdir was already present at position 0 and
    # still got ignored/confused with an unrelated example path from a tool's own description.
    workdir = _extract_working_directory(messages)
    if workdir:
        messages.append({
            "role": "system",
            "content": (
                f"Reminder: the working directory for this conversation is exactly "
                f"'{workdir}'. Build any absolute path a tool needs by joining this exact "
                f"directory with the filename (e.g. '{workdir}/name.ext'). Never substitute "
                "any other directory, including one that only appears as an example inside a "
                "tool's own description."
            ),
        })
    return {**body, "messages": messages}


def _headers():
    blocked = {"host", "content-length", "connection", "transfer-encoding", "content-encoding"}
    return {k: v for k, v in request.headers.items() if k.lower() not in blocked}


# Real failure observed live (2026-09-05), reproduced 2/6 runs even after the path-hallucination
# fix above: instead of emitting any tool call at all, this model sometimes writes a fenced code
# block that *simulates* calling the tool (`write("hello.txt", ...)`, or a fabricated `Output:`
# section) and then narrates success in prose - "Wrote file successfully", "confirming the
# operation succeeded" - even though no file was ever touched. This is a distinct failure from
# the path bug: there IS no tool call to repair here, textually or otherwise, so
# _repair_textual_tool_calls (which only looks for <tool_call> markup) can't catch it. Detected
# by: tools were offered, no real or textual tool call resulted, and the content contains the
# hallmarks of simulated execution (a code fence invoking something that looks like a tool, or a
# fabricated Output/Result block, or a success claim for an action-shaped tool). Retried a bounded
# number of times with an increasingly blunt corrective instruction before giving up and replacing
# the fabricated claim with an honest refusal - passing along a confident false "success" is worse
# than a visible failure, since the former hides a real bug from the user until they check the file.
# Kept at 1 (2 total attempts), not higher: each attempt re-sends the full conversation history
# to a model with only a 32768-token context window, and OpenCode's own tool descriptions alone
# are already substantial. A real regression was caught live raising this to 2 while also
# compounding the nudge onto itself each retry: attempt 2 failed outright with "Context size has
# been exceeded." _with_corrective_nudge now rebuilds from the ORIGINAL body every attempt
# instead of stacking onto the previous attempt's already-nudged body, precisely to stop that
# growth - but the retry count itself also stays conservative for the same reason.
_MAX_HALLUCINATION_RETRIES = 1

_FAKE_CODE_EXEC_RE = re.compile(r"```(?:python|py|bash|sh)?\s*\n.*?```", re.S)
_FAKE_OUTPUT_BLOCK_RE = re.compile(r"\b(?:output|result)\s*:?\s*\n?```", re.I)
_SUCCESS_CLAIM_RE = re.compile(
    r"\b(wrote|written|created|saved|confirm(?:ed|ing)?|success(?:fully)?|verified|matches?)\b",
    re.I,
)
_CORRECTIVE_NUDGE = (
    "\n\nYour previous reply did not contain a real tool call - it only described or simulated "
    "one in text or a code block, and no tool actually ran. Do not write code that pretends to "
    "call a tool, do not fabricate an 'Output' or 'Result', and do not claim a file was written, "
    "read, or verified unless you just received a real tool result confirming it in this "
    "conversation. If the task requires a tool, invoke it for real using the tool-calling "
    "mechanism now."
)

_EXPLICIT_TOOL_REQUEST_RE = re.compile(
    r"\b(?:must|required to|exactly|do not substitute|invoke|call|use)\b.*\btool\b|"
    r"\buse\s+(?:the\s+)?(?:glob|grep|read|write|edit|bash|playwright)\b",
    re.I | re.S,
)


def _explicitly_requires_tool(body: dict) -> bool:
    if body.get("tool_choice") == "required":
        return True
    for message in reversed(body.get("messages") or []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        return isinstance(content, str) and bool(_EXPLICIT_TOOL_REQUEST_RE.search(content))
    return False


def _action_tool_names(tools: list[dict]) -> set[str]:
    """Names of tools whose own schema takes a path/directory argument — the ones a false
    'wrote/read/created' claim would actually be lying about. Used only to decide whether a
    success-shaped sentence is suspicious; unrelated to _has_path_argument's boolean check."""
    names = set()
    for tool in tools or []:
        function = tool.get("function") or {}
        properties = (function.get("parameters") or {}).get("properties") or {}
        if any("path" in str(key).lower() or "director" in str(key).lower() for key in properties):
            name = function.get("name")
            if isinstance(name, str):
                names.add(name.lower())
    return names


def _looks_like_fake_tool_execution(content: str, tools: list[dict]) -> bool:
    if not content or not content.strip():
        return False
    action_names = _action_tool_names(tools)
    if not action_names:
        return False
    lowered = content.lower()
    mentions_action_tool = any(name in lowered for name in action_names)
    fake_code = bool(_FAKE_CODE_EXEC_RE.search(content)) and mentions_action_tool
    fake_output_block = bool(_FAKE_OUTPUT_BLOCK_RE.search(content))
    false_success_claim = bool(_SUCCESS_CLAIM_RE.search(content)) and mentions_action_tool
    return fake_code or fake_output_block or false_success_claim


def _honest_failure_message(reason: str) -> str:
    return (
        f"I attempted this {_MAX_HALLUCINATION_RETRIES + 1} time(s) but could not get a real "
        f"tool call to run — {reason}. Rather than claim it succeeded, I'm telling you it did "
        "not: please retry, or check whether the requested action needs to be done manually."
    )


def _call_upstream(body: dict, streamed: bool):
    return requests.post(
        f"{UPSTREAM}/v1/chat/completions", json=body, headers=_headers(),
        stream=streamed, timeout=(10, 900),
    )


def _with_corrective_nudge(body: dict) -> dict:
    messages = list(body.get("messages") or [])
    if messages and messages[0].get("role") == "system":
        messages[0] = {**messages[0], "content": (messages[0].get("content") or "") + _CORRECTIVE_NUDGE}
    else:
        messages.insert(0, {"role": "system", "content": _CORRECTIVE_NUDGE.strip()})
    return {**body, "messages": messages}


def _stream_chunks(upstream) -> list[bytes]:
    return [raw + b"\n" if raw else b"\n" for raw in upstream.iter_lines()]


def _stream_content_and_tool_calls(chunks: list[bytes]) -> tuple[str, bool]:
    """Reconstruct the full assistant text and whether any structured tool_calls delta
    appeared anywhere in the stream — used only to decide whether to retry, independent of
    _repair_streamed_textual_tool_calls's own (separate) job of rewriting the stream."""
    content = []
    has_tool_calls = False
    for raw_line in chunks:
        if not raw_line.startswith(b"data: "):
            continue
        payload = raw_line[len(b"data: "):].strip()
        if not payload or payload == b"[DONE]":
            continue
        try:
            delta = json.loads(payload)["choices"][0].get("delta") or {}
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        has_tool_calls = has_tool_calls or bool(delta.get("tool_calls"))
        if delta.get("content"):
            content.append(delta["content"])
    return "".join(content), has_tool_calls


@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    body = request.get_json(force=True, silent=True) or {}
    body = model_test_profiles.apply(body, "lmstudio")
    body = _inject_path_warning(_inject_tools_block(body))
    streamed = bool(body.get("stream"))
    tools = body.get("tools") or []
    tool_required = bool(tools) and _explicitly_requires_tool(body)
    attempt_body = body

    if streamed:
        for attempt in range(_MAX_HALLUCINATION_RETRIES + 1):
            try:
                upstream = _call_upstream(attempt_body, streamed=True)
            except requests.RequestException as exc:
                return {"error": {"message": str(exc), "type": "backend_unavailable"}}, 503
            chunks = _stream_chunks(upstream)
            repaired = _repair_streamed_textual_tool_calls(chunks, body) if tools else None
            if repaired is not None:
                final_chunks = [repaired]
                break
            text, has_tool_calls = _stream_content_and_tool_calls(chunks)
            missed_required_tool = tool_required and not has_tool_calls
            fake_execution = _looks_like_fake_tool_execution(text, tools)
            if has_tool_calls or (not fake_execution and not missed_required_tool) or attempt == _MAX_HALLUCINATION_RETRIES:
                if not has_tool_calls and (fake_execution or missed_required_tool):
                    chunks = _fabricate_honest_stream_chunks(chunks, _honest_failure_message(
                        "the model kept describing the action instead of invoking it"
                    ))
                final_chunks = chunks
                break
            attempt_body = _with_corrective_nudge(body)

        def generate():
            for piece in final_chunks:
                yield piece
        return Response(
            stream_with_context(generate()), status=upstream.status_code,
            content_type=upstream.headers.get("content-type", "text/event-stream"),
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    for attempt in range(_MAX_HALLUCINATION_RETRIES + 1):
        try:
            upstream = _call_upstream(attempt_body, streamed=False)
        except requests.RequestException as exc:
            return {"error": {"message": str(exc), "type": "backend_unavailable"}}, 503
        try:
            payload = upstream.json()
        except ValueError:
            return Response(upstream.content, status=upstream.status_code,
                            content_type=upstream.headers.get("content-type", "application/json"))
        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        if tools and not message.get("tool_calls"):
            clean, calls = _repair_textual_tool_calls(message.get("content") or "", body)
            if calls:
                message["content"] = clean or None
                message["tool_calls"] = calls
                choice["finish_reason"] = "tool_calls"
        if message.get("tool_calls"):
            break
        missed_required_tool = tool_required and not message.get("tool_calls")
        fake_execution = _looks_like_fake_tool_execution(message.get("content") or "", tools)
        if (not fake_execution and not missed_required_tool) or attempt == _MAX_HALLUCINATION_RETRIES:
            if fake_execution or missed_required_tool:
                message["content"] = _honest_failure_message(
                    "the model kept describing the action instead of invoking it"
                )
                choice["finish_reason"] = "stop"
            break
        attempt_body = _with_corrective_nudge(body)

    return Response(json.dumps(payload, separators=(",", ":")), status=upstream.status_code,
                    content_type=upstream.headers.get("content-type", "application/json"))


def _fabricate_honest_stream_chunks(chunks: list[bytes], message: str) -> list[bytes]:
    """Replace a hallucinated-success stream with a single honest chunk, preserving the
    id/model fields from the original response so OpenCode's SSE parser sees a normal shape."""
    template = {}
    for raw_line in chunks:
        if not raw_line.startswith(b"data: "):
            continue
        payload = raw_line[len(b"data: "):].strip()
        if not payload or payload == b"[DONE]":
            continue
        try:
            template = {k: v for k, v in json.loads(payload).items() if k not in {"choices", "usage"}}
            break
        except (ValueError, TypeError):
            continue
    body = json.dumps(
        {**template, "choices": [{"index": 0, "delta": {"content": message}, "finish_reason": "stop"}]},
        separators=(",", ":"),
    ).encode()
    return [b"data: " + body + b"\n\n", b"data: [DONE]\n\n"]


@app.route("/<path:path>", methods=["GET", "POST"])
def passthrough(path):
    upstream = requests.request(
        request.method, f"{UPSTREAM}/{path}", params=request.args,
        data=request.get_data(), headers=_headers(), timeout=30,
    )
    return Response(upstream.content, status=upstream.status_code,
                    content_type=upstream.headers.get("content-type", "application/json"))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=PORT, threaded=True)
