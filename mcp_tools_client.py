"""Bridges Hearth's own (synchronous, threaded) chat worker to the MCP servers configured
for OpenCode (~/.config/opencode/opencode.jsonc) -- Playwright browser automation and the
persistent Python REPL -- so the same tools are available from Hearth's own chat UI, not
just OpenCode.

The `mcp` client SDK is async-only; the webui-chat worker in app.py is a plain background
thread, not asyncio. Rather than threading asyncio through the whole chat pipeline, this
module owns one persistent event loop in a dedicated background thread (started once, lives
for the app's lifetime) and exposes plain synchronous functions that hop onto that loop via
asyncio.run_coroutine_threadsafe() and block for the result -- the same bridging pattern
used anywhere a sync codebase needs to call a handful of async operations without going
async everywhere.

Each MCP server is started once (persistent subprocess, same one OpenCode would otherwise
launch itself) and kept alive for the app's lifetime, not spawned fresh per tool call --
matters most for python-repl, whose entire point is a persistent namespace across calls.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
import playwright

import browse_history_store

# Same commands as ~/.config/opencode/opencode.jsonc's "mcp" block -- keep these in sync
# if that config ever changes (e.g. a different venv path after a reinstall).
#
# playwright-mcp needs DISPLAY explicitly: stdio_client's default env (when `env` is
# left unset) is a deliberately restricted safe-variables allowlist that does NOT
# include DISPLAY - reasonable as a general MCP security default, wrong for a tool that
# launches a real (non-headless) browser window. Confirmed live: without this, Chromium
# fails with "Missing X server or $DISPLAY" even though this process's own environment
# has DISPLAY set correctly - the subprocess just never receives it. Hardcoded to this
# machine's VNC display since that's the only display this single-user setup has.
def _chromium_path() -> str:
    override = os.environ.get("HEARTH_CHROMIUM_PATH")
    if override:
        return override
    manifest = Path(playwright.__file__).parent / "driver/package/browsers.json"
    browsers = json.loads(manifest.read_text())["browsers"]
    revision = next(b["revision"] for b in browsers if b["name"] == "chromium")
    cache = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache/ms-playwright")))
    return str(cache / f"chromium-{revision}" / "chrome-linux64/chrome")


_SERVERS = {
    "playwright": StdioServerParameters(
        command=os.environ.get("HEARTH_NODE_BIN", str(Path.home() / ".local/share/hearth/node/bin/node")),
        args=[str(Path.home() / ".local/share/hearth/mcp/node_modules/@playwright/mcp/cli.js"), "--headless", "--browser", "chromium", "--executable-path", _chromium_path()],
        env={**get_default_environment(), "DISPLAY": os.environ.get("DISPLAY", ":2")},
    ),
    "python_repl": StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).parent / "tools/python_repl_mcp.py")],
    ),
}

_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_sessions: dict[str, ClientSession] = {}
_openai_tools_cache: list[dict] | None = None
_init_lock = threading.Lock()


def _run_loop_forever(ready: threading.Event) -> None:
    """Create and run the event loop in its owning thread.

    Python 3.14 no longer reliably wakes a loop that was constructed in one
    thread and then run in another. That left every MCP lifecycle coroutine
    queued forever, so Playwright and the Python REPL both hit the 30-second
    startup timeout. Constructing the loop here also follows asyncio's thread
    ownership contract.
    """
    global _loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _loop = loop

    # Keep a short timer pending so cross-thread submissions are observed even on
    # Python/platform combinations where the selector's self-pipe wakeup is missed.
    # Without a pending timer run_forever() can remain blocked indefinitely despite
    # run_coroutine_threadsafe() having queued work.
    def wake_tick() -> None:
        loop.call_later(0.05, wake_tick)

    loop.call_later(0.05, wake_tick)
    ready.set()
    try:
        loop.run_forever()
    finally:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()
        asyncio.set_event_loop(None)


async def _server_lifecycle(server_name: str, params: StdioServerParameters, ready: threading.Event, tools_out: list) -> None:
    """Owns one MCP server's whole lifetime in a single task, exactly as anyio's
    structured concurrency requires (stdio_client's context manager uses task-group-based
    cancel scopes that must be entered and exited by the same task - a first attempt at
    this that did `await stdio_cm.__aenter__()` and never exited, from what became a
    *different* task once the setup coroutine returned, hit exactly that: "Attempted to
    exit cancel scope in a different task than it was entered in"). This coroutine instead
    stays alive for the app's whole lifetime, blocking on `_forever` after setup - the
    `async with` blocks below only ever get entered and exited by this one task, which
    never actually exits until the app process does."""
    try:
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                listed = await session.list_tools()
                _sessions[server_name] = session
                for tool in listed.tools:
                    tools_out.append({
                        "type": "function",
                        "function": {
                            # Qualified with the server name so call_tool() below knows
                            # which session owns it - OpenAI's tools array is flat, MCP's isn't.
                            "name": f"{server_name}__{tool.name}",
                            "description": tool.description or "",
                            "parameters": tool.inputSchema or {"type": "object", "properties": {}},
                        },
                    })
                ready.set()
                await asyncio.Event().wait()  # block forever - keeps the context open
    except Exception as exc:
        print(f"[mcp_tools_client] '{server_name}' failed: {exc}", flush=True)
        ready.set()  # unblock the waiting thread either way, just with no tools added


def _connect_all_sync(loop: asyncio.AbstractEventLoop) -> list[dict]:
    """Starts every configured MCP server's lifecycle task (each running forever on
    `loop`) and waits, from this thread, for each to finish its setup - a plain
    threading.Event is safe to .set() from the loop's own thread since it isn't an
    asyncio primitive, unlike asyncio.Event which only works within one loop."""
    tools: list[dict] = []
    for server_name, params in _SERVERS.items():
        ready = threading.Event()
        asyncio.run_coroutine_threadsafe(_server_lifecycle(server_name, params, ready, tools), loop)
        if not ready.wait(timeout=30):
            print(f"[mcp_tools_client] '{server_name}' timed out starting up.", flush=True)
    return tools


def ensure_started() -> list[dict]:
    """Idempotent: starts the background loop and connects to every MCP server on first
    call, returns the cached OpenAI-format tool list on every call after that. Safe to
    call from any thread, any number of times - callers don't need to know whether this
    is the first call."""
    global _loop, _loop_thread, _openai_tools_cache
    with _init_lock:
        if _openai_tools_cache is not None:
            return _openai_tools_cache
        loop_ready = threading.Event()
        _loop_thread = threading.Thread(target=_run_loop_forever, args=(loop_ready,), daemon=True)
        _loop_thread.start()
        if not loop_ready.wait(timeout=5) or _loop is None:
            raise RuntimeError("MCP event loop failed to start")
        _openai_tools_cache = _connect_all_sync(_loop)
        return _openai_tools_cache


def available() -> bool:
    """True if at least one MCP server actually started - callers should skip offering
    tools entirely rather than advertise an empty list (some backends treat an empty
    `tools: []` array differently from omitting the field altogether)."""
    return bool(ensure_started())


async def _call(server_name: str, tool_name: str, arguments: dict) -> str:
    session = _sessions.get(server_name)
    if session is None:
        return f"Error: MCP server '{server_name}' is not connected."
    result = await session.call_tool(tool_name, arguments)
    parts = []
    for block in result.content:
        if block.type == "text":
            parts.append(block.text)
        elif block.type == "image":
            parts.append(f"[image result, {len(block.data)} base64 chars - not shown as text]")
        else:
            parts.append(f"[unsupported content type: {block.type}]")
    text = "\n".join(parts) if parts else "(tool returned no content)"
    if result.isError:
        return f"Tool error: {text}"
    return text


def call_tool(qualified_name: str, arguments: dict | str) -> str:
    """Synchronous, thread-safe. `qualified_name` is "server__toolname" as produced by
    ensure_started()'s tool list. `arguments` may be a dict or a JSON string (chat
    completion tool_calls arrive as a JSON-encoded string in every backend this app
    talks to, so accepting either saves every caller from repeating the same json.loads).
    """
    ensure_started()
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError as exc:
            return f"Error: tool arguments were not valid JSON: {exc}"
    if "__" not in qualified_name:
        return f"Error: unknown tool '{qualified_name}' (missing server prefix)."
    server_name, tool_name = qualified_name.split("__", 1)
    assert _loop is not None
    future = asyncio.run_coroutine_threadsafe(_call(server_name, tool_name, arguments), _loop)
    try:
        result = future.result(timeout=120)
        # Browser actions initiated by a model bypass page_scraper.fetch_pages(), the
        # normal history choke point. Record explicit MCP navigations here so online
        # history remains complete whether research was server-driven or tool-driven.
        if server_name == "playwright" and tool_name in {"navigate", "browser_navigate"}:
            url = arguments.get("url") if isinstance(arguments, dict) else None
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                failed = result.startswith(("Error:", "Tool error:"))
                browse_history_store.add_visit(
                    url=url,
                    title="Playwright MCP navigation",
                    query=url,
                    source="playwright-mcp",
                    ok=not failed,
                )
        return result
    except Exception as exc:
        if server_name == "playwright" and tool_name in {"navigate", "browser_navigate"}:
            url = arguments.get("url") if isinstance(arguments, dict) else None
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                browse_history_store.add_visit(
                    url=url,
                    title="Playwright MCP navigation",
                    query=url,
                    source="playwright-mcp",
                    ok=False,
                )
        return f"Error calling tool '{qualified_name}': {exc}"
