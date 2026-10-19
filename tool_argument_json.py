"""Decode tool arguments, tolerating only redundant terminal closing delimiters."""
import json


def loads(raw):
    try:
        return json.loads(raw)
    except json.JSONDecodeError as original:
        stripped = raw.lstrip()
        try:
            value, end = json.JSONDecoder().raw_decode(stripped)
        except json.JSONDecodeError:
            raise original
        suffix = stripped[end:]
        # One complete object followed only by a small number of extra closers.
        # Never accept a second JSON value, text, markup, missing delimiters, or
        # a truncated string. The decoder preserves all bytes within string values.
        if (isinstance(value, dict) and 0 < len(suffix) <= 64
                and suffix.strip() and not suffix.strip(']} \t\r\n')):
            return value
        raise original


def _repair_function(fn):
    raw = fn.get('arguments')
    if not isinstance(raw, str):
        return False
    try:
        json.loads(raw)
        return False
    except json.JSONDecodeError:
        try:
            parsed = loads(raw)
        except json.JSONDecodeError:
            return False
    fn['arguments'] = json.dumps(parsed, separators=(',', ':'))
    return True


def repair_completion(payload):
    changed = False
    for choice in payload.get('choices') or []:
        if choice.get('finish_reason') != 'tool_calls':
            continue
        for call in (choice.get('message') or {}).get('tool_calls') or []:
            changed = _repair_function(call.get('function') or {}) or changed
    return changed


def repair_sse(lines):
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
            for ti, call in enumerate((choice.get('delta') or {}).get('tool_calls') or []):
                fn = call.get('function') or {}
                entry = calls.setdefault((ci, call.get('index', ti)), {'arguments': '', 'parts': []})
                entry['arguments'] += fn.get('arguments') or ''
                entry['parts'].append((pos, fn))
    changed = set()
    for (ci, _), call in calls.items():
        if finishes.get(ci) != 'tool_calls' or not _repair_function(call):
            continue
        for i, (pos, fn) in enumerate(call['parts']):
            fn['arguments'] = call['arguments'] if i == 0 else ''
            changed.add(pos)
    return [b'data: ' + json.dumps(events[i], separators=(',', ':')).encode() + b'\n'
            if i in changed else line for i, line in enumerate(lines)]
