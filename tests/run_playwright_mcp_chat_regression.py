"""Live Playwright MCP regression through Hearth chat and the user's active model.

Run against a running Hearth instance with an active chat model selected for the
configured user:

    .venv/bin/python tests/run_playwright_mcp_chat_regression.py

This is intentionally an end-to-end check: it uses Hearth's live HTTP chat route,
the user's saved active model, and the running Playwright MCP. It does not mock the
model, browser, web search, or MCP client.
"""

from __future__ import annotations

import os
import re
import secrets
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as hearth_app  # noqa: E402
from hearth.core.session_cookie import session_cookie_name  # noqa: E402
from hearth.storage import browse_history_store, users_store  # noqa: E402


BASE_URL = os.environ.get("HEARTH_TEST_BASE_URL", "https://127.0.0.1:5443").rstrip("/")
SALON_NAME = "Market Square"
SALON_ADDRESS = "620 Jefferson Rd, Rochester, NY 14623"
SALON_URL = "https://salons.greatclips.com/us/ny/rochester/620-jefferson-rd"
TIMEOUT_SECONDS = max(60, int(os.environ.get("HEARTH_PLAYWRIGHT_TEST_TIMEOUT", "240")))


def _test_user() -> dict:
    users = users_store.list_users(page=1, per_page=100).get("users", [])
    wanted = os.environ.get("HEARTH_PLAYWRIGHT_TEST_USER", "admin")
    user = next((row for row in users if row.get("username") == wanted), None)
    if user is None:
        raise RuntimeError(
            f"Hearth user {wanted!r} was not found. Set HEARTH_PLAYWRIGHT_TEST_USER "
            "to an existing account with an active chat model."
        )
    return user


def _authenticated_session(user: dict) -> tuple[requests.Session, str]:
    csrf = secrets.token_urlsafe(32)
    session_data = {
        "logged_in": True,
        "user_id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "agentic_workflow_enabled": user.get("agentic_workflow_enabled", False),
        "last_activity": time.time(),
        "login_csrf_token": csrf,
    }
    serializer = hearth_app.app.session_interface.get_signing_serializer(hearth_app.app)
    if serializer is None:
        raise RuntimeError("Hearth's Flask session signer is unavailable")
    cookie_value = serializer.dumps(session_data)
    parsed = urlparse(BASE_URL)
    client = requests.Session()
    client.verify = os.environ.get("HEARTH_TEST_CA_BUNDLE") or False
    client.cookies.set(
        session_cookie_name(hearth_app.APP_PORT), cookie_value,
        domain=parsed.hostname, path="/", secure=parsed.scheme == "https",
    )
    client.headers.update({"X-CSRF-Token": csrf})
    return client, csrf


def _active_chat_model(client: requests.Session, user: dict) -> tuple[str, str]:
    model_id = users_store.get_chat_model(user["id"])
    if not model_id:
        raise RuntimeError(
            f"User {user['username']!r} has no saved active chat model. Select one in Hearth Chat first."
        )
    response = client.get(f"{BASE_URL}/api/models", timeout=30)
    response.raise_for_status()
    models = (response.json() or {}).get("models", [])
    match = next((item for item in models if item.get("id") == model_id), None)
    if not match or match.get("state") != "loaded" or match.get("type") != "llm":
        raise RuntimeError(
            f"The user's active chat model {model_id!r} is not currently loaded in Hearth. "
            "The regression runner will not load or switch models."
        )
    return model_id, str(match.get("backend") or "unknown")


def _saved_assistant_reply(client: requests.Session, conversation_id: str) -> tuple[str, str]:
    response = client.get(f"{BASE_URL}/api/conversations/{conversation_id}", timeout=30)
    response.raise_for_status()
    data = response.json() or {}
    generation = data.get("generation") or {}
    assistant = next(
        (message for message in reversed(data.get("messages") or [])
         if message.get("role") == "assistant"),
        {},
    )
    return str(assistant.get("content") or ""), str(generation.get("status") or "unknown")


def run() -> None:
    user = _test_user()
    client, _csrf = _authenticated_session(user)
    conversation_id = str(uuid.uuid4())
    model_id, backend = _active_chat_model(client, user)
    print(f"Hearth user: {user['username']}")
    print(f"Active model: {model_id} (backend={backend})")

    # This official Rochester, NY salon has a live estimated-wait display. The test
    # checks for a readable wait/result, never submits a check-in, and does not depend
    # on the estimate remaining the same between runs.
    prompt = (
        f"Please use Hearth's Playwright MCP to check the current estimated wait time "
        f"at Great Clips {SALON_NAME}, {SALON_ADDRESS}. Open the official salon page: "
        f"{SALON_URL}. Do not check in or submit any form. Reply with the salon name, "
        "the wait in minutes (or say if no live estimate is shown/it is closed), and "
        "the source link."
    )
    started_at = time.time()
    response = client.post(
        f"{BASE_URL}/api/chat/start",
        json={
            "conversation_id": conversation_id,
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=30,
    )
    if response.status_code != 202:
        raise RuntimeError(
            f"Hearth rejected the live chat request ({response.status_code}): "
            f"{response.text[:1000]}"
        )
    print("Submitted through Hearth /api/chat/start; waiting for the active model and MCP…", flush=True)

    deadline = time.monotonic() + TIMEOUT_SECONDS
    reply = ""
    status = "running"
    while time.monotonic() < deadline:
        reply, status = _saved_assistant_reply(client, conversation_id)
        if status != "running" and reply:
            break
        time.sleep(1)
    if status == "running" or not reply:
        raise RuntimeError(
            f"Hearth chat did not finish within {TIMEOUT_SECONDS}s (status={status}). "
            f"Conversation {conversation_id} is retained for diagnosis."
        )
    if status != "completed":
        raise RuntimeError(
            f"Hearth chat finished with status={status}. Conversation {conversation_id} "
            "is retained for diagnosis.\n" + reply[-3000:]
        )

    plain_reply = re.sub(r"(?m)^\s*🔧 \*Calling `[^`]+`\.\.\.\*\s*$", "", reply).strip()
    tool_calls = re.findall(r"Calling `(playwright__browser_[^`]+)`", reply)
    print(f"Playwright MCP calls: {', '.join(tool_calls) if tool_calls else 'none'}")
    print("Hearth assistant reply:")
    print(plain_reply)

    if not tool_calls:
        raise AssertionError("Hearth returned no visible Playwright MCP browser-tool call")
    if not plain_reply:
        raise AssertionError("The active model returned only tool-call markers, no answer")
    if not re.search(r"market\s+square|great\s+clips", plain_reply, re.I):
        raise AssertionError("The answer did not identify the requested New York Great Clips salon")
    if not re.search(r"\bwait\b", plain_reply, re.I):
        raise AssertionError("The answer did not report a wait estimate or explain why none is shown")

    with browse_history_store._connect() as conn:
        visits = conn.execute(
            "SELECT url FROM browse_history WHERE owner_id = %s AND source = 'playwright-mcp' "
            "AND visited_at >= %s AND url ILIKE %s ORDER BY visited_at DESC LIMIT 10",
            (user["id"], started_at, "%salons.greatclips.com/us/ny/rochester/620-jefferson-rd%"),
        ).fetchall()
    if not visits:
        raise AssertionError(
            "The answer mentioned browser research, but Hearth recorded no Playwright MCP "
            "visit to the New York salon page."
        )
    print("Verified user-scoped Playwright MCP history for the official Rochester salon page.")

    # Keep successful runs out of the user's persistent chat list. On failures the
    # conversation stays saved so the model/tool trace is available for diagnosis.
    cleanup = client.delete(f"{BASE_URL}/api/conversations/{conversation_id}", timeout=30)
    cleanup.raise_for_status()
    print("PASS: live active-model + Playwright MCP chat regression")


if __name__ == "__main__":
    run()
