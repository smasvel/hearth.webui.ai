"""Machine-verifiable readiness rules for the experimental Qwen3.8-27B FLM port.

Weight conversion, kernel compilation, and runtime installation are deliberately
separate stages.  A converted Q4NX file must never become loadable merely because
files with familiar XCLBIN names exist: the profile also requires an exact artifact
binding, an ABI compatibility manifest, and recorded numerical/soak validation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable


PROFILE_PATH = Path(__file__).resolve().parent / "flm_profiles/qwen35-27b-text/profile.json"
PROFILE = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
PROFILE_ID = PROFILE["id"]
CONVERTER_PROFILE = PROFILE["converter_profile"]
RUNTIME_TAG = PROFILE["runtime"]["tag"]
RUNTIME_FAMILY = PROFILE["runtime"]["family"]
MIN_RUNTIME_VERSION = PROFILE["runtime"]["minimum_version"]
XCLBIN_DIRECTORY = PROFILE["runtime"]["xclbin_directory"]
COMPATIBILITY_MANIFEST = PROFILE["compatibility_manifest"]

_VERSION_RE = re.compile(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)")
_LAYER_RE = re.compile(r"^model\.layers\.(\d+)\.")
_QUANT_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:IQ\d(?:_[A-Z0-9]+)?|Q\d(?:_[A-Z0-9]+)+|NVFP4|BF16|F16)(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def public_profile() -> dict[str, Any]:
    """Return a detached profile value suitable for JSON responses."""
    return json.loads(json.dumps(PROFILE))


def parse_version(value: str | None) -> tuple[int, int, int] | None:
    match = _VERSION_RE.search(value or "")
    return tuple(map(int, match.groups())) if match else None


def infer_source_quantization(source: str | Path) -> str | None:
    match = _QUANT_RE.search(Path(source).name)
    return match.group(0).upper() if match else None


def kernel_requirements() -> list[dict[str, str]]:
    return [
        {"name": name, "policy": values["policy"], "reason": values["reason"]}
        for name, values in PROFILE["kernels"].items()
    ]


def new_conversion_manifest(source: str | Path, tensor_contract: dict[str, Any],
                            created_at: float) -> dict[str, Any]:
    source = str(Path(source).resolve())
    return {
        "schema": 2,
        "profile": CONVERTER_PROFILE,
        "backend_profile": PROFILE_ID,
        "variant": "text-only",
        "source": {
            "path": source,
            "file": Path(source).name,
            "quantization": infer_source_quantization(source),
        },
        "output": {
            "format": PROFILE["weights"]["format"],
            "quantization": PROFILE["weights"]["quantization"],
            "tensor_contract": tensor_contract,
        },
        "capabilities": dict(PROFILE["capabilities"]),
        "runtime": {
            "family": RUNTIME_FAMILY,
            "tag": RUNTIME_TAG,
            "minimum_version": MIN_RUNTIME_VERSION,
            "ready": False,
        },
        "created_at": created_at,
    }


def _read_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"missing {path.name}"
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"invalid {path.name}: {exc}"
    if not isinstance(value, dict):
        return None, f"invalid {path.name}: expected a JSON object"
    return value, None


def read_q4nx_header(path: Path) -> dict[str, Any]:
    """Read only a safetensors/Q4NX header; tensor payloads are never mapped."""
    with path.open("rb") as stream:
        raw_length = stream.read(8)
        if len(raw_length) != 8:
            raise ValueError("truncated safetensors length")
        (header_length,) = struct.unpack("<Q", raw_length)
        if not 2 <= header_length <= 64 * 1024 * 1024:
            raise ValueError(f"unsafe safetensors header length {header_length}")
        raw_header = stream.read(header_length)
        if len(raw_header) != header_length:
            raise ValueError("truncated safetensors header")
    value = json.loads(raw_header)
    if not isinstance(value, dict):
        raise ValueError("safetensors header is not an object")
    return value


def _expect_tensor(header: dict[str, Any], key: str, dtype: str | None = None,
                   shape: list[int] | None = None) -> str | None:
    value = header.get(key)
    if not isinstance(value, dict):
        return f"missing tensor {key}"
    if dtype is not None and value.get("dtype") != dtype:
        return f"tensor {key} has dtype {value.get('dtype')}, expected {dtype}"
    if shape is not None and value.get("shape") != shape:
        return f"tensor {key} has shape {value.get('shape')}, expected {shape}"
    return None


def inspect_q4nx(path: Path) -> dict[str, Any]:
    """Validate the text-only 27B tensor topology and representative packed shapes."""
    errors: list[str] = []
    try:
        header = read_q4nx_header(path)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        return {"valid": False, "errors": [f"invalid model.q4nx header: {exc}"]}

    keys = {key for key in header if key != "__metadata__"}
    layer_ids = sorted({int(match.group(1)) for key in keys if (match := _LAYER_RE.match(key))})
    expected_layers = list(range(PROFILE["dimensions"]["num_hidden_layers"]))
    if layer_ids != expected_layers:
        errors.append("model.q4nx must contain contiguous layers 0..63")

    fixed = {
        "model.embed_tokens.weight": ("BF16", [248320, 5120]),
        # Official FastFlowLM v1.0.2 Qwen3.5 packages use the Q8NX layout for
        # the vocabulary projection.  A raw BF16 head is not lm_head.xclbin ABI.
        "lm_head.weight": ("I8", [7760, 20, 8704]),
        "model.norm.weight": ("BF16", [5120]),
    }
    for key, (dtype, shape) in fixed.items():
        if error := _expect_tensor(header, key, dtype, shape):
            errors.append(error)

    common_shapes = {
        "input_layernorm.weight": ("BF16", [5120]),
        "post_attention_layernorm.weight": ("BF16", [5120]),
        "mlp.gate_proj.weight": ("I8", [544, 20, 5120]),
        "mlp.up_proj.weight": ("I8", [544, 20, 5120]),
        "mlp.down_proj.weight": ("I8", [160, 68, 5120]),
    }
    linear_shapes = {
        "self_attn.gate_proj.weight": ("I8", [192, 20, 5120]),
        "linear_attn.qkv_proj.weight": ("I8", [320, 20, 5120]),
        "linear_attn.ssm_a": ("F32", [48]),
        "linear_attn.ssm_alpha_proj.weight": ("I8", [2, 20, 8704]),
        "linear_attn.ssm_alpha_proj.bf16.weight": ("BF16", [48, 5120]),
        "linear_attn.ssm_beta_proj.weight": ("I8", [2, 20, 8704]),
        "linear_attn.ssm_beta_proj.bf16.weight": ("BF16", [48, 5120]),
        "linear_attn.ssm_conv1d.weight": ("BF16", [4, 10240]),
        "linear_attn.ssm_dt.bias": ("F32", [48]),
        "linear_attn.ssm_norm.weight": ("BF16", [128]),
        "linear_attn.ssm_out_proj.weight": ("I8", [160, 24, 8704]),
    }
    attention_shapes = {
        "self_attn.q_norm.weight": ("BF16", [256]),
        "self_attn.k_norm.weight": ("BF16", [256]),
        "self_attn.q_proj.weight": ("I8", [384, 20, 5120]),
        "self_attn.k_proj.weight": ("I8", [32, 20, 5120]),
        "self_attn.v_proj.weight": ("I8", [32, 20, 5120]),
        "self_attn.o_proj.weight": ("I8", [160, 24, 5120]),
    }
    full_attention_layers: list[int] = []
    linear_attention_layers: list[int] = []
    for layer in expected_layers:
        prefix = f"model.layers.{layer}."
        shapes = dict(common_shapes)
        if (layer + 1) % PROFILE["dimensions"]["full_attention_interval"] == 0:
            full_attention_layers.append(layer)
            shapes.update(attention_shapes)
        else:
            linear_attention_layers.append(layer)
            shapes.update(linear_shapes)
        for suffix, (dtype, shape) in shapes.items():
            if error := _expect_tensor(header, prefix + suffix, dtype, shape):
                errors.append(error)

    forbidden = sorted(
        key for key in keys
        if any(part in key.lower() for part in ("visual", "vision", "mtp", "draft"))
    )
    if forbidden:
        errors.append("text-only profile contains MTP/vision tensors: " + ", ".join(forbidden[:5]))

    error_count = len(errors)
    if error_count > 32:
        errors = errors[:32] + [f"and {error_count - 32} more tensor-contract error(s)"]
    return {
        "valid": error_count == 0,
        "errors": errors,
        "error_count": error_count,
        "tensor_count": len(keys),
        "layers": len(layer_ids),
        "full_attention_layers": full_attention_layers,
        "linear_attention_layers": linear_attention_layers,
        "has_mtp": any("mtp" in key.lower() or "draft" in key.lower() for key in keys),
        "has_vision": any("visual" in key.lower() or "vision" in key.lower() for key in keys),
    }


def inspect_tokenizer(path: Path) -> dict[str, Any]:
    """Check the chat/control tokens that the GGUF exporter must preserve."""
    tokenizer, error = _read_json(path)
    if error or tokenizer is None:
        return {"valid": False, "errors": [error or "invalid tokenizer.json"]}
    added = {
        item.get("content"): item
        for item in tokenizer.get("added_tokens", [])
        if isinstance(item, dict) and isinstance(item.get("content"), str)
    }
    required = {
        "<|endoftext|>": (248044, True),
        "<|im_start|>": (248045, True),
        "<|im_end|>": (248046, True),
        "<tool_call>": (248058, False),
        "</tool_call>": (248059, False),
        "<tool_response>": (248066, False),
        "</tool_response>": (248067, False),
        "<think>": (248068, False),
        "</think>": (248069, False),
    }
    errors: list[str] = []
    for token, (token_id, special) in required.items():
        entry = added.get(token)
        if not entry:
            errors.append(f"tokenizer.json does not preserve added token {token}")
        elif entry.get("id") != token_id or entry.get("special") is not special:
            errors.append(
                f"tokenizer.json has the wrong ID/type for {token}; expected {token_id}/{special}"
            )
    return {
        "valid": not errors,
        "errors": errors,
        "added_token_count": len(added),
    }


def is_profile_conversion(directory: Path) -> bool:
    manifest, error = _read_json(directory / "conversion.json")
    if error or not manifest:
        return False
    return (
        manifest.get("profile") == CONVERTER_PROFILE
        or manifest.get("backend_profile") == PROFILE_ID
    )


@lru_cache(maxsize=64)
def _sha256_cached(path: str, size: int, mtime_ns: int) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256(path: Path) -> str:
    stat = path.stat()
    return _sha256_cached(str(path), stat.st_size, stat.st_mtime_ns)


def _block(blockers: list[dict[str, str]], code: str, message: str) -> None:
    blockers.append({"code": code, "message": message})


def _validate_config(config: dict[str, Any], blockers: list[dict[str, str]]) -> None:
    if config.get("architectures") != [PROFILE["architecture"]]:
        _block(blockers, "config-architecture", f"config.json must use {PROFILE['architecture']}")
    if config.get("model_type") not in {"qwen3_5", "qwen3_5_text"}:
        _block(blockers, "config-model-type", "config.json must dispatch through qwen3_5_text")
    for field, expected in PROFILE["dimensions"].items():
        if field == "full_attention_interval":
            config_field = field
        else:
            config_field = field
        if config.get(config_field) != expected:
            _block(blockers, "config-dimensions", f"config.json {config_field} must be {expected}")
    for field in PROFILE["config_fields"]:
        if not isinstance(config.get(field), int) or config[field] <= 0:
            _block(blockers, "config-runtime-address", f"config.json needs AMD-validated {field}")
    if config.get("vision_model_weight") or config.get("vision_config"):
        _block(blockers, "config-capability", "text-only profile must not advertise vision weights")


def _manifest_runtime(manifest: dict[str, Any]) -> tuple[str | None, bool]:
    runtime = manifest.get("runtime")
    if isinstance(runtime, dict):
        return runtime.get("tag"), runtime.get("ready") is True
    return manifest.get("runtime_tag"), manifest.get("runtime_ready") is True


def assess_conversion(directory: Path, xclbin_root: Path, installed_tags: Iterable[str],
                      runtime_version: str | None) -> dict[str, Any]:
    """Return structured blockers.  ``ready`` is true only when every gate passes."""
    directory = directory.resolve()
    flm_dir = xclbin_root.resolve().parent
    xclbin_dir = (xclbin_root / XCLBIN_DIRECTORY).resolve()
    installed_tags = set(installed_tags)
    blockers: list[dict[str, str]] = []
    warnings: list[str] = []

    manifest, manifest_error = _read_json(directory / "conversion.json")
    if manifest_error:
        _block(blockers, "conversion-manifest", manifest_error)
        manifest = {}
    elif manifest.get("profile") != CONVERTER_PROFILE and manifest.get("backend_profile") != PROFILE_ID:
        _block(blockers, "conversion-profile", f"conversion.json is not {PROFILE_ID}")
    if manifest and manifest.get("schema") != 2:
        warnings.append("legacy conversion manifest; reconvert or migrate to schema 2")

    source_value = manifest.get("source") if manifest else None
    source = source_value.get("path") if isinstance(source_value, dict) else source_value
    source_quantization = infer_source_quantization(source) if source else None
    if source_quantization and source_quantization != "NVFP4":
        warnings.append(
            f"source filename indicates {source_quantization}; this artifact is not an NVFP4 runtime profile"
        )

    for filename in PROFILE["package_files"]:
        path = directory / filename
        if not path.is_file() or path.stat().st_size <= 0:
            _block(blockers, "package-file", f"missing non-empty {filename} in {directory}")

    weights = directory / "model.q4nx"
    tensor_contract: dict[str, Any] | None = None
    if weights.is_file() and weights.stat().st_size > 0:
        tensor_contract = inspect_q4nx(weights)
        for error in tensor_contract.get("errors", []):
            _block(blockers, "tensor-contract", error)

    tokenizer = directory / "tokenizer.json"
    tokenizer_contract: dict[str, Any] | None = None
    if tokenizer.is_file() and tokenizer.stat().st_size > 0:
        tokenizer_contract = inspect_tokenizer(tokenizer)
        for error in tokenizer_contract.get("errors", []):
            _block(blockers, "tokenizer-contract", error)

    config, config_error = _read_json(directory / "config.json")
    if config_error is None and config is not None:
        _validate_config(config, blockers)

    parsed_runtime = parse_version(runtime_version)
    minimum_runtime = parse_version(MIN_RUNTIME_VERSION)
    if parsed_runtime is None:
        _block(blockers, "runtime-version", "could not verify the installed FLM version")
    elif minimum_runtime and parsed_runtime < minimum_runtime:
        _block(
            blockers, "runtime-version",
            f"FLM {runtime_version} is too old; {MIN_RUNTIME_VERSION}+ is required",
        )

    kernel_state: list[dict[str, Any]] = []
    for requirement in kernel_requirements():
        path = xclbin_dir / requirement["name"]
        exists = path.is_file() and path.stat().st_size > 0
        kernel_state.append({**requirement, "path": str(path), "exists": exists, "validated": False})
        if not exists:
            _block(blockers, "kernel-file", f"missing non-empty {requirement['name']} in {xclbin_dir}")

    host_state: list[dict[str, Any]] = []
    compatibility, compatibility_error = _read_json(xclbin_dir / COMPATIBILITY_MANIFEST)
    if compatibility_error:
        _block(blockers, "kernel-compatibility", compatibility_error)
        compatibility = {}
    else:
        if compatibility.get("schema") != 1 or compatibility.get("profile") != PROFILE_ID:
            _block(blockers, "kernel-compatibility", "compatibility.json has the wrong schema/profile")
        if compatibility.get("target") != "aie2p":
            _block(blockers, "kernel-target", "compatibility.json target must be aie2p/XDNA2")
        if compatibility.get("runtime_abi") != PROFILE["runtime"]["abi"]:
            _block(blockers, "runtime-abi", "FastFlowLM Qwen3.5 runtime ABI is not validated")
        host_proofs = compatibility.get("host_libraries")
        host_proofs = host_proofs if isinstance(host_proofs, dict) else {}
        for name in PROFILE["runtime"]["host_libraries"]:
            path = flm_dir / "lib" / name
            exists = path.is_file() and path.stat().st_size > 0
            proof = host_proofs.get(name)
            proof = proof if isinstance(proof, dict) else {}
            expected_hash = proof.get("sha256")
            validated = proof.get("validated") is True
            host_state.append({
                "name": name, "path": str(path), "exists": exists,
                "validated": validated, "sha256": expected_hash,
            })
            if not exists:
                _block(blockers, "host-library", f"missing non-empty runtime library {path}")
            elif not validated or not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
                _block(blockers, "host-library", f"{name} has no validated runtime ABI hash")
            elif _sha256(path) != expected_hash:
                _block(blockers, "host-library", f"{name} does not match its validated SHA-256")
        if compatibility.get("dimensions") != PROFILE["dimensions"]:
            _block(blockers, "kernel-dimensions", "kernel dimensions do not match the 27B profile")

        kernels = compatibility.get("kernels")
        kernels = kernels if isinstance(kernels, dict) else {}
        by_name = {entry["name"]: entry for entry in kernel_state}
        for name, entry in by_name.items():
            proof = kernels.get(name)
            if not isinstance(proof, dict) or proof.get("validated") is not True:
                _block(blockers, "kernel-validation", f"{name} has no validated compatibility record")
                continue
            expected_hash = proof.get("sha256")
            path = Path(entry["path"])
            if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
                _block(blockers, "kernel-hash", f"{name} has no valid SHA-256 record")
            elif entry["exists"] and _sha256(path) != expected_hash:
                _block(blockers, "kernel-hash", f"{name} does not match its validated SHA-256")
            else:
                entry["validated"] = entry["exists"]

        validation = compatibility.get("validation")
        validation = validation if isinstance(validation, dict) else {}
        if validation.get("status") != "passed":
            _block(blockers, "acceptance-tests", "kernel acceptance status is not passed")
        results = validation.get("gates")
        results = results if isinstance(results, dict) else {}
        for gate in PROFILE["validation_gates"]:
            if results.get(gate) is not True:
                _block(blockers, "acceptance-tests", f"validation gate not passed: {gate}")

    installation, installation_error = _read_json(directory / "installation.json")
    if installation_error is None and installation is not None:
        if installation.get("schema") != 1 or installation.get("profile") != PROFILE_ID:
            _block(blockers, "artifact-binding", "installation.json has the wrong schema/profile")
        if installation.get("runtime_tag") != RUNTIME_TAG:
            _block(blockers, "artifact-binding", f"installation.json must bind {RUNTIME_TAG}")
        installed_weight = installation.get("installed_weight")
        try:
            exact_binding = bool(installed_weight) and os.path.samefile(weights, Path(installed_weight))
        except OSError:
            exact_binding = False
        if not exact_binding:
            _block(
                blockers, "artifact-binding",
                "runtime model.q4nx must be a symlink or hard link to this exact converted artifact",
            )

    if RUNTIME_TAG not in installed_tags:
        _block(blockers, "runtime-catalog", f"FLM runtime catalog has no installed {RUNTIME_TAG}")
    manifest_tag, manifest_ready = _manifest_runtime(manifest)
    if manifest_tag != RUNTIME_TAG:
        _block(blockers, "runtime-tag", f"conversion manifest must target {RUNTIME_TAG}")
    if not manifest_ready:
        _block(blockers, "runtime-ready", "conversion manifest has not been finalized by the installer")

    return {
        "ready": not blockers,
        "profile": PROFILE_ID,
        "display_name": PROFILE["display_name"],
        "directory": str(directory),
        "weights": str(weights),
        "runtime_tag": RUNTIME_TAG,
        "runtime_family": RUNTIME_FAMILY,
        "runtime_version": runtime_version,
        "minimum_runtime_version": MIN_RUNTIME_VERSION,
        "xclbin_directory": str(xclbin_dir),
        "source": source,
        "source_quantization": source_quantization,
        "capabilities": dict(PROFILE["capabilities"]),
        "tensor_contract": tensor_contract,
        "tokenizer_contract": tokenizer_contract,
        "kernels": kernel_state,
        "host_libraries": host_state,
        "blockers": blockers,
        "warnings": warnings,
    }


def blocker_message(report: dict[str, Any], limit: int | None = None) -> str:
    messages = [entry["message"] for entry in report.get("blockers", [])]
    if limit is not None and len(messages) > limit:
        messages = messages[:limit] + [f"and {len(report['blockers']) - limit} more blocker(s)"]
    return "Not loadable yet: " + "; ".join(messages) if messages else ""
