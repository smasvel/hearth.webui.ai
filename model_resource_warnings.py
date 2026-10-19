"""Attach reviewed local resource incidents to the matching backend and model."""
import json
from pathlib import Path

CATALOG = Path(__file__).with_name('model_resource_warnings.json')


def annotate(models, local_backends):
    try:
        catalog = json.loads(CATALOG.read_text())
    except (OSError, ValueError):
        return models
    warnings = {(entry['backend'], entry['model'].casefold()): entry
                for entry in catalog['models']}
    result = []
    for model in models:
        model = dict(model)
        key = (model.get('backend'), str(model.get('id', '')).casefold())
        if key[0] in local_backends and key in warnings:
            model['resource_warning'] = {**warnings[key], 'reviewed_at': catalog['reviewed_at'],
                                         'scope': catalog['scope']}
        result.append(model)
    return result
