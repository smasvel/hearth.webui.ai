"""Thin HTTP client the LAN-facing shims (flm_tool_shim.py, lemonade_guard_shim.py) use to
report live streaming activity into the main app's live_requests.py registry.

Why this exists at all: live_requests.py's registry is an in-memory dict inside the ai-webui
process. The shims are separate OS processes (their own systemd units) sitting inline for
real LAN traffic, so they can't call live_requests.start()/update()/finish() directly - there's
no shared memory. This posts to a small set of internal, localhost-only endpoints on the main
app instead (see app.py's /api/internal/live/* routes), which do the actual registry calls.

Every function here is best-effort and swallows all errors: a metrics-reporting hiccup must
never slow down or break an actual chat/tool-call response. If the main app is down or
unreachable, callers just get None back and the live feed simply doesn't show that request -
no different from the LAN-visibility gap that already existed for LM Studio/vLLM/KoboldCPP.
"""

from __future__ import annotations

import requests
import urllib3

# The main app serves its own self-signed cert (app.py's CERT_FILE/KEY_FILE) - verify=False
# below is expected for this localhost-only call, not a real security concern, so silence the
# warning urllib3 would otherwise log on every single request.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Must match app.py's APP_PORT (the PORT env var, default 5443) - hardcoded rather than
# imported to keep these shims lightweight standalone processes, same reason FLM_BASE/
# LEMONADE_BASE are hardcoded rather than looked up.
_APP_BASE = "https://127.0.0.1:5443"
_TIMEOUT = 2.0


def start(
    *, source: str, backend: str, model: str, max_tokens: int | None = None,
    is_npu: bool = False, request_content: str | None = None,
) -> int | None:
    try:
        resp = requests.post(
            f"{_APP_BASE}/api/internal/live/start",
            json={
                "source": source, "backend": backend, "model": model,
                "max_tokens": max_tokens, "is_npu": is_npu,
                "request_content": request_content,
            },
            timeout=_TIMEOUT, verify=False,
        )
        resp.raise_for_status()
        return resp.json().get("id")
    except requests.RequestException:
        return None


def update(req_id: int | None, *, delta_chars: int, delta_content: str | None = None) -> None:
    if req_id is None:
        return
    try:
        requests.post(
            f"{_APP_BASE}/api/internal/live/update",
            json={"id": req_id, "delta_chars": delta_chars, "delta_content": delta_content},
            timeout=_TIMEOUT, verify=False,
        )
    except requests.RequestException:
        pass


def finish(
    req_id: int | None, *, status: str = "done", total_tokens: int | None = None,
    status_code: int | None = None, error_detail: str | None = None,
) -> None:
    if req_id is None:
        return
    try:
        requests.post(
            f"{_APP_BASE}/api/internal/live/finish",
            json={
                "id": req_id, "status": status, "total_tokens": total_tokens,
                "status_code": status_code, "error_detail": error_detail,
            },
            timeout=_TIMEOUT, verify=False,
        )
    except requests.RequestException:
        pass


def record_metric(**fields) -> None:
    """Record a body-aware historical row from an inline shim. Unlike Caddy's access-log
    tailer, the shim can report model and usage fields from the OpenAI JSON/SSE payload."""
    try:
        requests.post(
            f"{_APP_BASE}/api/internal/metrics/record", json=fields,
            timeout=_TIMEOUT, verify=False,
        )
    except requests.RequestException:
        pass
