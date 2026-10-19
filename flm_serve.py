"""Start FLM with the selected model's supported context, rather than a global 8K cap."""
import os
from pathlib import Path

from flm_context import supported_context


def command(model):
    return ['/opt/fastflowlm/bin/flm', 'serve', '--host', '127.0.0.1',
            '--port', '52625', '--ctx-len', str(supported_context(model)), '--quiet']


if __name__ == '__main__':
    active = Path.home() / '.config/fastflowlm/active'
    model = active.read_text().strip() if active.exists() else ''
    args = command(model)
    print(f'Starting FLM for {model or "default model"} with context {supported_context(model)}', flush=True)
    os.execv(args[0], args)
