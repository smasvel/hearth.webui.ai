"""Preserve explicitly supplied write bytes when a model changes empty lines.

This reads only the latest user instruction, never test expectations, assistant
messages or tool results. It neither guesses file contents nor executes tools.
"""
import json
import re

_EXACT_WRITE = re.compile(r'\b(?:next\s+)?call\s+(write|write_to_file)\s+with\s+exactly(?:\s+these\s+arguments)?\s*:\s*', re.I)


def _contract(body):
    message = next((m for m in reversed(body.get('messages') or []) if m.get('role') == 'user'), {})
    content = message.get('content')
    if not isinstance(content, str):
        return None
    match = _EXACT_WRITE.match(content.strip())
    if not match:
        return None
    try:
        args, end = json.JSONDecoder().raw_decode(content.strip()[match.end():])
    except ValueError:
        return None
    # A second instruction or JSON block makes the request ambiguous.
    if content.strip()[match.end() + end:].strip() not in ('', '.'):
        return None
    name = match.group(1).lower()
    schema = next((t.get('function', {}).get('parameters', {}) for t in body.get('tools') or []
                   if t.get('function', {}).get('name') == name), {})
    properties = schema.get('properties', {}) if isinstance(schema, dict) else {}
    if (not isinstance(args, dict) or not isinstance(args.get('content'), str)
            or not isinstance(args.get('filePath'), str)
            or set(args) != {'filePath', 'content'}
            or any(not isinstance(properties.get(k), dict) or properties[k].get('type') != 'string'
                   for k in args)):
        return None
    return name, args


def _restore(fn, contract):
    if contract is None or fn.get('name') != contract[0]:
        return False
    raw = fn.get('arguments')
    try:
        args = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return False
    supplied = contract[1]
    if (not isinstance(args, dict) or set(args) != set(supplied)
            or args.get('filePath') != supplied['filePath']
            or not isinstance(args.get('content'), str)):
        return False
    actual, wanted = args['content'], supplied['content']
    # Preserve indentation, spaces within lines, string literals, CRs, and code.
    # Only genuinely empty LF-delimited lines (including the final LF) may differ.
    if actual == wanted or [s for s in actual.split('\n') if s] != [s for s in wanted.split('\n') if s]:
        return False
    fn['arguments'] = json.dumps(supplied, separators=(',', ':'))
    return True


def repair_completion(payload, body):
    contract = _contract(body)
    changed = False
    for choice in payload.get('choices') or []:
        if choice.get('finish_reason') != 'tool_calls':
            continue
        calls = (choice.get('message', choice.get('delta')) or {}).get('tool_calls') or []
        if len(calls) == 1:
            changed = _restore(calls[0].get('function') or {}, contract) or changed
    return changed


def repair_sse(lines, body):
    contract = _contract(body)
    if contract is None:
        return lines
    events, calls, finishes = {}, {}, {}
    for pos, line in enumerate(lines):
        if not line.startswith(b'data: '):
            continue
        try:
            event = json.loads(line[6:])
        except ValueError:
            continue
        events[pos] = event
        for ci, choice in enumerate(event.get('choices') or []):
            ci = choice.get('index', ci)
            if choice.get('finish_reason'):
                finishes[ci] = choice['finish_reason']
            for ti, tc in enumerate((choice.get('delta') or {}).get('tool_calls') or []):
                fn = tc.get('function') or {}
                entry = calls.setdefault((ci, tc.get('index', ti)), {'name': '', 'arguments': '', 'parts': []})
                entry['name'] += fn.get('name') or ''
                entry['arguments'] += fn.get('arguments') or ''
                entry['parts'].append((pos, fn))
    changed = set()
    for (ci, _), call in calls.items():
        if finishes.get(ci) != 'tool_calls' or sum(k[0] == ci for k in calls) != 1:
            continue
        if not _restore(call, contract):
            continue
        for i, (pos, fn) in enumerate(call['parts']):
            fn['arguments'] = call['arguments'] if i == 0 else ''
            changed.add(pos)
    return [b'data: ' + json.dumps(events[i], separators=(',', ':')).encode() + b'\n'
            if i in changed else line for i, line in enumerate(lines)]
