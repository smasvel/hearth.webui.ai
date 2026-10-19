import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path

import flm_backend_profile as profile


def tensor(dtype, shape):
    return {"dtype": dtype, "shape": shape, "data_offsets": [0, 0]}


def write_valid_q4nx(path):
    header = {
        "model.embed_tokens.weight": tensor("BF16", [248320, 5120]),
        "lm_head.weight": tensor("I8", [7760, 20, 8704]),
        "model.norm.weight": tensor("BF16", [5120]),
    }
    common = {
        "input_layernorm.weight": ("BF16", [5120]),
        "post_attention_layernorm.weight": ("BF16", [5120]),
        "mlp.gate_proj.weight": ("I8", [544, 20, 5120]),
        "mlp.up_proj.weight": ("I8", [544, 20, 5120]),
        "mlp.down_proj.weight": ("I8", [160, 68, 5120]),
    }
    linear = {
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
    attention = {
        "self_attn.q_norm.weight": ("BF16", [256]),
        "self_attn.k_norm.weight": ("BF16", [256]),
        "self_attn.q_proj.weight": ("I8", [384, 20, 5120]),
        "self_attn.k_proj.weight": ("I8", [32, 20, 5120]),
        "self_attn.v_proj.weight": ("I8", [32, 20, 5120]),
        "self_attn.o_proj.weight": ("I8", [160, 24, 5120]),
    }
    for layer in range(64):
        shapes = {**common, **(attention if layer % 4 == 3 else linear)}
        for suffix, (dtype, shape) in shapes.items():
            header[f"model.layers.{layer}.{suffix}"] = tensor(dtype, shape)
    encoded = json.dumps(header, separators=(",", ":")).encode()
    path.write_bytes(struct.pack("<Q", len(encoded)) + encoded)


def write_valid_tokenizer(path):
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
    path.write_text(json.dumps({"added_tokens": [
        {"content": token, "id": token_id, "special": special}
        for token, (token_id, special) in required.items()
    ]}))


def write_ready_fixture(root):
    conversion = root / "conversion"
    xclbin_root = root / "xclbins"
    kernels_dir = xclbin_root / profile.XCLBIN_DIRECTORY
    conversion.mkdir()
    kernels_dir.mkdir(parents=True)
    write_valid_q4nx(conversion / "model.q4nx")
    write_valid_tokenizer(conversion / "tokenizer.json")
    (conversion / "tokenizer_config.json").write_text("{}")
    (conversion / "chat_template.jinja").write_text("{{ messages }}")

    config = {
        "architectures": [profile.PROFILE["architecture"]],
        "model_type": "qwen3_5_text",
        **profile.PROFILE["dimensions"],
        "addr_qk": 1,
        "addr_kv": 2,
    }
    (conversion / "config.json").write_text(json.dumps(config))
    manifest = profile.new_conversion_manifest(
        "/models/source-Q4_K_S.gguf", profile.inspect_q4nx(conversion / "model.q4nx"), 1.0,
    )
    manifest["runtime"]["ready"] = True
    (conversion / "conversion.json").write_text(json.dumps(manifest))
    (conversion / "installation.json").write_text(json.dumps({
        "schema": 1,
        "profile": profile.PROFILE_ID,
        "runtime_tag": profile.RUNTIME_TAG,
        "installed_weight": str(conversion / "model.q4nx"),
        "binding": "symlink-or-hardlink",
        "ready": True,
    }))

    kernel_records = {}
    for requirement in profile.kernel_requirements():
        payload = (requirement["name"] + "-test").encode()
        kernel_path = kernels_dir / requirement["name"]
        kernel_path.write_bytes(payload)
        kernel_records[requirement["name"]] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "provenance": "test",
            "validated": True,
        }
    host_records = {}
    lib_dir = root / "lib"
    lib_dir.mkdir()
    for name in profile.PROFILE["runtime"]["host_libraries"]:
        payload = (name + "-test").encode()
        (lib_dir / name).write_bytes(payload)
        host_records[name] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "validated": True,
        }
    compatibility = {
        "schema": 1,
        "profile": profile.PROFILE_ID,
        "target": "aie2p",
        "runtime_abi": profile.PROFILE["runtime"]["abi"],
        "host_libraries": host_records,
        "dimensions": profile.PROFILE["dimensions"],
        "kernels": kernel_records,
        "validation": {
            "status": "passed",
            "gates": {gate: True for gate in profile.PROFILE["validation_gates"]},
        },
    }
    (kernels_dir / profile.COMPATIBILITY_MANIFEST).write_text(json.dumps(compatibility))
    return conversion, xclbin_root


class FLMBackendProfileTests(unittest.TestCase):
    def test_version_and_source_quantization(self):
        self.assertEqual(profile.parse_version("FLM v1.0.2"), (1, 0, 2))
        self.assertEqual(profile.infer_source_quantization("model-Q4_K_S.gguf"), "Q4_K_S")

    def test_complete_fixture_is_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            conversion, xclbins = write_ready_fixture(Path(tmp))
            report = profile.assess_conversion(
                conversion, xclbins, {profile.RUNTIME_TAG}, "1.0.2",
            )
            self.assertTrue(report["ready"], report["blockers"])

    def test_kernel_hash_mismatch_blocks_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            conversion, xclbins = write_ready_fixture(Path(tmp))
            (xclbins / profile.XCLBIN_DIRECTORY / "layer.xclbin").write_bytes(b"changed")
            report = profile.assess_conversion(
                conversion, xclbins, {profile.RUNTIME_TAG}, "1.0.2",
            )
            self.assertFalse(report["ready"])
            self.assertIn("kernel-hash", {item["code"] for item in report["blockers"]})

    def test_raw_bf16_lm_head_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "model.q4nx"
            write_valid_q4nx(model)
            header = profile.read_q4nx_header(model)
            header["lm_head.weight"] = tensor("BF16", [248320, 5120])
            encoded = json.dumps(header, separators=(",", ":")).encode()
            model.write_bytes(struct.pack("<Q", len(encoded)) + encoded)
            report = profile.inspect_q4nx(model)
            self.assertFalse(report["valid"])
            self.assertTrue(any("lm_head.weight has dtype BF16" in error for error in report["errors"]))

    def test_missing_chat_tokens_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tokenizer = Path(tmp) / "tokenizer.json"
            tokenizer.write_text(json.dumps({"added_tokens": []}))
            report = profile.inspect_tokenizer(tokenizer)
            self.assertFalse(report["valid"])
            self.assertTrue(any("<|im_start|>" in error for error in report["errors"]))


if __name__ == "__main__":
    unittest.main()
