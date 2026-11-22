"""Start FLM with the selected model's load context."""
import os
from pathlib import Path

from hearth.backends.flm_context import load_context


def command(model):
    return ['/opt/fastflowlm/bin/flm', 'serve', '--host', '127.0.0.1',
            '--port', '52625', '--ctx-len', str(load_context(model)), '--quiet']


def main():
    active = Path.home() / '.config/fastflowlm/active'
    model = active.read_text().strip() if active.exists() else ''
    args = command(model)
    print(f'Starting FLM for {model or "default model"} with context {load_context(model)}', flush=True)
    os.execv(args[0], args)


if __name__ == '__main__':
    main()
