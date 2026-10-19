"""Model-specific llama.cpp template overrides for Lemonade load requests."""
import shlex
from pathlib import Path


def _small_amd_memory_budget() -> bool:
    """The 23.8 GB Nemotron quant exhausts this APU's ~23 GB GTT pool."""
    for path in Path('/sys/class/drm').glob('card*/device/mem_info_gtt_total'):
        try:
            budget = int(path.read_text().strip())
        except (OSError, ValueError):
            continue
        if 0 < budget < 32 * 1024**3:
            return True
    return False


def load_options(model_id: str, current: dict | None = None) -> dict:
    if (model_id.lower() == 'nvidia-nemotron-3.5-lightning-30b-a3b-gguf'
            and _small_amd_memory_budget()):
        # Vulkan fails with ErrorDeviceLost even with partial offloading. CPU
        # loading and inference were verified at 65,536 context on this APU.
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
        template = (Path(__file__).resolve().parent / "chat_templates" / "devstral-small-2507.jinja").read_text().strip()
        args.extend(["--chat-template", template])
    options["llamacpp_args"] = shlex.join(args)
    return options
