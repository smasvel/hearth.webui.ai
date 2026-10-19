"""Bounded, reversible per-model compatibility recipes; never generated Python code."""
import settings_store

RECIPE = 'strict-tools-v1'


def key(backend, model):
    canonical = str(model).removeprefix('flm/') if backend == 'fastflowlm' else str(model).removeprefix('sglang/') if backend == 'sglang' else str(model)
    return 'tool_test_profile:' + backend + ':' + canonical


def get(backend, model):
    return settings_store.get(key(backend, model))


def set_profile(backend, model, value):
    if value not in (None, RECIPE):
        raise ValueError('Unknown compatibility recipe')
    settings_store.set(key(backend, model), value)


def apply(body, backend):
    if not body.get('tools') or body.get('tool_choice') == 'none':
        return body
    return apply_recipe(body, get(backend, body.get('model', '')))


def apply_recipe(body, recipe):
    if recipe != RECIPE or not body.get('tools') or body.get('tool_choice') == 'none':
        return body
    instruction = (
        'Tool contract: use only the offered functions and exact schema parameter names. '
        'Preserve JSON types, required fields, and requested function names. '
        'When tool_choice is required, call a function; for a named choice call that function. '
        'When the user explicitly asks for multiple tool calls in the same response, emit '
        'a separate function call for each, in order, using the model transport\'s tool format. For ordinary questions that need '
        'no function answer directly. After tool results, answer from the result, never '
        'invent a result. Read files before editing and run requested verification before finishing.'
    )
    messages = list(body.get('messages') or [])
    if any(m.get('role') == 'system' and isinstance(m.get('content'), str) and m['content'].endswith(instruction) for m in messages):
        return {**body, 'temperature': 0}
    if messages and messages[0].get('role') == 'system' and isinstance(messages[0].get('content'), str):
        messages[0] = {**messages[0], 'content': messages[0]['content'] + '\n\n' + instruction}
    else:
        messages.insert(0, {'role': 'system', 'content': instruction})
    return {**body, 'messages': messages, 'temperature': 0}
