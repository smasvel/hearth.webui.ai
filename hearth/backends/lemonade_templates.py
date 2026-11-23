"""Model-specific load options for Lemonade — large-context and template overrides.

Two distinct knob families live here, both keyed off the model_id because Lemonade's
catalog entries carry no structured per-model option fields, only a free-form checkpoint
string and labels. Everything is fail-soft and additive: an unknown/absent model_id
falls straight through to `{}` (no options), so this never breaks a model it doesn't
recognize.
"""
import shlex
import re
from pathlib import Path


def _amd_shared_gpu_budget_bytes() -> int | None:
    """Return the smallest detected AMD shared GPU pool, if available."""
    budgets = []
    for path in Path('/sys/class/drm').glob('card*/device/mem_info_gtt_total'):
        try:
            budget = int(path.read_text().strip())
        except (OSError, ValueError):
            continue
        if budget > 0:
            budgets.append(budget)
    return min(budgets) if budgets else None


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

_QWEN_27B_ACCELERATION_ARGS = (
    ("--flash-attn", "on"),
    ("--cache-type-k", "q8_0"),
    ("--cache-type-v", "q8_0"),
)


def load_options(model_id: str, current: dict | None = None, model_size_gb: float | None = None,
                 *, rocm_available: bool = False) -> dict:
    model_key = model_id.lower()
    options = dict(current or {})
    if model_key in _LARGE_CONTEXT_FLM_MODELS:
        options.setdefault('ctx_size', _LARGE_CONTEXT_TOKENS)
    is_qwen_27b = bool(re.search(r"qwen.*\b27b\b", model_key))
    if is_qwen_27b:
        args = shlex.split(options.get("llamacpp_args") or "")
        for flag, value in _QWEN_27B_ACCELERATION_ARGS:
            if flag not in args:
                args.extend((flag, value))
        options["llamacpp_args"] = shlex.join(args)
        if rocm_available:
            options["llamacpp_backend"] = "rocm"

    shared_gpu_budget = _amd_shared_gpu_budget_bytes()
    known_gpu_heavy = model_key in {
            'nvidia-nemotron-3.5-lightning-30b-a3b-gguf',
            'ornith-1.5-35b-a3b-gguf',
            'k2-horizon-mova-36b-a4b-gguf',
            'phillylabs-kat-coder-v2.5-dev-35b-a3b-abliterated-uncensored-philadelphia-class',
        }
    # On small AMD shared-memory systems, large GGUFs can exhaust Vulkan command
    # submission even when their weights fit in system RAM. Make the decision by
    # actual model size so newly added models receive the same protection.
    if (shared_gpu_budget is not None and shared_gpu_budget < 32 * 1024**3
            and (known_gpu_heavy or (model_size_gb is not None and
                 model_size_gb * 1e9 >= shared_gpu_budget * 0.5))):
        if not (is_qwen_27b and rocm_available):
            options['llamacpp_backend'] = 'cpu'
        return options
    if model_key != "devstral-small-2507-gguf":
        return options if (is_qwen_27b or model_key in _LARGE_CONTEXT_FLM_MODELS) else {}
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
