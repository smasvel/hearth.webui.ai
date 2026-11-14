"""Enforce disabled per-model thinking across Lemonade request paths."""
import json

from hearth.storage import settings_store


def apply(body):
    try:
        overrides = json.loads(settings_store.get('lemonade_model_thinking_overrides') or '{}')
    except (TypeError, ValueError):
        return body
    if not isinstance(overrides, dict) or overrides.get(body.get('model')) is not False:
        return body
    kwargs = body.get('chat_template_kwargs')
    kwargs = kwargs if isinstance(kwargs, dict) else {}
    return {**body, 'enable_thinking': False,
            'chat_template_kwargs': {**kwargs, 'enable_thinking': False}}
