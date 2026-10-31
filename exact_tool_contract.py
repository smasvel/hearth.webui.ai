"""Do not broaden an explicitly supplied tool call with invented optional fields."""
import json
import re
import uuid

import tool_argument_json

_EXACT = re.compile(r'\b(?:next\s+)?call\s+([A-Za-z_][\w.-]*)\s+with\s+exactly(?:\s+these\s+arguments)?\s*:\s*', re.I)
# Same contract, framed inside a sentence and using singular wording. Only consulted when
# the caller passes relaxed=True (lmstudio_shim); every other caller keeps _EXACT above.
_EXACT_RELAXED = re.compile(
    r'\b(?:next\s+)?call\s+([A-Za-z_][\w.-]*)\s+with\s+exactly'
    r'(?:\s+(?:these|this)\s+arguments?)?\s*:\s*', re.I)


def _request(body, relaxed=False):
    message = next((m for m in reversed(body.get('messages') or []) if m.get('role') == 'user'), {})
    text = message.get('content')
    if not isinstance(text, str):
        return None
    text = text.strip()
    # Strict (default): the whole message is the contract, exactly as before.
    # Relaxed (used only when the client forced one named function): the instruction may
    # be framed by surrounding sentences ("Read the invoice first. Call read with exactly
    # this argument: {...}. Then read the caller."). What keeps this safe is unchanged -
    # a complete JSON object must directly follow the colon, and the named function must
    # be one of the offered schemas.
    if relaxed:
        match = _EXACT_RELAXED.search(text)
        if not match:
            return None
        try:
            args, _end = json.JSONDecoder().raw_decode(text[match.end():])
        except ValueError:
            return None
        if not isinstance(args, dict):
            return None
    else:
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


def enforce_named_choice_completion(payload, body, relaxed=False):
    """Honor a named OpenAI tool choice when the user supplied its exact arguments."""
    choice = body.get('tool_choice')
    function = choice.get('function') if isinstance(choice, dict) else None
    chosen = function.get('name') if isinstance(function, dict) else None
    contract = _request(body, relaxed=relaxed)
    if (not isinstance(choice, dict) or choice.get('type') != 'function' or not isinstance(chosen, str)
            or contract is None or contract[0] != chosen):
        return False
    name, arguments, _schema = contract
    choices = payload.get('choices') or []
    if not choices:
        return False
    result = choices[0]
    message = result.setdefault('message', {})
    calls = message.get('tool_calls') or []
    if len(calls) > 1:
        # Duplicate emission of the one function the client forced is transport noise,
        # not a parallel request: observed live with mimo-v2.6-distill, which repeats its
        # legacy markup until the budget ends and LM Studio surfaces the same call twice.
        # Only the relaxed (lmstudio_shim) caller opts into this; it collapses only when
        # every call targets the contracted function and the parseable argument sets are
        # identical. Every other caller keeps the previous no-op behavior.
        if not relaxed:
            return False
        parsed = []
        for call in calls:
            function = call.get('function') or {}
            if function.get('name') != name:
                return False
            try:
                parsed.append(tool_argument_json.loads(function.get('arguments') or '{}'))
            except (ValueError, TypeError):
                parsed.append(None)
        valid = [args for args in parsed if isinstance(args, dict)]
        if not valid or any(json.dumps(args, sort_keys=True) != json.dumps(valid[0], sort_keys=True)
                            for args in valid):
            return False
        calls = calls[:1]
        message['tool_calls'] = calls
    if calls:
        call = calls[0]
        function = call.setdefault('function', {})
        desired = {'name': name, 'arguments': json.dumps(arguments, separators=(',', ':'))}
        changed = function.get('name') != name or function.get('arguments') != desired['arguments']
        function.update(desired)
    else:
        call = {'id': 'call_hearth_' + uuid.uuid4().hex, 'type': 'function',
                'function': {'name': name, 'arguments': json.dumps(arguments, separators=(',', ':'))}}
        message['tool_calls'] = [call]
        changed = True
    if changed:
        message['content'] = None
        result['finish_reason'] = 'tool_calls'
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


def enforce_named_choice_sse(lines, body, relaxed=False):
    """Repair a single named choice in a buffered SSE response, preserving its metadata."""
    choice = body.get('tool_choice')
    function = choice.get('function') if isinstance(choice, dict) else None
    chosen = function.get('name') if isinstance(function, dict) else None
    contract = _request(body, relaxed=relaxed)
    if (not isinstance(choice, dict) or choice.get('type') != 'function' or not isinstance(chosen, str)
            or contract is None or contract[0] != chosen):
        return lines
    events = []
    done_line = b'data: [DONE]\n'
    for line in lines:
        if line.startswith(b'data: [DONE]'):
            done_line = line
            continue
        if not line.startswith(b'data: '):
            continue
        try:
            event = json.loads(line[6:])
        except (ValueError, TypeError):
            continue
        if event.get('choices'):
            events.append(event)
    if not events:
        return lines
    payload = {'choices': [{'message': {'tool_calls': []}, 'finish_reason': None}]}
    calls = {}
    for event in events:
        for item in event.get('choices') or []:
            if item.get('finish_reason'):
                payload['choices'][0]['finish_reason'] = item['finish_reason']
            for index, call in enumerate((item.get('delta') or {}).get('tool_calls') or []):
                slot = calls.setdefault(call.get('index', index), {'id': '', 'type': 'function',
                    'function': {'name': '', 'arguments': ''}})
                slot['id'] += call.get('id') or ''
                slot['type'] = call.get('type') or slot['type']
                fn = call.get('function') or {}
                slot['function']['name'] += fn.get('name') or ''
                slot['function']['arguments'] += fn.get('arguments') or ''
    payload['choices'][0]['message']['tool_calls'] = [calls[key] for key in sorted(calls)]
    if not enforce_named_choice_completion(payload, body, relaxed=relaxed):
        return lines
    meta = {key: value for key, value in events[-1].items() if key not in ('choices', 'usage')}
    tool_call = payload['choices'][0]['message']['tool_calls'][0]
    first = {**meta, 'choices': [{'index': 0, 'delta': {'tool_calls': [
        {'index': 0, **tool_call}]}, 'finish_reason': None}]}
    final = {**meta, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]}
    usage = next((event['usage'] for event in reversed(events) if 'usage' in event), None)
    replacement = [b'data: ' + json.dumps(first, separators=(',', ':')).encode() + b'\n\n',
                   b'data: ' + json.dumps(final, separators=(',', ':')).encode() + b'\n\n']
    if usage is not None:
        replacement.append(b'data: ' + json.dumps({**meta, 'choices': [], 'usage': usage},
            separators=(',', ':')).encode() + b'\n\n')
    replacement.append(done_line)
    return replacement
