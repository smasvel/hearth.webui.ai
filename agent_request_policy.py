"""Shared request policy for local coding-model tool calls."""

_AGENT_RELIABILITY_INSTRUCTION = """
<agent_reliability>
The latest user message defines the scope of this response. Perform only its requested
step. If it asks for a text reply without tools, give that reply and end the turn, even
when older project work remains unfinished. Do not resume older work in that response.
You are in an agent tool loop. When you say you will inspect, read, run, edit, check, or
verify something, emit the matching tool call in that same response; never end a response
with narration such as "Let me check" or "I will update it". Continue using tools until the
requested coding task is actually complete. After a write reports success, read the exact
absolute target path and verify the new content. If verification disagrees, do not repeat the
same write unchanged: inspect path/symlink/cwd details and use a different corrective action.
For an open-ended coding task, return a prose response when the task is complete or a
concrete blocker requires user input. A requested text-only reply also completes the turn.
The latest user instruction controls this turn: if it asks for a reply without tools,
or to stop after a dismissed question, give that reply and stop. Do not resume older
project work in that response. A dismissed optional question must not be asked again.
When the latest instruction specifies the next tool call, perform only that step.
Do not skip a requested read to edit a file even when the fix seems obvious. Do not
add questions, options, tool calls or tasks that were not requested in that step.
When explicit tool arguments are supplied, preserve string contents byte for byte,
including trailing newlines. Copy arrays element by element in their supplied order,
including question and option arrays. Never alphabetize, sort, rewrite, add, or omit
choices. When asked for multiple independent tool calls in one
response, emit all of them before waiting for results, in the requested order.
For the bash tool, the JSON arguments MUST contain the required key "command". Never use
"cmd", "script", or an empty object in place of "command".
Use the path key declared by the offered tool schema, such as "filePath" or "path".
For file tools, a write call must contain the schema's path key and "content".
An edit call must contain the schema's path key, "oldString", and "newString".
Use "filePath" only when the offered schema declares "filePath"; when it declares
"path", use "path" instead. Never append quote characters to JSON property names or path
values, and never put a path inside a property name.
When a dedicated write/edit/patch tool is available, NEVER write or rewrite a file through
bash, python -c, a heredoc, printf redirection, sed -i, or shell redirection. Use the
structured file tool so quoting cannot corrupt the requested content.
`write_to_file`/`write` is ONLY for creating a new file that is known not to exist. Never
use it to replace an existing file after `replace_in_file`/`edit` fails. Ignore any generic
client suggestion to fall back to a whole-file write after repeated exact-match failures.
Instead, re-read the target, copy a smaller exact current block, and retry replace/edit once;
if that remains unsuitable, use the structured patch tool. Preserve all unrelated content.
</agent_reliability>
""".strip()

_TOOL_HISTORY_MAX_CHARS = 6000

_TOOL_HISTORY_HEAD_CHARS = 1500

_TOOL_HISTORY_TAIL_CHARS = 3500

def prepare(body: dict, *, compact_history=True, qwen_penalty=True) -> dict:
    """Harden tools-bearing Qwen requests against a client-ending narration-only turn."""
    if not body.get("tools") or body.get("tool_choice") == "none":
        return body
    prepared = dict(body)
    messages = [dict(message) for message in (body.get("messages") or [])]
    # OpenCode resends the complete agent history on every turn. Raw HTML/command output can
    # be hundreds of KB; one real DuckDuckGo debugging session consequently spent >300s in
    # NPU prefill and the client disconnected before the first token. Preserve the useful
    # beginning and most-recent tail of oversized tool results, but do not repeatedly prefill
    # an entire webpage that the agent has already inspected.
    for message in messages if compact_history else []:
        if message.get("role") != "tool" or not isinstance(message.get("content"), str):
            continue
        content = message["content"]
        if len(content) <= _TOOL_HISTORY_MAX_CHARS:
            continue
        omitted = len(content) - _TOOL_HISTORY_HEAD_CHARS - _TOOL_HISTORY_TAIL_CHARS
        message["content"] = (
            content[:_TOOL_HISTORY_HEAD_CHARS]
            + f"\n\n[Hearth compacted {omitted:,} characters of historical tool output]\n\n"
            + content[-_TOOL_HISTORY_TAIL_CHARS:]
        )
    if messages and messages[0].get("role") in ("system", "developer"):
        existing = messages[0].get("content") or ""
        messages[0]["content"] = f"{existing}\n\n{_AGENT_RELIABILITY_INSTRUCTION}"
    else:
        messages.insert(0, {"role": "system", "content": _AGENT_RELIABILITY_INSTRUCTION})
    prepared["messages"] = messages
    # Direct answers are more reliable for tool JSON than a long hidden reasoning pass, so the
    # default stays off. An explicit caller preference must be honored, though - and it has to
    # be kept in sync across BOTH spellings. OpenCode's generated config sends
    # chat_template_kwargs.enable_thinking=true (when the admin enabled Lemonade thinking) so
    # the client can display the model's reasoning. Forcing the separate top-level field to
    # False silently overrode that for templates that read the top-level value (confirmed live
    # with GLM-4.7-Flash): thinking was disabled and OpenCode saw no reasoning_content at all,
    # even though the request asked for it. Precedence: explicit top-level, else explicit
    # chat_template_kwargs, else the reliability default.
    template_kwargs = dict(prepared.get("chat_template_kwargs") or {})
    if "enable_thinking" in prepared:
        thinking = prepared["enable_thinking"]
    elif "enable_thinking" in template_kwargs:
        thinking = template_kwargs["enable_thinking"]
    else:
        thinking = False
    prepared["enable_thinking"] = thinking
    template_kwargs["enable_thinking"] = thinking
    prepared["chat_template_kwargs"] = template_kwargs
    # This penalty addressed Qwen repetition loops. Applying it to every model
    # discourages legitimate repeated tokens when copying source code verbatim.
    if qwen_penalty and 'qwen' in str(prepared.get('model', '')).lower():
        prepared.setdefault("frequency_penalty", 0.4)
    return prepared
