"""Do not broaden an explicitly supplied tool call with invented optional fields."""
import json
import re

import tool_argument_json

_EXACT = re.compile(r'\b(?:next\s+)?call\s+([A-Za-z_][\w.-]*)\s+with\s+exactly(?:\s+these\s+arguments)?\s*:\s*', re.I)


def _request(body):
    message = next((m for m in reversed(body.get('messages') or []) if m.get('role') == 'user'), {})
    text = message.get('content')
    if not isinstance(text, str):
        return None
    text = text.strip()
    match = _EXACT.match(text)
    if not match:
        return None
    try:
        args, end = json.JSONDecoder().raw_decode(text[match.end():])
    except ValueError:
        return None
    if not isinstance(args, dict) or text[match.end() + end:].strip() not in ('', '.'):
        return None
    name = match.group(1)
    offered = [t.get('function') or {} for t in body.get('tools') or []
               if (t.get('function') or {}).get('name') == name]
    if len(offered) != 1:
        return None
    return name, args, offered[0].get('parameters') or {}


def _restrict(actual, supplied, schema):
    if isinstance(actual, dict) and isinstance(supplied, dict):
        if not isinstance(schema, dict) or not set(supplied).issubset(actual):
            raise ValueError('Missing or unsupported fields')
        properties = schema.get('properties') or {}
        if not isinstance(properties, dict):
            raise ValueError('No object schema')
        extra = set(actual) - set(supplied)
        if any(k not in properties or k in (schema.get('required') or []) for k in extra):
            raise ValueError('Unknown or required extra field')
        return {k: _restrict(actual[k], v, properties.get(k, {})) for k, v in supplied.items()}
    if isinstance(actual, list) and isinstance(supplied, list):
        if len(actual) != len(supplied):
            raise ValueError('Array contents changed')
        items = schema.get('items', {}) if isinstance(schema, dict) else {}
        return [_restrict(a, b, items) for a, b in zip(actual, supplied)]
    if json.dumps(actual, sort_keys=True) != json.dumps(supplied, sort_keys=True):
        raise ValueError('Supplied value changed')
    return actual


def repair_completion(payload, body):
    contract = _request(body)
    if contract is None:
        return False
    name, supplied, schema = contract
    changed = False
    for choice in payload.get('choices') or []:
        if choice.get('finish_reason') != 'tool_calls':
            continue
        message = choice.get('message', choice.get('delta')) or {}
        calls = message.get('tool_calls') or []
        if len(calls) != 1:
            continue
        fn = calls[0].get('function') or {}
        raw = fn.get('arguments')
        if fn.get('name') != name or not isinstance(raw, str):
            continue
        try:
            args = tool_argument_json.loads(raw)
            restricted = _restrict(args, supplied, schema)
        except (ValueError, TypeError):
            continue
        if restricted != args:
            fn['arguments'] = json.dumps(restricted, separators=(',', ':'))
            changed = True
    return changed


def repair_sse(lines, body):
    if _request(body) is None:
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
                entry = calls.setdefault((ci, tc.get('index', ti)), {'function': {'name': '', 'arguments': ''}, 'parts': []})
                entry['function']['name'] += fn.get('name') or ''
                entry['function']['arguments'] += fn.get('arguments') or ''
                entry['parts'].append((pos, fn))
    changed = set()
    for (ci, _), call in calls.items():
        if sum(key[0] == ci for key in calls) != 1:
            continue
        complete = {'choices': [{'message': {'tool_calls': [call]}, 'finish_reason': finishes.get(ci)}]}
        if not repair_completion(complete, body):
            continue
        for i, (pos, fn) in enumerate(call['parts']):
            fn['arguments'] = call['function']['arguments'] if i == 0 else ''
            changed.add(pos)
    return [b'data: ' + json.dumps(events[i], separators=(',', ':')).encode() + b'\n'
            if i in changed else line for i, line in enumerate(lines)]
