"""Print ~/.config/opencode/opencode.jsonc content from Hearth's live backends.

Run manually or from deploy/opencode-config-sync.timer so the desktop OpenCode
config can be refreshed from current model context limits and formula-derived
compaction budgets, using help_data.configured_compaction_for_window(). The script
prints the config for review and never writes the user's local file.

Exit 0 always unless generation itself fails; a backend hiccup must never kill a
running desktop OpenCode session by rewriting its config mid-flight.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hearth.core import help_data
from hearth.backends import q38rocm_client

CONFIG_PATH = Path.home() / '.config/opencode/opencode.jsonc'


def _strip_jsonc(text: str) -> dict:
    raw = re.sub(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/',
                 lambda m: m.group(1) or '', text, flags=re.S)
    raw = re.sub(r'("(?:\\.|[^"\\])*")|,([\s]*[}\]])',
                 lambda m: m.group(1) or m.group(2), raw)
    return json.loads(raw)


def _add_desktop_q38_provider(config: dict) -> None:
    """Expose the loopback-only q38rocm model to OpenCode on this desktop."""
    if not q38rocm_client._MODEL_PATH.is_file():
        return
    model = q38rocm_client._MODEL_NAME
    config.setdefault('provider', {})['q38rocm'] = {
        'npm': '@ai-sdk/openai-compatible',
        'name': 'q38rocm (this desktop)',
        'options': {
            'baseURL': q38rocm_client.base_url().rstrip('/') + '/v1',
            'apiKey': q38rocm_client.api_key() or 'q38rocm-local',
        },
        'models': {
            model: {
                'name': model + ' (local RADV Vulkan)',
                'limit': {'context': q38rocm_client.configured_context(),
                          'output': q38rocm_client.configured_context() // 2},
            },
        },
    }


def main() -> int:
    try:
        config = json.loads(help_data.build()['opencode_config'])
        _add_desktop_q38_provider(config)
    except Exception:
        print('sync-opencode-config: generation failed; no file was changed',
              file=sys.stderr)
        return 0

    text = None
    try:
        text = CONFIG_PATH.read_text()
    except FileNotFoundError:
        pass
    existing = None
    if text is not None:
        try:
            existing = _strip_jsonc(text)
        except ValueError:
            pass

    if existing:
        # Keep the current model only while it remains in the live catalog. This
        # avoids pinning OpenCode to a removed model after Hearth refreshes providers.
        old_model = existing.get('model')
        old_provider, separator, old_model_id = str(old_model or '').partition('/')
        available_models = ((config.get('provider') or {}).get(old_provider) or {}).get('models') or {}
        if separator and old_model_id in available_models:
            config['model'] = old_model

        # Preserve custom agents and settings while refreshing generated provider data.
        existing_agents = existing.get('agent') or {}
        generated_agents = config.get('agent') or {}
        if existing_agents or generated_agents:
            config['agent'] = {**existing_agents, **generated_agents}
        default_agent = existing.get('default_agent')
        if default_agent:
            config['default_agent'] = default_agent
        for key, value in existing.items():
            if key not in {'$schema', 'model', 'provider', 'compaction', 'agent', 'default_agent'}:
                config.setdefault(key, value)
        for provider, value in (existing.get('provider') or {}).items():
            if provider not in (config.get('provider') or {}):
                config['provider'][provider] = value

    provider, _, model_id = str(config.get('model') or '').partition('/')
    window = (((config.get('provider') or {}).get(provider) or {})
              .get('models', {}).get(model_id) or {}).get('limit', {}).get('context')
    if isinstance(window, int) and window > 0:
        settings = help_data.compaction_settings()
        budgets = help_data.configured_compaction_for_window(
            window, keep_override=settings['keep_tokens'], buffer_override=settings['buffer'])
        config['compaction'] = {'auto': settings['auto'], 'keep': {'tokens': budgets['keep']},
                                'buffer': budgets['buffer']}

    # Display only: never overwrite the local OpenCode config.
    print(json.dumps(config, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
