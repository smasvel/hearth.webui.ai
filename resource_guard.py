"""Shared pre-flight memory guard, checked before every backend's model load in app.py.

Real incident (2026-07-26): Lemonade had its own copy of qwen3.6-moe:35b-a3b loaded (26GB)
at the same time flm.service's standalone copy - the one behind the OpenCode/Continue
tool-calling shim on Caddy's :8447 - needed to load the same model to serve a file-creation
request. Together they exceeded this machine's 43GB of RAM and the kernel OOM-killed
flm.service outright, breaking the in-flight tool call. The existing per-backend size checks
(lemonade_client.check_safe_to_load, vllm_client.check_safe_to_load) only compare a model's
OWN file size against TOTAL system RAM - they say nothing about what else is ALREADY loaded,
and can't even evaluate NPU/flm models at all (bare tags like "qwen3.6-moe:35b-a3b" have no
Hugging-Face-repo shape to size, so those checks always wave them through as "safe").

This is a second, backend-agnostic layer that doesn't need to know the incoming model's size
at all: it just refuses when there's already too little RAM free right now to safely start
another load, regardless of which backend or model is asking, or why memory is already tight
(another backend's duplicate load, a memory-hungry unrelated process, etc.).
"""

from __future__ import annotations

import psutil

_MIN_AVAILABLE_GB = 6.0


def check_available_memory() -> str | None:
    """Returns a human-readable refusal message if available RAM is already too low to
    safely start another model load, None if there's enough headroom. Uses psutil's
    `available` figure (accounts for reclaimable page cache, unlike the raw `free` column)
    so ordinary disk-cache usage doesn't trigger a false refusal - only genuine memory
    pressure does. Deliberately checked right before the actual load call (after any
    same-backend eviction, e.g. app.py's _free_npu_except), so freed memory from that
    eviction counts toward the available figure."""
    available_gb = psutil.virtual_memory().available / 1e9
    if available_gb >= _MIN_AVAILABLE_GB:
        return None
    return (
        f"Refusing to load: only {available_gb:.1f}GB of RAM is free right now (below the "
        f"{_MIN_AVAILABLE_GB:.0f}GB safety floor). Loading another model on top of whatever's "
        f"already using memory risks the kernel OOM-killing a backend mid-request - this "
        f"happened for real (flm.service got OOM-killed three times when Lemonade had its own "
        f"duplicate copy of the same NPU model loaded at the same time). Unload something "
        f"unused first, then try again."
    )
