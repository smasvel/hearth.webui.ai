"""Regenerate ~/.config/opencode/opencode.jsonc from Hearth's live backends.

Run manually or from deploy/opencode-config-sync.timer so the desktop OpenCode
config never drifts from reality: model context limits (advertised windows) and
compaction keep/buffer are re-derived from whatever each backend currently
reports, using help_data.compaction_for_window(). The write is idempotent — if
the freshly generated config equals the current file (ignoring comments), nothing
is touched. Each real change leaves a timestamped .bak next to the file.

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
        # Preserve the user's chosen default model and hand-added agents; only
        # refresh provider limits and compaction budgets for display.
        if 'model' in existing:
            config['model'] = existing['model']
        if 'agents' in existing:
            agents = existing['agents']
            config.setdefault('agents', {}).update(agents)

    provider, _, model_id = str(config.get('model') or '').partition('/')
    window = (((config.get('provider') or {}).get(provider) or {})
              .get('models', {}).get(model_id) or {}).get('limit', {}).get('context')
    if isinstance(window, int) and window > 0:
        budgets = help_data.compaction_for_window(window)
        config['compaction'] = {'auto': help_data.compaction_settings()['auto'], 'keep': {'tokens': budgets['keep']},
                                'buffer': budgets['buffer']}

    # Display only: never overwrite the local OpenCode config.
    print(json.dumps(config, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
