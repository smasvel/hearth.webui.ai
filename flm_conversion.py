"""Admin-facing GGUF -> FLM/Q4NX conversion jobs.

The converter is intentionally isolated from the live FLM model registry: producing
weights does not imply that dimension-matched NPU kernels exist for the model.
"""

from __future__ import annotations

import os
import json
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import flm_backend_profile as backend_profile


CONVERTER_ROOT = Path(os.environ.get(
    "FLM_CONVERTER_ROOT", str(Path.home() / ".local/share/hearth/FLM_Q4NX_Converter")
)).resolve()
CONVERTER_PYTHON = Path(os.environ.get(
    "FLM_CONVERTER_PYTHON", str(CONVERTER_ROOT / "venv/bin/python")
)).expanduser().absolute()
GGUF_PY = Path(os.environ.get(
    "FLM_GGUF_PY", str(Path.home() / ".local/share/hearth/llama.cpp/gguf-py")
)).resolve()
OUTPUT_ROOT = Path(os.environ.get(
    "FLM_CONVERSION_OUTPUT_ROOT", str(Path.home() / ".config/flm/conversions")
)).resolve()
SOURCE_ROOTS = tuple(Path(value).expanduser().resolve() for value in os.environ.get(
    "FLM_GGUF_SOURCE_ROOTS", str(Path.home() / ".lmstudio/models")
).split(os.pathsep) if value.strip())

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_jobs: dict[str, dict] = {}
_processes: dict[str, subprocess.Popen] = {}
_lock = threading.RLock()


class ConversionError(ValueError):
    pass


def _environment() -> dict[str, str]:
    env = os.environ.copy()
    additions = [str(GGUF_PY), str(CONVERTER_ROOT)]
    existing = env.get("PYTHONPATH")
    if existing:
        additions.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(additions)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def availability() -> dict:
    missing = []
    for path, label in (
        (CONVERTER_ROOT / "convert.py", "converter"),
        (CONVERTER_ROOT / "validate_profile.py", "profile validator"),
        (CONVERTER_PYTHON, "converter Python environment"),
        (GGUF_PY, "GGUF reader"),
    ):
        if not path.exists():
            missing.append(f"{label}: {path}")
    return {
        "available": not missing,
        "missing": missing,
        "output_root": str(OUTPUT_ROOT),
        "supported_profile": backend_profile.CONVERTER_PROFILE,
        "backend_profile": backend_profile.public_profile(),
    }


def _source_path(raw_path: str) -> Path:
    if not raw_path:
        raise ConversionError("Select a GGUF source model.")
    path = Path(raw_path).expanduser().resolve()
    if path.suffix.lower() != ".gguf" or not path.is_file():
        raise ConversionError("The selected source is not a local GGUF file.")
    if not any(path.is_relative_to(root) for root in SOURCE_ROOTS):
        raise ConversionError("The selected file is outside the configured model directories.")
    return path


def discover_models() -> list[dict]:
    models = []
    seen = set()
    for root in SOURCE_ROOTS:
        if not root.is_dir():
            continue
        for path in root.rglob("*.gguf"):
            try:
                resolved = path.resolve()
                if resolved in seen or not resolved.is_file() or not resolved.is_relative_to(root):
                    continue
                stat = resolved.stat()
            except OSError:
                continue
            seen.add(resolved)
            models.append({
                "path": str(resolved),
                "name": resolved.name,
                "location": str(resolved.parent),
                "size_bytes": stat.st_size,
                "size_gb": round(stat.st_size / (1024 ** 3), 2),
            })
    return sorted(models, key=lambda item: item["name"].lower())


def validate_model(raw_path: str) -> dict:
    source = _source_path(raw_path)
    ready = availability()
    if not ready["available"]:
        raise ConversionError("Converter is unavailable: " + "; ".join(ready["missing"]))
    try:
        result = subprocess.run(
            [str(CONVERTER_PYTHON), str(CONVERTER_ROOT / "validate_profile.py"),
             str(source), "--json"],
            cwd=CONVERTER_ROOT,
            env=_environment(),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ConversionError("Model validation timed out after 3 minutes.") from exc
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    try:
        validation = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        validation = None
    if result.returncode or not isinstance(validation, dict) or not validation.get("eligible"):
        tail = "\n".join(output.splitlines()[-12:])
        raise ConversionError("This GGUF is not eligible for the 27B FLM profile.\n" + tail)
    hidden = int(validation["hidden_size"])
    intermediate = int(validation["intermediate_size"])
    layers = int(validation["layers"])
    tensors = int(validation["tensors"])
    unmapped = int(validation["unmapped"])
    missing = int(validation.get("missing", -1))
    if validation.get("profile") != backend_profile.CONVERTER_PROFILE or unmapped != 0 or missing != 0:
        raise ConversionError("The GGUF does not have complete QWEN35_27B tensor coverage.")
    expected = backend_profile.PROFILE["dimensions"]
    actual = (hidden, intermediate, layers)
    wanted = (
        expected["hidden_size"], expected["intermediate_size"],
        expected["num_hidden_layers"],
    )
    if actual != wanted:
        raise ConversionError(
            "The GGUF was classified as QWEN35_27B but has the wrong dimensions: "
            f"hidden/intermediate/layers={actual}; expected {wanted}."
        )
    return {
        "eligible": True,
        "source": str(source),
        "profile": validation["profile"],
        "hidden_size": hidden,
        "intermediate_size": intermediate,
        "layers": layers,
        "tensors": tensors,
        "unmapped": unmapped,
        "missing": missing,
        "runtime_ready": False,
        "kernel_status": {
            "ready": False,
            "missing": [item["name"] for item in backend_profile.kernel_requirements()],
            "requirements": backend_profile.kernel_requirements(),
            "manifest": str(backend_profile.PROFILE_PATH),
            "message": "Weights can be converted, but all six NPU kernels and the FastFlowLM runtime ABI must be validated for this 27B profile.",
        },
    }


def _snapshot(job: dict) -> dict:
    return {key: value for key, value in job.items() if not key.startswith("_")}


def list_jobs() -> list[dict]:
    with _lock:
        return [_snapshot(job) for job in sorted(_jobs.values(), key=lambda j: j["created_at"], reverse=True)]


def get_job(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return _snapshot(job) if job else None


def _update(job_id: str, **fields) -> None:
    with _lock:
        if job_id in _jobs:
            _jobs[job_id].update(fields)
            _jobs[job_id]["updated_at"] = time.time()


def _append_log(job_id: str, line: str) -> None:
    line = line.rstrip()
    if not line:
        return
    with _lock:
        job = _jobs[job_id]
        job["logs"] = (job["logs"] + [line])[-250:]
        if "Processing tensor:" in line:
            job["processed_tensors"] += 1
            job["progress"] = min(95, 3 + round(92 * job["processed_tensors"] / job["total_tensors"]))
        job["updated_at"] = time.time()


def _run(job_id: str) -> None:
    with _lock:
        job = _jobs[job_id]
        source, output = job["source"], job["output"]
        staging_output = job["staging_output"]
        if job.get("cancel_requested"):
            job["state"] = "cancelled"
            job["detail"] = "Cancelled before conversion started."
            job["updated_at"] = time.time()
            return
    try:
        Path(staging_output).mkdir(parents=True, exist_ok=False)
        proc = subprocess.Popen(
            [str(CONVERTER_PYTHON), "-u", str(CONVERTER_ROOT / "convert.py"),
             "-i", source, "-o", staging_output],
            cwd=CONVERTER_ROOT,
            env=_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        with _lock:
            _processes[job_id] = proc
        _update(job_id, state="running", detail="Converting tensors", progress=3)
        assert proc.stdout is not None
        for line in proc.stdout:
            _append_log(job_id, line)
        returncode = proc.wait()
        with _lock:
            cancelled = _jobs[job_id].get("cancel_requested", False)
        if cancelled:
            _update(job_id, state="cancelled", detail="Cancelled; partial output was preserved.")
        elif returncode == 0 and (Path(staging_output) / "model.q4nx").is_file():
            tensor_contract = backend_profile.inspect_q4nx(Path(staging_output) / "model.q4nx")
            tokenizer_contract = backend_profile.inspect_tokenizer(Path(staging_output) / "tokenizer.json")
            contract_errors = tensor_contract["errors"] + tokenizer_contract["errors"]
            if contract_errors:
                for error in contract_errors:
                    _append_log(job_id, f"CONTRACT ERROR: {error}")
                _update(
                    job_id, state="failed",
                    detail="Conversion output failed the QWEN35_27B_TEXT tensor contract.",
                )
                return
            tensor_contract["tokenizer"] = tokenizer_contract
            manifest = backend_profile.new_conversion_manifest(source, tensor_contract, time.time())
            (Path(staging_output) / "conversion.json").write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
            )
            if Path(output).exists():
                raise ConversionError(
                    f"Final output appeared while conversion was running: {output}"
                )
            Path(staging_output).replace(output)
            _update(job_id, state="complete", progress=100,
                    detail="Weights and tensor contract validated. NPU kernel/ABI validation is still required.")
        else:
            _update(job_id, state="failed", detail=f"Converter exited with status {returncode}.")
    except Exception as exc:
        _append_log(job_id, f"ERROR: {exc}")
        _update(job_id, state="failed", detail=str(exc))
    finally:
        with _lock:
            _processes.pop(job_id, None)


def start_job(raw_path: str, name: str, acknowledge_unrunnable: bool) -> dict:
    if not _NAME_RE.fullmatch(name or ""):
        raise ConversionError("Output name must use 1-100 letters, numbers, dots, dashes, or underscores.")
    if not acknowledge_unrunnable:
        raise ConversionError("Acknowledge that the 27B weights are not runnable until NPU kernels are compiled.")
    validation = validate_model(raw_path)
    source = Path(validation["source"])
    output = (OUTPUT_ROOT / name).resolve()
    if not output.is_relative_to(OUTPUT_ROOT):
        raise ConversionError("Invalid output directory.")
    if output.is_dir() and not any(output.iterdir()):
        # The converter creates its directory before processing and writes model.q4nx
        # only at final export. A failed early conversion therefore leaves an empty,
        # server-owned directory. Remove that harmless shell so Retry can reuse the
        # same output name; never remove a directory containing artifacts.
        output.rmdir()
    if output.exists():
        raise ConversionError(f"Output already exists and is not empty: {output}")
    with _lock:
        if any(job["state"] in {"queued", "running", "cancelling"} for job in _jobs.values()):
            raise ConversionError("Another FLM conversion is already running on this machine.")
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        required = int(source.stat().st_size * 1.1)
        if shutil.disk_usage(OUTPUT_ROOT).free < required:
            raise ConversionError("Not enough free disk space for this conversion.")
        job_id = uuid.uuid4().hex
        staging_output = OUTPUT_ROOT / f".{name}.partial-{job_id}"
        now = time.time()
        _jobs[job_id] = {
            "id": job_id, "name": name, "source": str(source), "output": str(output),
            "staging_output": str(staging_output),
            "profile": validation["profile"], "state": "queued", "progress": 1,
            "detail": "Validated; waiting for converter", "processed_tensors": 0,
            "total_tensors": validation["tensors"], "runtime_ready": False,
            "kernel_status": validation["kernel_status"], "logs": [],
            "created_at": now, "updated_at": now, "cancel_requested": False,
        }
        thread = threading.Thread(target=_run, args=(job_id,), daemon=True)
        thread.start()
        return _snapshot(_jobs[job_id])


def cancel_job(job_id: str) -> dict:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            raise ConversionError("Conversion job not found.")
        if job["state"] not in {"queued", "running", "cancelling"}:
            return _snapshot(job)
        job["cancel_requested"] = True
        job["state"] = "cancelling"
        job["detail"] = "Stopping converter"
        proc = _processes.get(job_id)
    if proc and proc.poll() is None:
        proc.terminate()
    return get_job(job_id)
