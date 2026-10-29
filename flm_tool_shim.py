"""Tiny reverse-proxy shim between Caddy's :8447 LAN endpoint and FastFlowLM's own server
(127.0.0.1:52625) — fixes exactly one gap in FastFlowLM's OpenAI-compat surface so OpenCode's
tool-calling actually works against it.

Real diagnosis (2026-07-25): OpenCode sends `tools`-bearing chat requests; FastFlowLM's
/v1/chat/completions accepted the `tools` field but never rendered its own model's
chat_template.jinja "# Tools" system block from it — confirmed by inspecting
~/.config/flm/models/*/chat_template.jinja directly, which DOES have full tool-calling support
(the model's own fine-tuning format: `<tool_call>\n<function=NAME>\n<parameter=P>value</parameter>
</function>\n</tool_call>`), but that block only gets rendered if the `tools` variable is
populated in the *template's Jinja context*, which FLM's server evidently never does. Without
it, the model has no idea it's supposed to emit that format and just answers in prose.

The important discovery: FastFlowLM's response-side tool-call DETECTION is already fully
correct — feed it a request where that exact system block has been inserted as a plain,
literal system message (bypassing whatever FLM does or doesn't do with the `tools` field
internally), and FLM's own output parsing already returns proper structured `tool_calls`,
in both streaming and non-streaming mode, and correctly consumes prior `tool_calls`/role="tool"
history on follow-up turns — verified live for all three. The shim now also validates complete tool responses and ends blocked operations with
a normal assistant stop response, so client agents cannot retry synthetic shell results.

Runs as its own systemd --user unit (flm-tool-shim.service) on 127.0.0.1:52626. Caddy's :8447
block should point its `reverse_proxy` at this port instead of FastFlowLM's raw 52625 so LAN
clients (OpenCode) get the fix; hitting FastFlowLM at 52625 directly (e.g. this app's own
fastflowlm_client.py, which never sends `tools`) is unaffected either way.
"""
from __future__ import annotations

import json
import question_tool_compat
import tool_argument_json
import exact_tool_contract
import exact_file_content
import re
import threading
import time
from pathlib import Path

import requests
import model_test_profiles
import agent_request_policy
from flask import Flask, Response, request, stream_with_context

import fastflowlm_client as fastflowlm
import live_report
import npu_request_lock
import flm_context

FLM_BASE = "http://127.0.0.1:52625"
LEMONADE_BASE = "http://127.0.0.1:13305"
LISTEN_PORT = 52626

app = Flask(__name__)
_SERVICE_START_LOCK = threading.Lock()


def _free_lemonade_npu() -> None:
    """FastFlowLM (this shim's upstream) and Lemonade both drive the one physical NPU and
    can't hold a model loaded at the same time without risking an OOM kill.

    Real incident (2026-07-26): Lemonade had its own independent copy of qwen3.6-moe:35b-a3b
    loaded (26GB) while a LAN request came through this shim and made FLM_BASE load the same
    model to answer it. Together they exceeded this machine's 43GB of RAM and the kernel
    OOM-killed flm.service outright, breaking the in-flight OpenCode file-creation tool call.
    app.py already has this exact mutual-exclusion policy (_free_npu_except), but it only
    runs when a load is triggered through this app's own UI/API — it never sees a request
    that arrives straight through Caddy to this shim, which is exactly how every real
    OpenCode/Continue request gets here. So this shim enforces the same policy itself, right
    before forwarding, since it's the only thing actually inline for that traffic.

    Cheap in the common case (one GET; unload is only called if something's actually
    loaded), and fails silently if Lemonade isn't running — this must never block a
    FastFlowLM request that would otherwise be perfectly fine."""
    try:
        resp = requests.get(f"{LEMONADE_BASE}/v1/health", timeout=3)
        resp.raise_for_status()
        for entry in resp.json().get("all_models_loaded", []):
            if entry.get("recipe") == "flm" and entry.get("loaded"):
                requests.post(
                    f"{LEMONADE_BASE}/v1/unload",
                    json={"model_name": entry["model_name"]}, timeout=10,
                )
    except requests.RequestException:
        pass


def _ensure_fastflow_ready(model: str) -> None:
    """Bring the standalone FLM server back after a Lemonade NPU request stopped it.

    The two runtimes must remain mutually exclusive, but that handoff should be invisible
    to callers.  Previously the Lemonade guard correctly stopped flm.service and the
    FastFlow shim then kept proxying to the dead port forever.  Serialize cold starts so
    simultaneous OpenCode/Continue requests do not race systemd.
    """
    requested = fastflowlm.resolve_model(model)
    active = (fastflowlm.current_served_model() or "").removeprefix("flm/")
    if fastflowlm.is_active() and active == requested:
        return
    with _SERVICE_START_LOCK:
        active = (fastflowlm.current_served_model() or "").removeprefix("flm/")
        if fastflowlm.is_active() and active == requested:
            return
        # A model switch requested directly by OpenCode/Continue used to be left to
        # FLM's request handler, which loaded the new weights over the old ones. Stop
        # the runtime first so LAN traffic follows the machine-wide unload-before-load
        # rule too. The caller already owns the cross-process NPU lock.
        fastflowlm.unload_model(npu_lock_held=True)
        fastflowlm.load_model(model)

# Debug capture, added 2026-07-25 after a report of an agent `write` tool call arriving with
# a required argument (filePath) missing entirely — no way to diagnose that after the fact
# without seeing the model's raw tool_call output, and nothing here previously logged response
# bodies at all. Cline tool-enabled requests are also captured because a failed model response
# can contain only narration and no "tool_calls" marker. Normal chat traffic remains excluded;
# the file and individual entries are capped so a busy session cannot fill the disk.
_DEBUG_LOG = Path.home() / ".cache" / "hearth" / "flm_shim_toolcalls.log"
_DEBUG_LOG_MAX_BYTES = 5_000_000
_DEBUG_LOG_MAX_ENTRY_BYTES = 250_000
_DEBUG_LOG_KEEP_SECONDS = 2 * 24 * 60 * 60


def _log_tool_call_capture(direction: str, raw: bytes, *, force: bool = False) -> None:
    if not force and b"tool_calls" not in raw:
        return
    try:
        _DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        if (_DEBUG_LOG.exists()
                and time.time() - _DEBUG_LOG.stat().st_mtime > _DEBUG_LOG_KEEP_SECONDS):
            _DEBUG_LOG.unlink()
        if _DEBUG_LOG.exists() and _DEBUG_LOG.stat().st_size > _DEBUG_LOG_MAX_BYTES:
            _DEBUG_LOG.unlink()
        with _DEBUG_LOG.open("ab") as f:
            f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {direction} ===\n".encode())
            if len(raw) > _DEBUG_LOG_MAX_ENTRY_BYTES:
                half = _DEBUG_LOG_MAX_ENTRY_BYTES // 2
                f.write(raw[:half])
                f.write(f"\n... {len(raw) - (2 * half)} bytes omitted ...\n".encode())
                f.write(raw[-half:])
            else:
                f.write(raw)
            f.write(b"\n")
    except OSError:
        pass

# Verbatim from chat_template.jinja lines 47-53 (the model's own trained tool-calling format),
# so a model given this system content behaves exactly as if FLM had rendered its own template
# with `tools` populated.
_TOOLS_BLOCK_TMPL = (
    "# Tools\n\nYou have access to the following functions:\n\n<tools>{tools}\n</tools>\n\n"
    "If you choose to call a function ONLY reply in the following format with NO suffix:\n\n"
    "<tool_call>\n<function=example_function_name>\n<parameter=example_parameter_1>\nvalue_1\n</parameter>\n"
    "<parameter=example_parameter_2>\nThis is the value for the second parameter\nthat can span\nmultiple lines\n"
    "</parameter>\n</function>\n</tool_call>\n\n<IMPORTANT>\nReminder:\n"
    "- Function calls MUST follow the specified format: an inner <function=...></function> block must be nested "
    "within <tool_call></tool_call> XML tags\n- Required parameters MUST be specified\n"
    "- You may provide optional reasoning for your function call in natural language BEFORE the function call, "
    "but NOT after\n- If there is no function call available, answer the question like normal with your current "
    "knowledge and do not tell the user about function calls\n</IMPORTANT>"
)

# Not part of the model's trained template (that block above stays verbatim, deliberately, so
# the model still sees exactly the format it was fine-tuned on) - appended separately. Added
# 2026-07-25 after catching this live in flm.service's own request/response log on a real
# OpenCode session: at ~14k tokens of accumulated context, the model opened a parameter block
# with `</parameter=pattern>` (a closing-tag spelling) instead of `<parameter=pattern>`. FLM's
# own server-side parser doesn't recover from that malformed opening tag - it silently drops
# that one parameter rather than erroring, so the tool call reaches OpenCode missing whichever
# argument got mistyped. When that argument happens to be required (e.g. `write`'s `filePath`),
# OpenCode's own schema validation then rejects the call with a "Missing key" error. This can't
# be fixed on the response side - FLM never exposes the raw pre-parse text over HTTP, only the
# already-mangled JSON - so this is the only lever available: make the one specific mistake
# observed maximally hard to repeat.
_PARAMETER_TAG_WARNING = (
    "\n\n<CRITICAL>\nEvery parameter block starts with an OPENING tag in the exact form "
    "<parameter=NAME> - a less-than sign, then parameter=NAME, then a greater-than sign. "
    "NEVER write </parameter=NAME> to start a parameter (that is a CLOSING tag spelling and is "
    "wrong there). Only the bare </parameter> (no name inside it) may ever follow a slash. "
    "Double-check the very first parameter of every function call in particular - re-read it "
    "before sending.\n</CRITICAL>"
    "\n\n<FILE_TOOL_RELIABILITY>\n"
    "Use the exact parameter names in each offered tool schema. The path parameter "
    "(path or filePath) contains only the path, never file content or markup. Write content "
    "belongs in its separate content parameter. For shell tools use command. When a "
    "structured write/edit tool exists, do not use bash, heredocs, python -c, sed -i, or "
    "redirection to create or rewrite files. Do not install packages unless the user "
    "explicitly requested installation. After editing code, run its existing syntax/tests; "
    "never claim completion after a truncated generation or failed test. write_to_file/write "
    "is only for a new file known not to exist. Never rewrite an existing file because "
    "replace_in_file/edit failed, even if the client suggests that fallback after several "
    "failures. Re-read the file and use a smaller exact replacement or structured patch, "
    "preserving all unrelated content. Never put a read/verification call in the same "
    "parallel tool-call batch as the write or edit it depends on. Wait for the write/edit "
    "result, then verify in the next turn. If any tool result reports an error, retry or "
    "report the failure; never claim completion.\n"
    "</FILE_TOOL_RELIABILITY>"
)


def _tools_system_block(tools: list[dict]) -> str:
    rendered = "".join("\n" + json.dumps(t) for t in tools)
    return _TOOLS_BLOCK_TMPL.format(tools=rendered) + _PARAMETER_TAG_WARNING


def _inject_tools_block(body: dict) -> dict:
    """If the request carries `tools`, prepend/merge the rendered block into the first system
    message (or add one) — same placement chat_template.jinja itself uses. No-op otherwise."""
    tools = body.get("tools")
    if not tools or body.get("tool_choice") == "none":
        return body
    model = str(body.get("model") or "").removeprefix("flm/")
    if model == "minicpm-v-4.7:1b":
        instruction = (
            "FastFlowLM MiniCPM tool contract: when a user requests an offered function, "
            "return the function call in the model's native tool format. Do not replace a "
            "tool call with Python, shell code, JSON examples, or a claim that an action ran. "
            "Use the exact function name and arguments requested. If the user asks for a "
            "direct text answer or says to stop using tools, do not emit a function call."
        )
        messages = list(body.get("messages") or [])
        if messages and messages[0].get("role") == "system":
            messages[0] = {**messages[0], "content": instruction + "\n\n" + (messages[0].get("content") or "")}
        else:
            messages.insert(0, {"role": "system", "content": instruction})
        return {**body, "messages": messages}
    # Qwen3.8's current runtime renders the tools itself. Injecting the legacy
    # workaround as well includes every schema twice and overflows its 8K window.
    # Keep the workaround for older runtimes/models that still require it.
    if model.startswith("qwen3.8"):
        return body
    if model.startswith(("qwen3:", "qwen3-it:")):
        block = ("# Tools\n\nFunction signatures:\n<tools>\n" +
                 "\n".join(json.dumps(tool) for tool in tools) +
                 '\n</tools>\nFor each function call return a JSON object within <tool_call> tags:\n'
                 '<tool_call>\n{"name": "function_name", "arguments": {"parameter": "value"}}\n</tool_call>\n'
                 'Use exactly the names and parameters in the offered schemas. Read project files before editing.')
    else:
        block = _tools_system_block(tools)
    block += (
        '\nOmit optional arguments unless they are needed for the requested operation. '
        'For a whole-file read, send only the declared path field; omit offset and limit. '
        'Use offset/limit only for an explicitly requested page or to continue a truncated read. '
        'Omit replaceAll unless the user explicitly requests replacing all matches. '
        'When exact arguments are supplied, do not add extra fields.'
    )
    messages = list(body.get("messages") or [])
    if messages and messages[0].get("role") == "system":
        existing = messages[0].get("content") or ""
        messages[0] = {**messages[0], "content": f"{block}\n\n{existing}" if existing else block}
    else:
        messages = [{"role": "system", "content": block}] + messages
    return {**body, "messages": messages}


_DEFAULT_FREQUENCY_PENALTY = 0.4


_TEXT_TOOL_BLOCK_RE = re.compile(r"<tool_call\b[^>]*>(.*?)</tool_call>", re.I | re.S)
_TEXT_FUNCTION_RE = re.compile(
    r"<function\s*=\s*([^>\s]+)\s*>(.*?)(?:</function>|$)", re.I | re.S
)
_TEXT_PARAMETER_RE = re.compile(
    r"<parameter\s*=\s*([^>\s]+)\s*>(.*?)(?:</parameter>|$)", re.I | re.S
)
_TEXT_DIRECT_ARG_RE = re.compile(r"<([A-Za-z_][\w.-]*)\s*=\s*([^<>\r\n]+)>")
_TEXT_FUNCTION_ELEMENT_RE = re.compile(r'<function>\s*([\w.-]+)\s*</function>', re.I)
_TEXT_PARAMETER_ELEMENT_RE = re.compile(r'<parameter>(.*?)</parameter>', re.I | re.S)
# JSON code fences are how small models (observed 2026-10: LM Studio qwen3.8-2b-distill,
# exact-args suite) emit tool calls as plain text: a single ```json object, or a
# ```json array for parallel calls. Only json-tagged fences qualify; ```bash and
# ```python blocks are answer code, never call markup.
_JSON_FENCE_RE = re.compile(r"```json[^\n]*\n(.*?)```", re.I | re.S)
_GEMMA_TOOL_RE = re.compile(r'<\|tool_call>call:([\w.-]+)(\{.*?\})(?:<tool_call\|>|<turn\|>)', re.S)



def _restore_schema_types(value, schema):
    """Restore unambiguous runtime string encodings using the offered schema."""
    expected = schema.get("type")
    if isinstance(value, str) and expected in ("boolean", "integer", "number", "object", "array", "null"):
        candidate = value.strip()
        if expected == "boolean" and candidate.lower() in ("true", "false"):
            value = candidate.lower() == "true"
        else:
            try:
                parsed = json.loads(candidate)
            except ValueError:
                parsed = value
            valid = {"integer": type(parsed) is int,
                     "number": type(parsed) in (int, float),
                     "boolean": type(parsed) is bool,
                     "object": isinstance(parsed, dict),
                     "array": isinstance(parsed, list), "null": parsed is None}
            if valid.get(expected, False):
                value = parsed
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        return {key: _restore_schema_types(item, properties.get(key, {}))
                for key, item in value.items()}
    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            return [_restore_schema_types(item, items) for item in value]
    return value


def _repair_textual_tool_calls(text: str, body: dict) -> tuple[str, list[dict]]:
    """Convert model-native tool markup leaked as content into OpenAI tool calls.

    Handles XML <tool_call> blocks, Gemma native tool markers, and ```json fences
    carrying one call object or an array of call objects. A fenced object without a
    name is only accepted when the request itself forces exactly one function,
    so an ordinary JSON answer can never become an executable call. Only names
    present in the request's tool schemas are accepted.
    """
    allowed = {
        str((tool.get("function") or {}).get("name"))
        for tool in body.get("tools") or []
        if (tool.get("function") or {}).get("name")
    }
    if body.get("tool_choice") == "none":
        return text, []
    schemas = {t["function"]["name"]: t["function"].get("parameters", {})
               for t in body.get("tools") or [] if (t.get("function") or {}).get("name")}

    def spec_from_value(value):
        if not isinstance(value, dict):
            return None
        fn = value.get("function")
        if isinstance(fn, dict):
            name = fn.get("name")
            arguments = fn.get("arguments")
        else:
            name = value.get("name") or (fn if isinstance(fn, str) else None)
            arguments = value.get("arguments")
            if arguments is None:
                arguments = {k: v for k, v in value.items() if k not in {"function", "name"}}
        if not isinstance(name, str) or name not in allowed:
            return None
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = {"value": arguments}
        if not isinstance(arguments, dict):
            arguments = {}
        return name, arguments

    def forced_name():
        choice = body.get("tool_choice")
        if isinstance(choice, dict) and choice.get("type") == "function":
            name = (choice.get("function") or {}).get("name")
            return name if name in allowed and name in schemas else None
        if choice == "required" and len(schemas) == 1:
            (only,) = schemas.keys()
            return only
        return None

    entries = []  # (span, [specs]) in document order; spans removed after collection

    for match in _GEMMA_TOOL_RE.finditer(text or ''):
        if match.group(1) not in allowed:
            continue
        import gemma_tool_arguments
        try:
            arguments = gemma_tool_arguments.loads(match.group(2))
        except (ValueError, RecursionError):
            continue
        entries.append((match.span(), [(match.group(1), arguments)]))

    for match in _TEXT_TOOL_BLOCK_RE.finditer(text or ""):
        payload = match.group(1).strip()
        specs = []
        try:
            value = json.loads(payload)
        except (TypeError, ValueError):
            value = None
        if isinstance(value, dict):
            spec = spec_from_value(value)
            if spec:
                specs.append(spec)
        if not specs:
            element = _TEXT_FUNCTION_ELEMENT_RE.search(payload)
            if element and element.group(1) in allowed:
                name = element.group(1)
                properties = schemas[name].get('properties') or {}
                parameters = _TEXT_PARAMETER_ELEMENT_RE.findall(payload)
                if len(properties) == len(parameters) == 1:
                    specs.append((name, {next(iter(properties)): parameters[0].strip()}))
                elif not properties and not parameters:
                    specs.append((name, {}))
        if not specs:
            fn_match = _TEXT_FUNCTION_RE.search(payload)
            if fn_match:
                name = fn_match.group(1).strip().strip('"\'')
                fn_body = fn_match.group(2)
                arguments = {
                    p.group(1).strip(): p.group(2).strip()
                    for p in _TEXT_PARAMETER_RE.finditer(fn_body)
                }
                # Recover the malformed but frequent shorthand dial=example.com>.
                if not arguments:
                    arguments = {
                        p.group(1).strip(): p.group(2).strip().strip('"\'')
                        for p in _TEXT_DIRECT_ARG_RE.finditer(fn_body)
                        if p.group(1).lower() not in {"function", "parameter"}
                    }
                if name in allowed:
                    if not isinstance(arguments, dict):
                        arguments = {}
                    specs.append((name, arguments))
        if specs:
            entries.append((match.span(), specs))

    for match in _JSON_FENCE_RE.finditer(text or ""):
        try:
            value = json.loads(match.group(1))
        except (TypeError, ValueError):
            continue
        if isinstance(value, list):
            specs = [spec_from_value(item) for item in value if isinstance(item, dict)]
            # Only convert when every array item resolves to an allowed tool.
            if specs and all(s is not None for s in specs) and len(specs) == len(value):
                entries.append((match.span(), specs))
        else:
            spec = spec_from_value(value) if isinstance(value, dict) else None
            if spec is None and isinstance(value, dict) and value.get("name") is None \
                    and value.get("arguments") is None:
                forced = forced_name()
                if forced:
                    spec = (forced, value)
            if spec:
                entries.append((match.span(), [spec]))

    if not entries:
        return text, []

    entries.sort(key=lambda entry: entry[0])
    # A JSON fence may itself occur inside a tool_call block.
    entries = [entry for index, entry in enumerate(entries)
               if not any(outer[0][0] <= entry[0][0] and entry[0][1] <= outer[0][1]
                          for outer in entries[:index])]

    calls = []
    for span, specs in entries:
        for name, arguments in specs:
            arguments = _restore_schema_types(arguments, schemas.get(name, {}))
            calls.append({
                "id": f"call_repaired_{len(calls) + 1}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, separators=(",", ":"))},
            })
    clean_parts, cursor = [], 0
    for span, _specs in entries:
        start, stop = span
        clean_parts.append(text[cursor:start])
        cursor = stop
    clean_parts.append(text[cursor:])
    return ''.join(clean_parts).strip(), calls


def _repair_streamed_textual_tool_calls(raw_lines: list[bytes], body: dict) -> bytes | None:
    """Return a replacement SSE stream when raw content contains leaked tool calls."""
    content = []
    has_structured_calls = False
    template = None
    usage = None
    for raw_line in raw_lines:
        if not raw_line.startswith(b"data: "):
            continue
        payload = raw_line[len(b"data: "):].strip()
        if not payload or payload == b"[DONE]":
            continue
        try:
            obj = json.loads(payload)
            usage = obj.get('usage') or usage
            choice = obj["choices"][0]
            delta = choice.get("delta") or {}
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        template = obj
        has_structured_calls = has_structured_calls or bool(delta.get("tool_calls"))
        if delta.get("content"):
            content.append(delta["content"])
    if has_structured_calls:
        return None
    clean, calls = _repair_textual_tool_calls("".join(content), body)
    if not calls:
        return None
    base = {k: v for k, v in (template or {}).items() if k not in {"choices", "usage"}}
    pieces = []
    if clean:
        pieces.append(b"data: " + json.dumps({**base, "choices": [{"index": 0, "delta": {"content": clean}, "finish_reason": None}]}, separators=(",", ":")).encode() + b"\n\n")
    pieces.append(b"data: " + json.dumps({**base, "choices": [{"index": 0, "delta": {"tool_calls": [{**tc, "index": i} for i, tc in enumerate(calls)]}, "finish_reason": "tool_calls"}]}, separators=(",", ":")).encode() + b"\n\n")
    if usage:
        pieces.append(b'data: ' + json.dumps({**base, 'choices': [], 'usage': usage}).encode() + b'\n\n')
    pieces.append(b"data: [DONE]\n\n")
    return b"".join(pieces)


def _guard_streamed_tool_calls(raw_lines: list[bytes], body: dict) -> bytes | None:
    """Validate complete calls, including arguments split across SSE chunks."""
    calls = {}
    text = []
    template = {}
    usage = None
    finish = "tool_calls"
    for line in raw_lines:
        if not line.startswith(b"data: "):
            continue
        try:
            obj = json.loads(line[6:])
        except ValueError:
            continue
        if obj.get("usage"):
            usage = obj["usage"]
        for choice in obj.get("choices", []):
            template = {k: v for k, v in obj.items() if k not in {"choices", "usage"}}
            delta = choice.get("delta") or {}
            if delta.get("content"):
                text.append(delta["content"])
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
            for call in delta.get("tool_calls") or []:
                index = call.get("index", 0)
                target = calls.setdefault(index, {"index": index, "type": "function",
                                                   "function": {"name": "", "arguments": ""}})
                if call.get("id"):
                    target["id"] = call["id"]
                for key in ("name", "arguments"):
                    target["function"][key] += (call.get("function") or {}).get(key) or ""
    stream_changed = False
    for index, call in list(calls.items()):
        fn = call["function"]
        if not fn["name"] and fn["arguments"].strip() in ("", "{}"):
            del calls[index]
            stream_changed = True
        elif not call.get("id"):
            call["id"] = f"call_hearth_{time.time_ns()}_{index}"
            stream_changed = True
    if not calls and stream_changed:
        choice = {"index": 0, "delta": {"content": "".join(text)}, "finish_reason": "stop"}
        if _repair_tool_arguments({"choices": [choice]}, body):
            calls = {i: call for i, call in enumerate(choice["delta"].get("tool_calls") or [])}
            text = []
            finish = "tool_calls"
            recovered_markup = True
        else:
            event = {**template, "choices": [choice]}
            if usage:
                event["usage"] = usage
            return b"data: " + json.dumps(event).encode() + b"\n\ndata: [DONE]\n\n"
    if not calls:
        choice = {"index": 0, "delta": {"content": "".join(text)}, "finish_reason": finish}
        if _repair_tool_arguments({"choices": [choice]}, body):
            calls = {i: call for i, call in enumerate(choice["delta"].get("tool_calls") or [])}
            text = []
            finish = "tool_calls"
            recovered_markup = True
        else:
            clean, recovered = _repair_textual_tool_calls("".join(text), body)
            if not recovered:
                return None
            text = [clean]
            calls = dict(enumerate(recovered))
            finish = "tool_calls"
            recovered_markup = True
    else:
        recovered_markup = False
    choice = {"index": 0, "delta": {"content": "".join(text), "tool_calls": list(calls.values())},
              "finish_reason": finish}
    if not _repair_tool_arguments({"choices": [choice]}, body) and not recovered_markup and not stream_changed:
        return None
    for index, call in enumerate(choice["delta"].get("tool_calls") or []):
        call.setdefault("index", index)
    event = {**template, "choices": [choice]}
    if usage:
        event["usage"] = usage
    return b"data: " + json.dumps(event).encode() + b"\n\ndata: [DONE]\n\n"


def _native_tool_parser_failed(lines):
    content = []
    for line in lines:
        if not line.startswith(b'data: '):
            continue
        try:
            event = json.loads(line[6:])
        except ValueError:
            continue
        for choice in event.get('choices') or []:
            delta = choice.get('delta') or {}
            if delta.get('tool_calls'):
                return False
            content.append(delta.get('content') or '')
    return ''.join(content).strip() == '[Error parsing tool call]'


def _recover_native_tool_parser(body, headers):
    """One non-stream attempt; the caller retains ownership of the NPU lock."""
    retry_body = {k: v for k, v in body.items() if k != 'stream_options'}
    retry_body['stream'] = False
    headers = {k: v for k, v in headers.items() if k.lower() != 'accept'}
    try:
        with requests.post(f'{FLM_BASE}/v1/chat/completions', json=retry_body,
                           headers=headers, timeout=(10, 90)) as response:
            response.raise_for_status()
            payload = response.json()
        _repair_tool_arguments(payload, body)
        choice = (payload.get('choices') or [{}])[0]
        message = choice.get('message') or {}
        if not message.get('tool_calls'):
            clean, calls = _repair_textual_tool_calls(message.get('content') or '', body)
            if calls:
                message.update(content=clean or None, tool_calls=calls)
                choice['finish_reason'] = 'tool_calls'
                _repair_tool_arguments(payload, body)
                message = choice.get('message') or {}
        if not message.get('tool_calls') and not message.get('content'):
            return None
        base = {k: v for k, v in payload.items() if k not in ('choices', 'usage')}
        base['object'] = 'chat.completion.chunk'
        delta = dict(message)
        if delta.get('tool_calls'):
            delta['tool_calls'] = [{**call, 'index': i} for i, call in enumerate(delta['tool_calls'])]
        event = {**base, 'choices': [{'index': 0, 'delta': delta,
                                     'finish_reason': choice.get('finish_reason')}]}
        if payload.get('usage'):
            event['usage'] = payload['usage']
        wire = b'data: ' + json.dumps(event).encode() + b'\n\ndata: [DONE]\n\n'
        return wire, payload.get('usage') or {}
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        return None


def _ensure_frequency_penalty(body: dict) -> dict:
    """Without a repetition penalty, this NPU-quantized model degenerates into a repeated-token
    loop on longer generations — confirmed live three times now: pure '/' characters before (a
    corrupt weights issue, since fixed), '?'/fragment repetition through OpenCode via this shim,
    and a run of garbled CJK-character repetition mid-tool-call-argument at ~22k tokens of
    context (2026-07-25, see _looks_degenerate below). Raised from the original 0.3 to 0.4 after
    that third occurrence — still a moderate value, since too high a penalty measurably hurts
    normal generation quality, and this alone clearly doesn't eliminate the failure mode (hence
    _looks_degenerate as a second, reactive line of defense). fastflowlm_client.py already
    guards its own (non-shimmed) calls to FLM with frequency_penalty=0.3 by default (see its
    chat_stream docstring) — but requests coming through this shim from OpenCode never pass
    through that code path at all, so they need their own default applied here. Only fills it in
    if the caller didn't already set one."""
    if "frequency_penalty" in body:
        return body
    return {**body, "frequency_penalty": _DEFAULT_FREQUENCY_PENALTY}


# Reactive line of defense, added 2026-07-25 after catching a live occurrence: mid-generation of
# a `write` tool call's `content` argument, the model dropped into a degenerate repetition loop
# (600+ tokens of repeated CJK punctuation/characters) and never reached the `filePath`
# parameter at all - burning ~90s of NPU time to produce a tool call OpenCode then rejected with
# a schema error anyway. frequency_penalty (above) is the proactive lever and clearly doesn't
# eliminate this; this is the reactive one - watch the accumulating text as it streams and cut
# the generation off the moment it looks degenerate, rather than let it run to completion.
#
# Two independent, deliberately conservative signals (either one alone risks false positives):
#   - very low character diversity in a trailing window: catches simple loops (pure '/', '?',
#     the CJK case above all show far fewer distinct characters in any 300-char span than real
#     text/code ever does)
#   - a heavy non-ASCII flood in a trailing window: this shim only ever sees English tool
#     descriptions and code (OpenCode's domain), so a sudden wall of CJK/other non-ASCII
#     characters is already anomalous there, independent of repetition - catches degenerate runs
#     that happen to cycle through enough distinct characters to dodge the diversity check.
# Only armed when the request actually carries `tools` (this shim's whole reason to exist) -
# never applied to this app's own multilingual webui chat traffic, which never comes through here.
_DEGEN_WINDOW = 300
_DEGEN_MIN_LEN = 300
_DEGEN_MAX_DISTINCT = 15
_DEGEN_NON_ASCII_RATIO = 0.4


def _looks_degenerate(text: str) -> bool:
    if len(text) < _DEGEN_MIN_LEN:
        return False
    tail = text[-_DEGEN_WINDOW:]
    if len(set(tail)) <= _DEGEN_MAX_DISTINCT:
        return True
    non_ascii = sum(1 for ch in tail if ord(ch) > 127)
    return (non_ascii / len(tail)) > _DEGEN_NON_ASCII_RATIO


def _stop_blocked_tool(choice: dict, reason: str) -> bool:
    key = "delta" if "delta" in choice else "message"
    choice[key] = {"role": "assistant", "content": reason + " No command was executed. This turn has stopped; revise the request before continuing."}
    choice["finish_reason"] = "stop"
    return True


def _is_minicpm_47(request_body: dict) -> bool:
    model = str(request_body.get("model") or "").removeprefix("flm/")
    return model == "minicpm-v-4.7:1b"


def _minicpm_direct_answer_turn(body: dict) -> bool:
    """Recognize explicit text-only turns for MiniCPM without changing other FLM models."""
    if not _is_minicpm_47(body) or body.get("tool_choice") not in (None, "auto"):
        return False
    latest = next((str(message.get("content") or "") for message in reversed(body.get("messages") or [])
                   if message.get("role") == "user"), "")
    # Let MiniCPM complete a bounded multi-step task (browser work often needs
    # navigation, waiting, and inspection). After the chat worker's six tool rounds,
    # force its next response to be text so the collected results are summarized.
    messages = body.get("messages") or []
    if messages and messages[-1].get("role") == "tool":
        latest_user = max((index for index, message in enumerate(messages)
                           if message.get("role") == "user"), default=-1)
        completed_tool_rounds = sum(
            1 for message in messages[latest_user + 1:]
            if message.get("role") == "assistant" and message.get("tool_calls")
        )
        return completed_tool_rounds >= 6
    if re.search(r"\banswer directly\b", latest, re.I):
        return True
    stop_patterns = (
        r"\bwithout\s+(?:calling|using|invoking)\s+(?:any\s+)?(?:more\s+)?tools?\b",
        r"\bdo not\s+(?:call|use|invoke)\s+(?:any\s+)?(?:more\s+)?tools?\b",
        r"\bdo not ask again\b",
        r"\b(?:permission|installation)\s+(?:was\s+)?denied\b",
        r"\bdo not retry\b.{0,100}\b(?:install|installation|package)\b",
    )
    return any(re.search(pattern, latest, re.I) for pattern in stop_patterns)


def _minicpm_named_request(body: dict):
    """Read an explicit MiniCPM named-tool request, including the checkpoint shorthand."""
    contract = exact_tool_contract._request(body)
    if contract is not None:
        return contract
    choice = body.get("tool_choice")
    fn = choice.get("function") if isinstance(choice, dict) else None
    name = fn.get("name") if isinstance(fn, dict) else None
    if choice.get("type") != "function" or name not in ("bash", "shell"):
        return None
    user = next((str(message.get("content") or "") for message in reversed(body.get("messages") or [])
                 if message.get("role") == "user"), "")
    match = re.search(r"\bNext call\s+(?:bash|shell)\s+with command\s+(.+?)\s+and workdir\s+([^\s.]+)\s*\.?$",
                      user, re.I | re.S)
    if not match:
        return None
    return name, {"command": match.group(1).strip(), "workdir": match.group(2)}, \
        next((tool.get("function", {}).get("parameters") or {} for tool in body.get("tools") or []
              if (tool.get("function") or {}).get("name") == name), {})


def _minicpm_parallel_arguments(body: dict):
    """Recover two literal values only when MiniCPM's request explicitly names both."""
    if (not _is_minicpm_47(body) or body.get("parallel_tool_calls") is not True
            or body.get("tool_choice") not in (None, "auto", "required")):
        return None
    tools = body.get("tools") or []
    if len(tools) != 1:
        return None
    function = tools[0].get("function") or {}
    name = function.get("name")
    schema = function.get("parameters") or {}
    required = schema.get("required") or []
    properties = schema.get("properties") or {}
    if len(required) != 1 or len(properties) != 1 or required[0] not in properties \
            or properties[required[0]].get("type") != "string":
        return None
    user = next((str(message.get("content") or "") for message in reversed(body.get("messages") or [])
                 if message.get("role") == "user"), "")
    match = re.search(rf"\bTWO\s+{re.escape(str(name))}\s+tool calls?\s*:\s*([^,.\n]+?)\s+first,\s*then\s+([^.\n]+)",
                      user, re.I)
    if not match:
        return None
    values = [match.group(1).strip(), match.group(2).strip()]
    if not all(values):
        return None
    return name, required[0], values


def _enforce_minicpm_named_call(obj: dict, request_body: dict) -> bool:
    """Honor an exact named choice for MiniCPM, using only arguments supplied by the user."""
    if not _is_minicpm_47(request_body):
        return False
    choice = request_body.get("tool_choice")
    function = choice.get("function") if isinstance(choice, dict) else None
    name = function.get("name") if isinstance(function, dict) else None
    if not isinstance(name, str) or choice.get("type") != "function":
        return False
    contract = _minicpm_named_request(request_body)
    if contract is None or contract[0] != name:
        return False
    _requested_name, arguments, schema = contract
    if not isinstance(arguments, dict) or not set(schema.get("required") or []).issubset(arguments):
        return False
    try:
        result = obj["choices"][0]
    except (KeyError, IndexError, TypeError):
        return False
    key = "delta" if "delta" in result else "message"
    message = result.setdefault(key, {})
    calls = message.get("tool_calls") or []
    call = calls[0] if calls else {"id": f"call_hearth_{time.time_ns()}", "type": "function"}
    desired_args = json.dumps(arguments, separators=(",", ":"))
    fn = call.setdefault("function", {})
    changed = (len(calls) != 1 or fn.get("name") != name or fn.get("arguments") != desired_args)
    fn.update(name=name, arguments=desired_args)
    call.setdefault("id", f"call_hearth_{time.time_ns()}")
    call.setdefault("type", "function")
    message["tool_calls"] = [call]
    if changed:
        message["content"] = None
        result["finish_reason"] = "tool_calls"
    return changed


def _repair_minicpm_parallel_calls(obj: dict, request_body: dict) -> bool:
    if not _is_minicpm_47(request_body):
        return False
    contract = _minicpm_parallel_arguments(request_body)
    if contract is None:
        return False
    name, argument_name, values = contract
    try:
        result = obj["choices"][0]
    except (KeyError, IndexError, TypeError):
        return False
    key = "delta" if "delta" in result else "message"
    message = result.setdefault(key, {})
    existing = list(message.get("tool_calls") or [])
    calls = existing[:2]
    changed = len(existing) != 2
    while len(calls) < 2:
        calls.append({"id": f"call_hearth_{time.time_ns()}_{len(calls)}", "type": "function"})
    for index, (call, value) in enumerate(zip(calls, values)):
        function = call.setdefault("function", {})
        arguments = json.dumps({argument_name: value}, separators=(",", ":"))
        if function.get("name") != name or function.get("arguments") != arguments:
            changed = True
        function.update(name=name, arguments=arguments)
        call.setdefault("id", f"call_hearth_{time.time_ns()}_{index}")
        call.setdefault("type", "function")
    message["tool_calls"] = calls
    if changed:
        message["content"] = None
        result["finish_reason"] = "tool_calls"
    return changed


def _repair_tool_arguments(obj: dict, request_body: dict) -> bool:
    """Normalize aliases and neutralize malformed file operations before OpenCode sees them."""
    try:
        choice = obj["choices"][0]
        calls = (choice.get("delta") or choice.get("message") or {}).get("tool_calls") or []
    except (KeyError, IndexError, TypeError):
        return False
    if request_body.get("tool_choice") == "none" and calls:
        key = "delta" if "delta" in choice else "message"
        message = {k: v for k, v in choice[key].items() if k != "tool_calls"}
        message["content"] = message.get("content") or "Tool calling is disabled for this request."
        choice[key] = message
        choice["finish_reason"] = "stop"
        return True
    named_choice_repaired = _enforce_minicpm_named_call(obj, request_body)
    if named_choice_repaired:
        calls = (choice.get("delta") or choice.get("message") or {}).get("tool_calls") or []
    parallel_repaired = _repair_minicpm_parallel_calls(obj, request_body)
    if parallel_repaired:
        calls = (choice.get("delta") or choice.get("message") or {}).get("tool_calls") or []
    available = {
        ((tool.get("function") or {}).get("name") or "").lower()
        for tool in (request_body.get("tools") or [])
    }
    schemas = {
        str((tool.get("function") or {}).get("name") or "").lower():
            (tool.get("function") or {}).get("parameters") or {}
        for tool in request_body.get("tools") or []
    }
    recent_history = "\n".join(
        str(message.get("content") or "")
        for message in (request_body.get("messages") or [])[-12:]
    )
    repeated_replace_failure = len(re.findall(
        r"(?:replace_in_file|SEARCH block|exact match).{0,180}"
        r"(?:fail|not found|did not match|exact matching)",
        recent_history, re.I | re.S,
    )) >= 2 or bool(re.search(
        r"failed twice.{0,300}(?:write_to_file|write to file)",
        recent_history, re.I | re.S,
    ))
    user_requested_install = any(
        isinstance(message.get("content"), str)
        and re.search(r"\b(?:install|add dependency|add package)\b", message["content"], re.I)
        for message in (request_body.get("messages") or [])
        if message.get("role") == "user"
    )
    repaired = exact_tool_contract.repair_completion(obj, request_body) or named_choice_repaired or parallel_repaired
    for call in calls:
        function = call.get("function") or {}
        original_name = function.get("name")
        name = str(original_name or "").strip().strip("\"'").lower()
        if name != original_name and name in available:
            function["name"] = name
            repaired = True
        if name in ("execute", "bash", "shell") and name not in available:
            target = next((candidate for candidate in ("shell", "bash") if candidate in available), None)
            if target:
                name = target
                function["name"] = target
                repaired = True
        tool_kind = (
            "edit" if name in ("edit", "replace_in_file", "replace") else
            "write" if name in ("write", "write_to_file", "create_file") else
            "read" if name in ("read", "read_file") else
            "patch" if name in ("patch", "apply_patch") else
            name
        )
        raw = function.get("arguments")
        try:
            args = tool_argument_json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(args, dict):
            continue

        properties = schemas.get(name, {}).get("properties") or {}
        if tool_kind == "edit":
            for canonical, alias in (("oldString", "old_string"), ("newString", "new_string"),
                                     ("replaceAll", "replace_all")):
                if canonical in properties and canonical not in args and alias in args:
                    args[canonical] = args.pop(alias)
                    repaired = True
        path_key = next((key for key in ("path", "filePath", "file_path", "filepath", "filename", "file")
                         if key in properties), None)
        # Older clients omit schemas in some requests; preserve their existing spelling.
        if not properties:
            path_key = next((key for key in ("filePath", "path", "file_path", "filepath", "filename", "file")
                             if key in args), "filePath")
        # Patch tools embed file paths inside patchText; do not invent a path argument.
        if tool_kind in ("read", "write", "edit", "patch") and path_key:
            if path_key not in args:
                for alias in ("path", "filePath", "file_path", "filepath", "filename", "file"):
                    if isinstance(args.get(alias), str) and args[alias]:
                        args[path_key] = args.pop(alias)
                        repaired = True
                        break
            path = args.get(path_key)
            malformed_path = bool(
                not isinstance(path, str)
                or not path.strip()
                or "\n" in path
                or len(path) > 1000
                or re.search(r"[\"'](?:,|>)", path)
            )
            missing_content = tool_kind == "write" and not isinstance(args.get("content"), str)
            if malformed_path or missing_content:
                reason = (
                    f"Hearth blocked malformed {name} arguments. Retry {name} with {path_key} "
                    "containing only the absolute path and content in its separate parameter."
                )
                return _stop_blocked_tool(choice, reason)

            if tool_kind == "write" and repeated_replace_failure:
                reason = (
                    "Hearth blocked a whole-file write fallback after repeated exact-match "
                    "failures. Re-read the existing file, then use a smaller exact replacement "
                    "or structured patch; preserve unrelated content."
                )
                return _stop_blocked_tool(choice, reason)

        if name in ("bash", "shell"):
            if "command" not in args:
                for alias in ("cmd", "script", "input"):
                    if isinstance(args.get(alias), str) and args[alias]:
                        args["command"] = args.pop(alias)
                        repaired = True
                        break
            command = args.get("command")
            has_file_tool = any(
                token in tool_name for tool_name in available
                for token in ("write", "edit", "patch")
            )
            shell_file_write = bool(
                isinstance(command, str) and has_file_tool and (
                    re.search(r"(?:^|[;&|]\s*)cat\s+.*(?:>|>>|<<)", command)
                    or re.search(r"\bpython(?:3)?\s+-c\b.*\b(?:write|write_text)\b", command, re.S)
                    or re.search(r"\bsed\s+-i\b|(?:^|[^<])>{1,2}\s*\S+", command)
                )
            )
            unsolicited_install = bool(
                isinstance(command, str)
                and not user_requested_install
                and re.search(r"\b(?:pip|pip3)\s+install\b|playwright\s+install\b", command)
            )
            if shell_file_write or unsolicited_install:
                reason = (
                    "Hearth blocked an unsafe shell file-write/package-install command. "
                    "Use the structured write/edit tool, or report a missing dependency."
                )
                return _stop_blocked_tool(choice, reason)

        args = _restore_schema_types(args, schemas.get(name, {}))
        if name == "question":
            args = question_tool_compat.normalize(args, schemas.get(name, {}))
        serialized = json.dumps(args, separators=(",", ":"))
        if serialized != raw and function.get("name") == name:
            function["arguments"] = serialized
            repaired = True
    return exact_file_content.repair_completion(obj, request_body) or repaired


def _abort_chunk(reason: str, model: str | None) -> bytes:
    """A synthetic, well-formed closing SSE event: finish_reason 'stop' (never 'tool_calls'),
    so a client that only acts on tool calls when finish_reason is exactly 'tool_calls' (OpenCode
    included) just sees an odd/truncated text reply instead of trying to execute a tool call
    whose arguments were cut off mid-garbage.

    Real incident (2026-07-26): this previously hardcoded `model` to one specific tag and
    omitted `created` entirely. A real OpenCode session that hit this abort path terminated
    with a client-side error anyway despite the graceful `finish_reason: "stop"` above — the
    likely cause is the Vercel AI SDK's openai-compatible provider (what OpenCode's config uses)
    validates each streamed chunk against a schema that expects a real `created` timestamp, and
    a `model` field that at least looks like the one it requested; a chunk missing/mismatching
    those can fail that validation and throw client-side instead of just rendering as odd text.
    Passing the request's own model id and a real timestamp costs nothing and removes that as a
    possible cause."""
    chunk = {
        "id": "chatcmpl-hearth-shim-abort",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model or "unknown",
        "choices": [{"index": 0, "delta": {"content": f"\n\n[Hearth shim: {reason}]"}, "finish_reason": "stop"}],
    }
    return f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()


def _split_live_reasoning(obj: dict) -> tuple[dict | None, dict]:
    """Stream reasoning now while keeping content/tool calls for validation.

    Remove reasoning from the buffered event so replay cannot display it twice.
    Keep finish reasons and usage on the buffered event.
    """
    choices = obj.get("choices") or []
    if not choices or not (choices[0].get("delta") or {}).get("reasoning_content"):
        return None, obj
    choice = choices[0]
    delta = choice["delta"]
    live = {k: v for k, v in obj.items() if k not in {"choices", "usage"}}
    live["choices"] = [{**choice, "delta": {
        "reasoning_content": delta["reasoning_content"],
    }, "finish_reason": None}]
    buffered_delta = {k: v for k, v in delta.items() if k != "reasoning_content"}
    buffered = {**obj, "choices": [{**choice, "delta": buffered_delta}, *choices[1:]]}
    return live, buffered


def _ensure_no_thinking(body: dict) -> dict:
    """qwen3.6-moe:35b-a3b is a 'thinking' model — FLM's server (mirroring the model's own
    chat_template.jinja) forces every assistant turn to open with a `<think>\\n` block by
    default. Confirmed live (2026-07-25) via FLM's own request/response logs on a real OpenCode
    session: on a tool-result-summarization turn deep in a multi-turn tool-calling conversation,
    the model produced a good, complete answer, then apparently got confused about where its own
    `</think>` closing marker belonged — emitting it belatedly, mid-response, followed by garbage
    tokens (`__`, `—`) — which is what showed up in OpenCode as a truncated/corrupted reply
    (finish_reason was 'stop', not 'length': this was the model breaking format, not hitting a
    token cap). FLM honors the OpenAI/Qwen convention `enable_thinking: false`, which the model's
    own template maps to a pre-closed empty think block (`<think>\\n\\n</think>\\n\\n`) instead of
    an open one — confirmed live to produce a clean, direct answer with no `<think>` framing
    leaking into content at all. OpenCode's tool-result summaries don't need a visible reasoning
    trace anyway (it's an agent loop, not a chat needing shown work), so defaulting this off
    sidesteps the whole failure class rather than trying to strip corrupted output after the fact.
    Only fills it in if the caller didn't already set a preference."""
    model = str(body.get("model") or "").removeprefix("flm/")
    result = body if "enable_thinking" in body else {**body, "enable_thinking": model == "minicpm-v-4.7:1b"}
    # The older Qwen3 runtime ignores this template kwarg; its tokenizer template
    # and model also support the explicit user-message mode marker.
    if model.startswith("qwen3:") and result.get("enable_thinking") is False:
        messages = list(result.get("messages") or [])
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if message.get("role") == "user" and isinstance(message.get("content"), str):
                if not message["content"].rstrip().endswith("/no_think"):
                    messages[index] = {**message, "content": message["content"] + "\n/no_think"}
                    result = {**result, "messages": messages}
                break
    return result


_HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "content-encoding", "content-length"}
_ID_PREFIX = "flm/"


def _forward_headers(headers) -> dict:
    # Hearth uses this label for local metrics attribution only. Do not expose it
    # to the model server, which may reject or interpret unknown request headers.
    return {k: v for k, v in headers.items()
            if k.lower() not in _HOP_BY_HOP
            and k.lower() not in {"host", "x-hearth-agent-name", "x-hearth-agent-relay"}}


def _source_from_headers(headers) -> str:
    """Same X-Hearth-Client convention caddy_metrics.py uses for the historical metrics
    rows - see help_data.build_continue_yaml/build_opencode_config for where this header
    gets set. Falls back to "lan" for anything else (a manual curl test, etc.)."""
    agent_name = (headers.get("X-Hearth-Agent-Name") or headers.get("x-hearth-agent-name") or "").strip()
    if agent_name and len(agent_name) <= 120 and not any(ord(char) < 32 for char in agent_name):
        return agent_name
    client = (headers.get("X-Hearth-Client") or "").lower()
    return client if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", client) else "lan"


def _record_chat_metric(*, started: float, source: str, model: str, status: int,
                        usage: dict | None, client_ip: str | None) -> None:
    usage = usage or {}
    duration_ms = round((time.monotonic() - started) * 1000, 1)
    completion = usage.get("completion_tokens")
    tps = usage.get("decoding_speed_tps")
    if tps is None and isinstance(completion, (int, float)) and duration_ms > 0:
        tps = round(completion / (duration_ms / 1000), 3)
    live_report.record_metric(
        source=source, backend="fastflowlm", model=model,
        request_type="chat_completion", ask_category="chat",
        duration_ms=duration_ms, prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=completion, total_tokens=usage.get("total_tokens"),
        tokens_per_sec=tps, status=status, client_ip=client_ip, is_npu=True,
    )


def _strip_prefix(body: dict) -> dict:
    """OpenCode's generated config (help_data.build_opencode_config) sends this app's own
    'flm/<tag>' namespaced id (see fastflowlm_client.py's module docstring — the prefix
    disambiguates FLM tags from Lemonade's overlapping ones) as the literal `model` field.
    FastFlowLM's own API expects the bare tag. Confirmed live (2026-07-25): sending the
    prefixed id through unchanged doesn't error, but produces badly malformed tool-call XML
    (missing closing tags, spurious fields) where the bare tag reliably produces correct
    output — so this must be stripped before forwarding, same as fastflowlm_client._tag()
    already does for this app's own (non-shimmed) calls to FLM."""
    model = body.get("model")
    if isinstance(model, str) and model.startswith(_ID_PREFIX):
        return {**body, "model": model[len(_ID_PREFIX):]}
    return body


def _request_trace(body: dict) -> str:
    """Readable prompt trace without credentials or the often-huge tool schemas."""
    rows = []
    for message in body.get("messages") or []:
        role = str(message.get("role") or "unknown")
        content = message.get("content")
        if content:
            rows.append(f"{role}: {content}")
        if message.get("tool_calls"):
            rows.append(f"{role}: [tool calls: {len(message['tool_calls'])}]")
    return "\n".join(rows)[-4000:]


@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    started = time.monotonic()
    if fastflowlm.requests_paused():
        return {"error": {"message": "FastFlowLM was manually unloaded. Load a model in Hearth's Models panel to resume API requests.",
                          "type": "backend_unavailable", "code": "model_manually_unloaded"}}, 503
    body = request.get_json(force=True, silent=True) or {}
    if body.get("stream"):
        body["stream_options"] = {**(body.get("stream_options") or {}), "include_usage": True}
    body = model_test_profiles.apply(body, "fastflowlm")
    # Break sessions already cycling on the former synthetic printf tool result.
    for message in reversed(body.get("messages") or []):
        if message.get("role") == "user":
            break
        if message.get("role") == "tool" and "Hearth blocked" in str(message.get("content") or ""):
            choice = {"index": 0, "message": {}}
            _stop_blocked_tool(choice, "Hearth stopped a repeated blocked-tool loop.")
            event = {"id": "hearth-blocked-tool", "object": "chat.completion",
                     "created": int(time.time()), "model": body.get("model"), "choices": [choice]}
            if body.get("stream"):
                choice["delta"] = choice.pop("message")
                event["object"] = "chat.completion.chunk"
                return Response(b"data: " + json.dumps(event).encode() + b"\n\ndata: [DONE]\n\n",
                                content_type="text/event-stream")
            return event
    body = _ensure_no_thinking(_ensure_frequency_penalty(_strip_prefix(body)))
    body = agent_request_policy.prepare(body, compact_history=False, qwen_penalty=False)
    if _minicpm_direct_answer_turn(body):
        body = {**body, "tool_choice": "none"}
    body, context_error = flm_context.prepare(body)
    if context_error:
        return context_error, 400
    body = _inject_tools_block(body)
    streamed = bool(body.get("stream"))
    recovery_headers = _forward_headers(request.headers)
    source = _source_from_headers(request.headers)
    model = body.get("model") or "unknown"
    cross_npu_lock = npu_request_lock.acquire()
    if fastflowlm.requests_paused():
        npu_request_lock.release(cross_npu_lock)
        return {"error": {"message": "FastFlowLM was manually unloaded. Load a model in Hearth's Models panel to resume API requests.",
                          "type": "backend_unavailable", "code": "model_manually_unloaded"}}, 503
    _free_lemonade_npu()
    try:
        _ensure_fastflow_ready(model)
    except fastflowlm.FastFlowLMError as exc:
        npu_request_lock.release(cross_npu_lock)
        return {
            "error": {
                "message": f"FastFlowLM could not start: {exc}",
                "type": "backend_unavailable",
            }
        }, 503
    forwarded = request.headers.get("X-Forwarded-For") or ""
    client_ip = forwarded.split(",", 1)[0].strip() or request.remote_addr
    # Reports this request into the metrics page's live-activity feed (see live_report.py's
    # docstring) - this shim is the only thing inline for real OpenCode/Continue FastFlowLM
    # traffic, so it's the only place that can see it streaming in real time. is_npu=True
    # unconditionally: FastFlowLM is the standalone NPU runtime, not a multi-recipe server
    # like Lemonade - every request that reaches it runs on the NPU, full stop.
    live_id = None if request.headers.get("X-Hearth-Agent-Relay") == "1" else live_report.start(
        source=source, backend="fastflowlm",
        model=model, max_tokens=body.get("max_tokens"), is_npu=True,
        request_content=_request_trace(body),
    )
    try:
        upstream = requests.post(
            f"{FLM_BASE}/v1/chat/completions",
            json={**body, "tools": []} if body.get("tool_choice") == "none" else body,
            headers=_forward_headers(request.headers), stream=streamed, timeout=(10, 900),
        )
    except requests.RequestException:
        # Without this, a dead/unreachable flm.service (e.g. mid-restart after an eviction)
        # would leave this request stuck showing "streaming" in the live feed forever -
        # finish() is only ever called below, which never runs if this raises.
        live_report.finish(live_id, status="error")
        npu_request_lock.release(cross_npu_lock)
        raise
    if upstream.status_code == 400 and 'max length reached' in upstream.text.lower():
        upstream.close()
        live_report.finish(live_id, status="error", status_code=400)
        npu_request_lock.release(cross_npu_lock)
        return flm_context.overflow_error(), 400
    if streamed:
        watch_degeneracy = bool(body.get("tools"))
        buffer_tool_output = any((tool.get("function") or {}).get("name")
                                 for tool in body.get("tools") or [])

        def tee():
            chunks = []
            text_seen = []
            aborted_reason = None
            usage = {}
            pending_chars = 0
            pending_content = []
            reported_finish = False
            try:
                for raw_line in upstream.iter_lines(chunk_size=1):
                    if raw_line is None:
                        continue
                    if not raw_line.startswith(b"data: "):
                        out_line = raw_line + b"\n" if raw_line else b"\n"
                        chunks.append(out_line)
                        if not buffer_tool_output:
                            yield out_line
                        continue
                    payload = raw_line[len(b"data: "):].strip()
                    if payload == b"[DONE]":
                        out_line = raw_line + b"\n"
                        chunks.append(out_line)
                        if not buffer_tool_output:
                            yield out_line
                        continue
                    try:
                        obj = json.loads(payload)
                    except (TypeError, json.JSONDecodeError):
                        out_line = raw_line + b"\n"
                        chunks.append(out_line)
                        if not buffer_tool_output:
                            yield out_line
                        continue
                    if obj.get("usage"):
                        # OpenAI-compatible streaming sends usage as a final event with
                        # choices=[]; capture it before requiring a content delta.
                        usage = obj["usage"]
                    try:
                        delta = obj["choices"][0]["delta"]
                    except (KeyError, IndexError, TypeError):
                        out_line = raw_line + b"\n"
                        chunks.append(out_line)
                        if not buffer_tool_output:
                            yield out_line
                        continue
                    if buffer_tool_output:
                        live_reasoning, obj = _split_live_reasoning(obj)
                        if live_reasoning is not None:
                            yield b"data: " + json.dumps(live_reasoning).encode() + b"\n\n"
                            raw_line = b"data: " + json.dumps(obj).encode()
                    out_line = raw_line + b"\n"
                    chunks.append(out_line)
                    if not buffer_tool_output:
                        yield out_line
                    content_piece = delta.get("content") or ""
                    if content_piece:
                        text_seen.append(content_piece)
                        pending_chars += len(content_piece)
                        pending_content.append(content_piece)
                    for tc in delta.get("tool_calls") or []:
                        args = (tc.get("function") or {}).get("arguments")
                        if args:
                            text_seen.append(args)
                            pending_chars += len(args)
                            pending_content.append(args)
                    # Throttled: one HTTP round-trip per ~20 characters, not per SSE chunk -
                    # FastFlowLM can stream many small chunks/sec, and the live feed only
                    # needs to look like it's moving, not track every single token.
                    if pending_chars >= 20:
                        live_report.update(
                            live_id, delta_chars=pending_chars,
                            delta_content="".join(pending_content),
                        )
                        pending_chars = 0
                        pending_content = []
                    if watch_degeneracy and _looks_degenerate("".join(text_seen)):
                        aborted_reason = "generation aborted — repetition-collapse detected"
                        break
                if pending_chars:
                    live_report.update(
                        live_id, delta_chars=pending_chars,
                        delta_content="".join(pending_content),
                    )
                if aborted_reason:
                    try:
                        upstream.close()
                    except Exception:
                        pass
                    abort_bytes = _abort_chunk(aborted_reason, body.get("model"))
                    chunks.append(abort_bytes)
                    if not buffer_tool_output:
                        yield abort_bytes
                if buffer_tool_output:
                    recovered = None
                    if (not aborted_reason and upstream.ok and body.get('tool_choice') != 'none'
                            and _native_tool_parser_failed(chunks)):
                        upstream.close()
                        recovered = _recover_native_tool_parser(body, recovery_headers)
                    if recovered is not None:
                        wire, retry_usage = recovered
                        for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
                            if key in usage and key in retry_usage:
                                retry_usage[key] += usage[key]
                        usage = retry_usage
                        chunks = [wire]
                        repaired_stream = wire
                    else:
                        repaired_stream = _guard_streamed_tool_calls(chunks, body)
                    if repaired_stream is None:
                        repaired_stream = _repair_streamed_textual_tool_calls(chunks, body)
                    if repaired_stream is not None:
                        yield repaired_stream
                    else:
                        for buffered_line in chunks:
                            yield buffered_line
                live_report.finish(
                    live_id, status="error" if aborted_reason else "done",
                    total_tokens=usage.get("completion_tokens"), status_code=upstream.status_code,
                    error_detail=aborted_reason,
                )
                _record_chat_metric(
                    started=started, source=source, model=model,
                    status=upstream.status_code, usage=usage, client_ip=client_ip,
                )
                reported_finish = True
                raw = b"".join(chunks)
                capture_client = source in {"vscode-cline", "opencode"} and bool(body.get("tools"))
                if b"tool_calls" in raw or b"<tool_call" in raw or aborted_reason or capture_client:
                    _log_tool_call_capture(
                        f"REQUEST [{source}]" if capture_client else "REQUEST",
                        json.dumps({"model": body.get("model"), "tools": body.get("tools")}).encode(),
                        force=capture_client or bool(aborted_reason),
                    )
                    _log_tool_call_capture(
                        "RESPONSE (stream, ABORTED)" if aborted_reason else "RESPONSE (stream)",
                        raw,
                        force=capture_client or bool(aborted_reason),
                    )
            finally:
                # Guarantees the live-feed entry never gets stuck showing "streaming" forever
                # if the client disconnects mid-stream or upstream drops the connection - both
                # abort this generator via GeneratorExit/an exception before the normal-path
                # finish() call above ever runs. Idempotent (live_requests.finish() no-ops on
                # an already-finished id), so this is a no-op in the normal-completion case.
                if not reported_finish:
                    try:
                        upstream.close()
                    except Exception:
                        pass
                    live_report.finish(live_id, status="error", total_tokens=usage.get("completion_tokens"))
                npu_request_lock.release(cross_npu_lock)
        response = Response(
            stream_with_context(tee()),
            status=upstream.status_code,
            content_type=upstream.headers.get("content-type", "text/event-stream"),
        )
        response.headers["X-Hearth-Metrics-Recorded"] = "1"
        return response
    usage = {}
    try:
        upstream_json = upstream.json()
        response_repaired = _repair_tool_arguments(upstream_json, body)
        choice = (upstream_json.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        if body.get("tools") and not message.get("tool_calls"):
            clean_content, repaired_calls = _repair_textual_tool_calls(
                message.get("content") or "", body
            )
            if repaired_calls:
                message["content"] = clean_content or None
                message["tool_calls"] = repaired_calls
                choice["finish_reason"] = "tool_calls"
                _repair_tool_arguments(upstream_json, body)
                response_repaired = True
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
        live_id, status="done" if upstream.ok else "error",
        total_tokens=usage.get("completion_tokens"), status_code=upstream.status_code,
    )
    _record_chat_metric(
        started=started, source=source, model=model,
        status=upstream.status_code, usage=usage, client_ip=client_ip,
    )
    capture_cline = source == "vscode-cline" and bool(body.get("tools"))
    if b"tool_calls" in upstream.content or capture_cline:
        _log_tool_call_capture(
            "REQUEST [vscode-cline]" if capture_cline else "REQUEST",
            json.dumps({"model": body.get("model"), "tools": body.get("tools")}).encode(),
            force=capture_cline,
        )
        _log_tool_call_capture("RESPONSE", upstream.content, force=capture_cline)
    response_content = (
        json.dumps(upstream_json, separators=(",", ":")).encode()
        if "upstream_json" in locals() and response_repaired
        else upstream.content
    )
    response = Response(
        response_content, status=upstream.status_code,
        content_type=upstream.headers.get("content-type", "application/json"),
    )
    response.headers["X-Hearth-Metrics-Recorded"] = "1"
    npu_request_lock.release(cross_npu_lock)
    return response


@app.route("/<path:path>", methods=["GET", "POST"])
def passthrough(path):
    """Everything else (/v1/models, etc.) — unmodified reverse proxy."""
    if path.rstrip("/") == "v1/models":
        try:
            fastflowlm.ensure_service()
        except fastflowlm.FastFlowLMError as exc:
            return {
                "error": {
                    "message": f"FastFlowLM could not start: {exc}",
                    "type": "backend_unavailable",
                }
            }, 503
    upstream = requests.request(
        request.method, f"{FLM_BASE}/{path}", params=request.args,
        data=request.get_data(), headers=_forward_headers(request.headers), timeout=30,
    )
    return Response(
        upstream.content, status=upstream.status_code,
        content_type=upstream.headers.get("content-type", "application/octet-stream"),
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=LISTEN_PORT, threaded=True)
