"""Per-model LM Studio thinking preference for Hearth-managed requests."""
import json

import settings_store


def overrides():
    try:
        data = json.loads(settings_store.get('lmstudio_model_thinking_overrides') or '{}')
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items()
            if isinstance(key, str) and isinstance(value, bool)}


def apply(body):
    if overrides().get(body.get('model')) is not False:
        return body
    kwargs = body.get('chat_template_kwargs')
    kwargs = kwargs if isinstance(kwargs, dict) else {}
    return {**body, 'enable_thinking': False,
            'chat_template_kwargs': {**kwargs, 'enable_thinking': False}}
