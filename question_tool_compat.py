"""Preserve OpenCode question semantics across local model response formats."""
import copy
import json
from collections import Counter


def normalize(arguments, schema, requested=None):
    if not isinstance(schema, dict) or not isinstance(schema.get('properties'), dict):
        return arguments
    questions = schema['properties'].get('questions')
    if not isinstance(questions, dict):
        return arguments
    items = questions.get('items')
    if not isinstance(items, dict) or not isinstance(items.get('properties'), dict):
        return arguments
    properties = items['properties']
    if not isinstance(arguments, dict) or not isinstance(arguments.get('questions'), list):
        return arguments
    result = copy.deepcopy(arguments)
    for question in result['questions']:
        if not isinstance(question, dict):
            continue
        # OpenCode defaults to a single selection. Never invent a required answer.
        if (isinstance(properties.get('multiple'), dict)
                and properties['multiple'].get('type') == 'boolean'
                and 'multiple' not in (items.get('required') or [])
                and 'multiple' not in question):
            question['multiple'] = False
        options = question.get('options')
        if 'options' in properties and isinstance(options, list):
            # OpenCode asks that recommended choices appear first. Keep all labels,
            # descriptions, other choices and their relative order intact.
            question['options'] = sorted(options, key=lambda option:
                0 if isinstance(option, dict) and isinstance(option.get('label'), str)
                and option['label'].rstrip().lower().endswith('(recommended)') else 1)
        # Reconcile only an unambiguous latest-turn payload whose question text,
        # header, and full option set match. The supplied selection mode is part of
        # that contract too; never replace generated labels, descriptions, or content.
        matches = []
        for supplied in (requested or {}).get('questions', []):
            if not isinstance(supplied, dict) or not isinstance(supplied.get('options'), list):
                continue
            without_choices = lambda value: {k: v for k, v in value.items()
                                              if k not in ('options', 'multiple')}
            if without_choices(question) != without_choices(supplied):
                continue
            encode = lambda value: json.dumps(value, sort_keys=True)
            if isinstance(options, list) and Counter(map(encode, options)) == Counter(map(encode, supplied['options'])):
                matches.append(supplied)
        if len(matches) == 1:
            supplied = matches[0]
            question['options'] = copy.deepcopy(supplied['options'])
            if isinstance(supplied.get('multiple'), bool):
                question['multiple'] = supplied['multiple']
    return result


def _requested(body, schema):
    """Find an unambiguous explicit JSON question payload in the latest user turn."""
    latest = next((m for m in reversed(body.get('messages') or []) if m.get('role') == 'user'), {})
    content = latest.get('content')
    if not isinstance(content, str):
        return None
    decoder = json.JSONDecoder()
    candidates = []
    for index, character in enumerate(content):
        if character != '{':
            continue
        try:
            value, _ = decoder.raw_decode(content[index:])
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get('questions'), list):
            candidates.append(normalize(value, schema))
            # normalize applies defaults; explicit option order takes precedence.
            for original, fixed in zip(value['questions'], candidates[-1]['questions']):
                if isinstance(original, dict) and isinstance(original.get('options'), list):
                    fixed['options'] = original['options']
    return candidates[0] if len(candidates) == 1 else None


def _schema(body):
    for tool in body.get('tools') or []:
        function = tool.get('function') or {}
        if function.get('name') == 'question':
            return function.get('parameters') or {}
    return {}


def repair_completion(payload, body):
    schema = _schema(body)
    if not schema:
        return False
    requested = _requested(body, schema)
    changed = False
    for choice in payload.get('choices') or []:
        message = choice.get('message', choice.get('delta', {})) or {}
        for call in message.get('tool_calls') or []:
            fn = call.get('function') or {}
            if fn.get('name') != 'question':
                continue
            raw = fn.get('arguments')
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                continue
            fixed = normalize(args, schema, requested)
            if fixed != args:
                fn['arguments'] = json.dumps(fixed, separators=(',', ':'))
                changed = True
    return changed


def repair_sse(lines, body):
    """Repair only complete question arguments; retain call IDs, usage and SSE order."""
    schema = _schema(body)
    if not schema:
        return lines
    requested = _requested(body, schema)
    events, calls = {}, {}
    for position, line in enumerate(lines):
        if not line.startswith(b'data: '):
            continue
        try:
            event = json.loads(line[6:])
        except (ValueError, TypeError):
            continue
        events[position] = event
        for index, choice in enumerate(event.get('choices') or []):
            for call_index, call in enumerate((choice.get('delta') or {}).get('tool_calls') or []):
                fn = call.get('function') or {}
                entry = calls.setdefault((choice.get('index', index), call.get('index', call_index)),
                                         {'name': '', 'arguments': '', 'parts': []})
                entry['name'] += fn.get('name') or ''
                entry['arguments'] += fn.get('arguments') or ''
                entry['parts'].append((position, fn))
    changed = set()
    for call in calls.values():
        if call['name'] != 'question':
            continue
        try:
            args = json.loads(call['arguments'])
        except ValueError:
            continue
        fixed = normalize(args, schema, requested)
        if fixed == args:
            continue
        for index, (position, fn) in enumerate(call['parts']):
            fn['arguments'] = json.dumps(fixed, separators=(',', ':')) if index == 0 else ''
            changed.add(position)
    return [b'data: ' + json.dumps(events[i], separators=(',', ':')).encode() + b'\n'
            if i in changed else line for i, line in enumerate(lines)]
