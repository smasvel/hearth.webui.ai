"""Model-specific load options for Lemonade — large-context and template overrides.

Two distinct knob families live here, both keyed off the model_id because Lemonade's
catalog entries carry no structured per-model option fields, only a free-form checkpoint
string and labels. Everything is fail-soft and additive: an unknown/absent model_id
falls straight through to `{}` (no options), so this never breaks a model it doesn't
recognize.
"""
import shlex
from pathlib import Path


def _small_amd_memory_budget() -> bool:
    """Detect AMD devices with a shared GPU pool smaller than 32 GiB."""
    for path in Path('/sys/class/drm').glob('card*/device/mem_info_gtt_total'):
        try:
            budget = int(path.read_text().strip())
        except (OSError, ValueError):
            continue
        if 0 < budget < 32 * 1024**3:
            return True
    return False


# Flm-recipe models with a context window larger than the default are configured to run at
# that larger window (via --ctx-size on the underlying llama-server) rather than their
# default. The model name alone is the only reliable signal at load time, so a
# case-insensitive name match supplies this built-in default. A saved per-model ctx-size in
# the Models page still overrides it: load_model() applies the explicit context_length after
# these options.
_LARGE_CONTEXT_FLM_MODELS = {
    'julianmb-qwen-3.8-27b-roml-r1-rocmfpp4-fast-gguf',
    'julianmb-qwen-3.8-27b-roml-r2-rocmfpp4-fast-gguf',
    'julianmb-qwen-3.8-27b-rocmfpp4-fast-gguf',
    'julianmb-qwen3.5-30b-a3b-instruct-rocmfpp4-fast-gguf',
}
_LARGE_CONTEXT_TOKENS = 120_000


def load_options(model_id: str, current: dict | None = None) -> dict:
    if model_id.lower() in _LARGE_CONTEXT_FLM_MODELS:
        options = dict(current or {})
        options.setdefault('ctx_size', _LARGE_CONTEXT_TOKENS)
        return options
    if (model_id.lower() in {
            'nvidia-nemotron-3.5-lightning-30b-a3b-gguf',
            'ornith-1.5-35b-a3b-gguf',
            'k2-horizon-mova-36b-a4b-gguf',
        }
            and _small_amd_memory_budget()):
        # These models exhaust the small shared GPU pool during initialization
        # (radv command submission failure / ErrorDeviceLost). Use system RAM.
        options = dict(current or {})
        options['llamacpp_backend'] = 'cpu'
        return options
    if model_id.lower() != "devstral-small-2507-gguf":
        return {}
    options = dict(current or {})
    args = shlex.split(options.get("llamacpp_args") or "--parallel 1")
    if "--chat-template" in args and args[args.index("--chat-template") + 1] == "mistral-v7":
        pos = args.index("--chat-template")
        del args[pos:pos + 2]
    if "--chat-template-file" not in args and "--chat-template" not in args:
        # With --jinja the value must be actual Jinja, not a built-in name.
        # Inline it because the snap daemon cannot read the web UI's home files.
        template = (Path(__file__).resolve().parents[2] / "chat_templates" / "devstral-small-2507.jinja").read_text().strip()
        args.extend(["--chat-template", template])
    options["llamacpp_args"] = shlex.join(args)
    return options
