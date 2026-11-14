"""Exercise Hearth's backend paths on a given side.

Run inside the container for the Docker Hearth instance and on the host for the host
Hearth instance, so each side is exercised one by one with the same code path."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _side_test_helpers import as_admin, csrf, patch_admin, report  # noqa: E402


def main() -> None:
    from hearth.backends import kobold_client as kobold
    from hearth.backends import lemonade_client as lemonade
    from hearth.backends import lmstudio_client as lms
    from hearth.backends import vllm_client as vllm
    from hearth.backends import fastflowlm_client as flm
    import app
    from hearth.backends import kobold_client

    print("== backend inventory ==")
    for label, client in (
        ("vLLM", vllm), ("KoboldCPP", kobold), ("LM Studio", lms),
        ("Lemonade", lemonade), ("FastFlowLM", flm),
    ):
        try:
            models = client.list_models()
        except Exception as exc:
            report(f"{label}", f"error: {type(exc).__name__}: {str(exc)[:110]}")
            continue
        report(f"{label}", [m.get("id") for m in models][:6])

    print()
    print("== vLLM load / unload ==")
    model = "TinyLlama/TinyLlama-1.1B-Chat-v1.0"
    served = vllm._current_served_model()
    report("served before", served if served else "nothing")
    if served is not None:
        vllm.unload_model(model)
        report("service after unload", vllm._systemctl("is-active"))
    vllm.load_model(model, max_model_len=2048)
    report("served after load", vllm._current_served_model())
    report("context window", vllm._served_max_model_len())
    report("service.env", vllm._SERVICE_ENV_FILE.read_text().replace("\n", " | "))

    print()
    print("== KoboldCPP load / unload ==")
    gguf = os.path.expanduser(
        "~/.lmstudio/models/empero-ai/Qwen3.8-2B-Distill-GGUF/Qwen3.8-2B-Q8_0.gguf"
    )
    kobold_model = "koboldcpp/Qwen3.8-2B-Q8_0"
    if kobold_model not in kobold._load_registry():
        kobold._register(gguf)
        report("registered", "koboldcpp/Qwen3.8-2B-Q8_0")
    kobold.unload_model()
    report("service after unload", kobold._systemctl("is-active"))
    kobold.load_model(kobold_model)
    report("served after load", kobold.current_served_model())
    report("service.env path", kobold._read_env_path())

    print()
    print("== LM Studio load / unload ==")
    lms_model = "qwen3.8-2b-distill"
    lms.load_model(lms_model, context_length=8192)
    report("loaded", [(m.get("id"), m.get("loaded_context_length")) for m in lms.list_models() if m.get("loaded_context_length")])
    lms.unload_model(lms_model)
    report("after unload", [(m.get("id"), m.get("loaded_context_length")) for m in lms.list_models() if m.get("loaded_context_length")])

    print()
    print("== FastFlowLM load / unload ==")
    flm_model = "flm/llama3.2:1b"
    flm.unload_model()
    report("active after unload", flm.is_active())
    flm.load_model(flm_model)
    report("active after load", flm.is_active())
    report("active tag", flm._read_active())
    flm.unload_model()
    report("active after final unload", flm.is_active())

    print()
    print("== Lemonade load / unload (embedding) ==")
    lemonade.load_model(lemonade.EMBEDDING_MODEL_ID)
    report("embedding loaded", lemonade.is_model_loaded(lemonade.EMBEDDING_MODEL_ID))
    lemonade.unload_model(lemonade.EMBEDDING_MODEL_ID)
    report("embedding after unload", lemonade.is_model_loaded(lemonade.EMBEDDING_MODEL_ID))

    print()
    print("== chat streaming ==")
    lm_chat_model = "qwen3.8-2b-distill"
    lms.load_model(lm_chat_model, context_length=8192)
    lines = list(
        lms.chat_stream(
            [
                {"role": "system", "content": "Reply exactly: Ready"},
                {"role": "user", "content": "ping"},
            ],
            lm_chat_model,
            temperature=0.0,
            max_tokens=64,
        )
    )
    report(f"lmstudio chat:{len(lines)} sse lines", lines[0][:140])
    try:
        kob_served = kobold.current_served_model()
        if kob_served:
            kot_lines = list(
                kobold.chat_stream(
                    [{"role": "user", "content": "ping"}], kob_served, temperature=0.0, max_tokens=32
                )
            )
            report(f"kobold chat:{len(kot_lines)} sse lines", kot_lines[0][:140])
    except Exception as exc:
        report("kobold chat", f"unreachable: {str(exc)[:140]}")
    lms.unload_model(lm_chat_model)

    print()
    print("== Hearth API round-trips ==")
    up = patch_admin()
    up.start()
    client = as_admin()
    payload_model = kobold_model
    r = client.post("/api/models/load", json={"model": payload_model}, headers=csrf())
    report("POST /api/models/load", f"{r.status_code} {r.get_json()}")
    r = client.get("/api/models", headers=csrf())
    models = (r.get_json() or {}).get("models") or []
    entry = next((m for m in models if m.get("id") == payload_model), None)
    report("GET /api/models", f"{r.status_code} ({'in list' if entry else 'missing'}; state={entry and entry.get('state')})")
    r = client.post("/api/models/unload", json={"identifier": payload_model}, headers=csrf())
    report("POST /api/models/unload", f"{r.status_code} {r.get_json()}")
    up.stop()

    print()
    print("== broker-side systemctl from this instance ==")
    try:
        report("vllm is-active", vllm._systemctl("is-active"))
        report("kobold is-active", kobold._systemctl("is-active"))
        report("flm is-active (system)", flm._systemctl("is-active"))
    except Exception as exc:
        report("broker-side systemctl", f"{type(exc).__name__}: {str(exc)[:140]}")


if __name__ == "__main__":
    main()
