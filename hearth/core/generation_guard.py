"""Response guards for malformed local-model streaming output."""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator

_SYMBOL_RUN_RE = re.compile(r"([^\w\s])\1{23,}$")
_WINDOW = 300


def looks_like_repetition_collapse(text: str) -> bool:
    """Detect terminal token loops without rejecting normal prose or source code."""
    if not text:
        return False
    tail = text[-_WINDOW:]
    if _SYMBOL_RUN_RE.search(tail):
        return True
    return len(tail) == _WINDOW and len(set(tail)) <= 12


def guard_sse_stream(lines: Iterable[str]) -> Iterator[str]:
    """Forward events immediately, stopping a response if output collapses into a loop.

    Buffering the whole response made chat appear frozen until generation completed.
    The guard therefore observes the content as it streams and cuts off once the
    repetition signal is strong enough. Earlier content cannot be retracted, but the
    loop itself is bounded and the client receives the stop notice immediately.
    """
    content = []
    for line in lines:
        if line.startswith("data:"):
            payload = line[len("data:"):].strip()
            if payload and payload != "[DONE]":
                try:
                    piece = json.loads(payload)["choices"][0]["delta"].get("content")
                except (ValueError, KeyError, IndexError, TypeError):
                    piece = None
                if piece:
                    content.append(piece)
                    if looks_like_repetition_collapse("".join(content)):
                        message = (
                            "⚠️ Generation stopped because the model entered a repetition loop. "
                            "Please retry, lower the temperature, or choose a larger model."
                        )
                        yield "data: " + json.dumps({"choices": [{"delta": {"content": message}, "finish_reason": "stop"}]})
                        yield "data: [DONE]"
                        return
        yield line


def guard_sse_bytes(chunks: list[bytes]) -> list[bytes]:
    """Byte-oriented wrapper for reverse proxies that stream raw SSE lines."""
    decoded = [chunk.decode("utf-8", errors="replace").rstrip("\n") for chunk in chunks]
    guarded = list(guard_sse_stream(decoded))
    if guarded == decoded:
        return chunks
    return [(line + "\n\n").encode("utf-8") for line in guarded]


def guard_completion_payload(payload: dict) -> bool:
    """Replace collapsed content in an OpenAI-compatible non-stream response in place."""
    try:
        choice = payload["choices"][0]
        message = choice["message"]
        content = message.get("content") or ""
    except (KeyError, IndexError, TypeError):
        return False
    if not looks_like_repetition_collapse(content):
        return False
    message["content"] = (
        "⚠️ Generation stopped because the model entered a repetition loop. "
        "Please retry, lower the temperature, or choose a larger model."
    )
    choice["finish_reason"] = "stop"
    return True
