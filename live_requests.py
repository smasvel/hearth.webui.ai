"""In-memory registry of in-flight/just-finished chat generations, for a real-time
"what's generating right now" view on the metrics page (LM Studio dev-log style).

Active streams are RAM-only. Finished request/response traces are persisted in the local
PostgreSQL database for two days so an administrator can debug a request after a restart;
they are automatically deleted after that retention window.

Only the webui-chat streaming path (app.py's /api/chat worker) can report live token-
by-token progress, because it's the only place this app actually reads each SSE chunk
as it arrives. LAN traffic (OpenCode/VSCode via the Caddy proxy) is invisible here -
caddy_metrics.py only sees a request after Caddy has already written the completed
access-log line, with no per-token body access - so it never appears in this feed.
"""

from __future__ import annotations

import itertools
import json
import database
import threading
import time

_lock = threading.Lock()
_active: dict[int, dict] = {}
_recent: list[dict] = []  # finished entries kept briefly so a fast reply doesn't just vanish

# Finished request/response traces remain available for debugging for two days, including
# across an ai-webui restart. Active streaming state remains RAM-only.
_RECENT_KEEP_SECONDS = 2 * 24 * 60 * 60
_RECENT_MAX = 5000
_TRACE_MAX_CHARS = 4000



def _init_trace_store() -> int:
    with database.connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS developer_traces (
                   id BIGINT PRIMARY KEY, finished_at DOUBLE PRECISION NOT NULL, payload TEXT NOT NULL
               )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_developer_traces_finished ON developer_traces(finished_at)")
        conn.execute("DELETE FROM developer_traces WHERE finished_at < %s", (time.time() - _RECENT_KEEP_SECONDS,))
        maximum = conn.execute("SELECT COALESCE(MAX(id), 0) FROM developer_traces").fetchone()[0]
        for row in conn.execute(
            "SELECT payload FROM developer_traces ORDER BY id DESC LIMIT %s", (_RECENT_MAX,)
        ):
            try:
                entry = json.loads(row["payload"])
                elapsed = float(entry.pop("_elapsed_s", 0))
                entry["started"] = time.monotonic() - elapsed
                entry["updated"] = time.monotonic()
                _recent.append(entry)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        _recent.reverse()
        return int(maximum) + 1


_counter = itertools.count(_init_trace_store())


def clear_all() -> None:
    """Clear finished live-log entries and saved traces without hiding active work."""
    with _lock:
        with database.connect() as conn:
            conn.execute("TRUNCATE TABLE developer_traces")
        _recent.clear()


def _persist_finished(entry: dict) -> None:
    payload = dict(entry)
    payload["_elapsed_s"] = max(0, entry["updated"] - entry["started"])
    payload.pop("started", None)
    payload.pop("updated", None)
    try:
        with database.connect() as conn:
            conn.execute(
                "INSERT INTO developer_traces(id, finished_at, payload) VALUES (%s, %s, %s) ON CONFLICT (id) DO UPDATE SET finished_at = EXCLUDED.finished_at, payload = EXCLUDED.payload",
                (entry["id"], entry["finished_at"], json.dumps(payload)),
            )
            conn.execute(
                "DELETE FROM developer_traces WHERE finished_at < %s",
                (time.time() - _RECENT_KEEP_SECONDS,),
            )
            conn.execute(
                """DELETE FROM developer_traces WHERE id NOT IN
                   (SELECT id FROM developer_traces ORDER BY id DESC LIMIT %s)""",
                (_RECENT_MAX,),
            )
    except (OSError, database.Error, TypeError, ValueError):
        pass


def start(
    *, source: str, backend: str, model: str, max_tokens: int | None = None,
    is_npu: bool = False, request_content: str | None = None,
) -> int:
    now = time.monotonic()
    wall_now = time.time()
    with _lock:
        req_id = next(_counter)
        _active[req_id] = {
            "id": req_id,
            "source": source,
            "backend": backend,
            "model": model,
            "max_tokens": max_tokens,
            "is_npu": is_npu,
            "tokens": 0,
            "chars": 0,
            "started": now,
            "started_at": wall_now,
            "first_token_at": None,
            "finished_at": None,
            "updated": now,
            "status": "streaming",
            "request_content": (request_content or "")[:_TRACE_MAX_CHARS] or None,
            "response_content": "",
            "response_truncated": False,
        }
    return req_id


def update(req_id: int, *, delta_chars: int, delta_content: str | None = None) -> None:
    with _lock:
        entry = _active.get(req_id)
        if entry is None:
            return
        if entry["first_token_at"] is None and delta_chars > 0:
            entry["first_token_at"] = time.time()
        entry["chars"] += delta_chars
        # No per-chunk token count from the backends mid-stream (only the final `usage`
        # object has it) - a ~4-chars-per-token estimate is what drives the live counter
        # and %-of-max-tokens bar; get_snapshot() below swaps in the real completion_tokens
        # once the request finishes.
        entry["tokens"] = max(1, entry["chars"] // 4)
        if delta_content:
            remaining = _TRACE_MAX_CHARS - len(entry["response_content"])
            if remaining > 0:
                entry["response_content"] += delta_content[:remaining]
            if len(delta_content) > remaining:
                entry["response_truncated"] = True
        entry["updated"] = time.monotonic()


def finish(
    req_id: int, *, status: str = "done", total_tokens: int | None = None,
    status_code: int | None = None, error_detail: str | None = None,
) -> None:
    with _lock:
        entry = _active.pop(req_id, None)
        if entry is None:
            return
        entry["status"] = status
        entry["updated"] = time.monotonic()
        entry["finished_at"] = time.time()
        entry["status_code"] = status_code
        entry["error_detail"] = (error_detail or "").strip()[:300] or None
        if total_tokens is not None:
            entry["tokens"] = total_tokens
        _recent.append(entry)
        cutoff = time.time() - _RECENT_KEEP_SECONDS
        while _recent and ((_recent[0].get("finished_at") or 0) < cutoff or len(_recent) > _RECENT_MAX):
            _recent.pop(0)
        # Serialize persistence with clear_all() so a completion racing with a reset
        # cannot write its trace back after the table has been cleared.
        _persist_finished(entry)


def snapshot(limit: int = 100) -> list[dict]:
    now = time.monotonic()
    with _lock:
        cutoff = time.time() - _RECENT_KEEP_SECONDS
        rows = [e for e in _recent if (e.get("finished_at") or 0) >= cutoff] + list(_active.values())
    out = []
    for e in rows:
        elapsed = now - e["started"]
        tps = round(e["tokens"] / elapsed, 1) if elapsed > 0 and e["tokens"] else None
        pct = None
        if e.get("max_tokens"):
            pct = min(100, round(e["tokens"] / e["max_tokens"] * 100))
        out.append({
            "id": e["id"],
            "source": e["source"],
            "backend": e["backend"],
            "model": e["model"],
            "is_npu": e.get("is_npu", False),
            "tokens": e["tokens"],
            "tokens_per_sec": tps,
            "pct": pct,
            "elapsed_s": round(elapsed, 1),
            "started_at": e.get("started_at"),
            "finished_at": e.get("finished_at"),
            "ttft_s": (
                round(e["first_token_at"] - e["started_at"], 3)
                if e.get("first_token_at") and e.get("started_at") else None
            ),
            "status": e["status"],
            "status_code": e.get("status_code"),
            "error_detail": e.get("error_detail"),
            "response_truncated": e.get("response_truncated", False),
        })
    # Developer logs read like a conventional event stream: newest displayed timestamp
    # first. Previously active/status grouping plus elapsed time made completed rows appear
    # out of chronological order even though each row showed a timestamp.
    out.sort(
        key=lambda r: r.get("finished_at") or r.get("started_at") or 0,
        reverse=True,
    )
    return out[:max(1, min(int(limit), 500))]


def trace(request_id: int) -> dict | None:
    """Load one request's saved trace on demand instead of shipping thousands per poll."""
    with _lock:
        entry = next((row for row in _active.values() if row.get("id") == request_id), None)
        if entry is None:
            entry = next((row for row in reversed(_recent) if row.get("id") == request_id), None)
        if entry is not None:
            return {
                "request_content": entry.get("request_content"),
                "response_content": entry.get("response_content") or None,
                "response_truncated": entry.get("response_truncated", False),
            }
    with database.connect() as conn:
        row = conn.execute("SELECT payload FROM developer_traces WHERE id = %s", (request_id,)).fetchone()
    if not row:
        return None
    try:
        entry = json.loads(row["payload"])
    except (TypeError, json.JSONDecodeError):
        return None
    return {
        "request_content": entry.get("request_content"),
        "response_content": entry.get("response_content") or None,
        "response_truncated": entry.get("response_truncated", False),
    }


def npu_busy() -> bool:
    """True if any currently-streaming request (not a just-finished `_recent` one — this is
    "right now," not "in the last few seconds") is actually running on the NPU. Callers
    already tag each entry with `is_npu` at start() time (see live_report.py / the two LAN
    shims / app.py's webui-chat path) — this just answers the one question the metrics page's
    live NPU indicator needs, without every caller re-deriving it from the raw rows."""
    with _lock:
        return any(e.get("is_npu") for e in _active.values())
