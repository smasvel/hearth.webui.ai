"""Let Granite emit its native tool markup before parsing complete arguments.

llama.cpp's inferred object grammar conflicts with Granite's JSON-encoded argument
strings. Supply tools through the model's existing available_tools template input
so generation is unconstrained; decode only actual complete, offered tool calls.
"""
import json
import re
import uuid
import tool_argument_json


def enabled(body):
    return (str(body.get('model', '')).lower().split('/')[-1].startswith('granite-4')
            and bool(body.get('tools')) and body.get('tool_choice') != 'none'
            and body.get('n', 1) == 1)


def transport(body):
    if not enabled(body):
        return body
    result = dict(body)
    kwargs = dict(body.get('chat_template_kwargs') or {})
    kwargs['available_tools'] = result.pop('tools')
    result['chat_template_kwargs'] = kwargs
    choice = result.pop('tool_choice', 'auto')
    instruction = ('Tool-call format: put the JSON object directly after "arguments":. '
                   'Do not quote or escape the arguments object. '
                   'Close every complete tool call with </tool_call>. '
                   'When several independent calls are requested, emit a separate <tool_call> '
                   'block for each in this same response, before waiting for any result.')
    if choice == 'required':
        instruction += ' You must respond with at least one call to an offered tool.'
    elif isinstance(choice, dict):
        name = (choice.get('function') or {}).get('name')
        if name:
            instruction += f' You must call the offered function {name} in this response.'
    if body.get('parallel_tool_calls') is False:
        instruction = (instruction + ' ' if instruction else '') + 'Emit at most one tool call in this response.'
    if instruction:
        messages = [dict(m) for m in result.get('messages') or []]
        if messages and messages[0].get('role') == 'system':
            messages[0]['content'] = (messages[0].get('content') or '') + '\n' + instruction
        else:
            messages.insert(0, {'role': 'system', 'content': instruction})
        result['messages'] = messages
    return result


def repair_completion(payload, body):
    if not enabled(body):
        return False
    changed = False
    for choice in payload.get('choices') or []:
        message = choice.get('message') or {}
        if choice.get('finish_reason') != 'stop' or message.get('tool_calls'):
            continue
        clean, calls = _parse_calls(message.get('content') or '', body)
        if calls:
            message.update(content=clean or None, tool_calls=calls)
            choice['finish_reason'] = 'tool_calls'
            changed = True
    return changed


def _parse_calls(text, body):
    blocks = list(re.finditer(r'<tool_call>\s*(.*?)\s*</tool_call>', text, re.S))
    if not blocks or len(blocks) != text.count('<tool_call>'):
        return text, []
    offered = {(tool.get('function') or {}).get('name') for tool in body['tools']}
    calls, clean, previous = [], [], 0
    for block in blocks:
        try:
            value = tool_argument_json.loads(block.group(1))
            if not isinstance(value, dict) or value.get('name') not in offered:
                return text, []
            args = value['arguments']
            if isinstance(args, str):
                args = tool_argument_json.loads(args)
            if not isinstance(args, dict):
                return text, []
        except (ValueError, KeyError, TypeError):
            return text, []
        calls.append({'id': 'call_' + uuid.uuid4().hex, 'type': 'function',
                      'function': {'name': value['name'], 'arguments': json.dumps(args)}})
        clean.append(text[previous:block.start()])
        previous = block.end()
    clean.append(text[previous:])
    return ''.join(clean).strip(), calls


def repair_sse(lines, body):
    if not enabled(body) or not any(line.strip() == b'data: [DONE]' for line in lines):
        return lines
    finishes, content, usage, template = [], [], {}, {}
    for line in lines:
        if not line.startswith(b'data: '):
            continue
        try:
            event = json.loads(line[6:])
        except ValueError:
            continue
        usage.update(event.get('usage') or {})
        template.update({k: v for k, v in event.items() if k not in ('choices', 'usage')})
        for choice in event.get('choices') or []:
            delta = choice.get('delta') or {}
            if delta.get('tool_calls'):
                return lines
            content.append(delta.get('content') or '')
            if choice.get('finish_reason'):
                finishes.append(choice['finish_reason'])
    if not finishes or any(reason != 'stop' for reason in finishes):
        return lines
    clean, calls = _parse_calls(''.join(content), body)
    if not calls:
        return lines
    delta = {'tool_calls': [{**call, 'index': i} for i, call in enumerate(calls)]}
    if clean:
        delta['content'] = clean
    event = {**template, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': 'tool_calls'}]}
    if usage:
        event['usage'] = usage
    return [b'data: ' + json.dumps(event).encode() + b'\n\n', b'data: [DONE]\n\n']
