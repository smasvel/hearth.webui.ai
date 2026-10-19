# Qwen3.8-27B text backend profile

This is an engine-development profile for the Qwen3.5-compatible text stack used by
Qwen3.8-27B. It is deliberately not an assertion of official FastFlowLM support.

## Target contract

- Runtime family/tag: `qwen3.5` / `qwen3.5:27b`
- Target: AMD AIE2P/XDNA2
- Initial capability: text only, MTP disabled, 32K validated context
- Hidden/FFN/layers: 5120 / 17408 / 64
- Attention heads: 24 Q, 4 KV
- Linear attention: 16 key heads, 48 value heads, 6144 inner width
- Q4NX tile: 32x256, 5120-byte packed block
- Q8NX tile: 32x256, 8704-byte packed block
- LM head: I8 `[7760,20,8704]`

The current legacy conversion is not compatible with this contract: it has a raw BF16
LM head, old 128-column Q8 packing, and an incomplete tokenizer. Regenerate it with the
corrected converter before attempting kernel validation.

## Readiness boundary

`profile.json` is the source of truth consumed by the web UI and load gate. A package is
loadable only when all of these are true:

1. Q4NX tensor and tokenizer contracts pass.
2. All package/config files exist, including AMD-validated `addr_qk` and `addr_kv`.
3. FLM 1.0.2 or newer is installed.
4. All six language XCLBINs match the hashes in `compatibility.json`.
5. The Qwen, LM-head and Q4 host libraries match the validated ABI hashes.
6. Fixed-logit, deterministic decode and 32K soak gates are recorded as passed.
7. `installation.json` proves the runtime model is a symlink/hard link to this exact
   conversion rather than merely another model registered under the same tag.
8. The schema-2 conversion manifest is finalized with `runtime.ready=true`.

The compatibility and installation templates intentionally contain no approved hashes.
Do not change `validated` or `runtime.ready` merely to clear the UI gate.

## Commands

```bash
cd /home/YOUR_USERNAME/ai_projects/ai-webui
python3 -m unittest discover -s tests -v
tools/check_flm_backend.py /path/to/conversion --allow-incomplete
```

Converter regressions:

```bash
cd /home/YOUR_USERNAME/ai_projects/FLM_Q4NX_Converter
PYTHONPATH=/home/YOUR_USERNAME/ai_projects/llama.cpp/gguf-py:. \
  venv/bin/python -m unittest discover -s tests -v
```

## Kernel project phases

1. Regenerate and freeze the corrected weight/tokenizer package.
2. Obtain the matching FastFlowLM v1.0.2 host ABI or implement a separate IRON host.
3. Build new `layer.xclbin` and `lm_head.xclbin` for the 27B dimensions.
4. Validate or rebuild GateDeltaNet, conv, attention and generic MM helpers using only
   artifacts from the same runtime generation.
5. Run operator goldens, end-to-end token parity, repeated state-reset/load cycles and
   the 32K soak before producing the compatibility attestation.

Copying or renaming the 9B layer/LM-head binaries is never a valid implementation.
