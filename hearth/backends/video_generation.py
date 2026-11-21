"""Wan 2.2 text-to-video through the Vulkan sd.cpp bundled with Lemonade.

Lemonade's public HTTP API does not currently expose video generation. The
installed Lemonade container does ship sd.cpp, so this adapter runs its CLI
inside that container and stores downloaded weights in Lemonade's shared HF
cache. No ComfyUI service or external inference API is used.
"""

from __future__ import annotations

import os
import subprocess
import threading
import uuid
from functools import lru_cache
from pathlib import Path

from huggingface_hub import hf_hub_download


class VideoGenerationError(RuntimeError):
    pass


CONTAINER = os.environ.get("HEARTH_VIDEO_CONTAINER", "lemonade-server")
CONTAINER_HF_CACHE = "/opt/lemonade/.cache/huggingface"
MODEL_REPO = "off-grid-ai/Wan2.2-TI2V-5B-GGUF"
MODEL_FILE = "Wan2.2-TI2V-5B-Q4_K_S.gguf"
VAE_FILE = "wan2.2_vae.safetensors"
T5_REPO = "off-grid-ai/Video-Companions-GGUF"
T5_FILE = "umt5-xxl-encoder-Q4_K_M.gguf"
HF_CACHE = Path(os.environ.get("LEMONADE_HF_CACHE", str(Path.home() / "lemonade-hf-cache")))
if not (HF_CACHE / "hub").is_dir():
    HF_CACHE = Path(os.environ.get("HF_HOME", str(Path.home() / ".cache/huggingface")))

_generation_lock = threading.Lock()
_download_lock = threading.Lock()
_prepare_lock = threading.Lock()
_prepare_status = {"state": "idle", "error": None}

_MODEL_FILES = (
    (MODEL_REPO, MODEL_FILE),
    (MODEL_REPO, VAE_FILE),
    (T5_REPO, T5_FILE),
)


def _cached_file(repo_id: str, filename: str) -> Path | None:
    try:
        return Path(hf_hub_download(
            repo_id, filename, cache_dir=str(HF_CACHE), local_files_only=True
        ))
    except Exception:
        return None


def model_inventory_entry() -> dict:
    """Return the Models-page entry without claiming the CLI model stays resident."""
    files = [_cached_file(repo, filename) for repo, filename in _MODEL_FILES]
    existing = [path for path in files if path and path.is_file()]
    assets_ready = len(existing) == len(_MODEL_FILES)
    with _prepare_lock:
        prepare_state = _prepare_status["state"]
        prepare_error = _prepare_status.get("error")
    if assets_ready:
        download_state = "ready"
    elif prepare_state == "downloading":
        download_state = "downloading"
    elif prepare_error:
        download_state = "error"
    else:
        download_state = "missing"
    try:
        _find_sd_cli()
        runtime_available = True
    except VideoGenerationError:
        runtime_available = False
    return {
        "id": "Wan 2.2 TI2V-5B Q4_K_S",
        "backend": "wan22",
        "type": "video",
        "publisher": "off-grid-ai",
        "quantization": "Q4_K_S",
        "size_gb": round(sum(path.stat().st_size for path in existing) / (1024 ** 3), 2) or None,
        "expected_size_gb": 7.5,
        "state": "not-loaded",
        "assets_ready": assets_ready,
        "runtime_available": runtime_available,
        "download_state": download_state,
        "load_error": prepare_error,
        "externally_managed": True,
        "deletable": False,
        "loadable": False,
    }


def model_download_active() -> bool:
    with _prepare_lock:
        return _prepare_status["state"] == "downloading"


def start_model_download() -> str:
    """Begin preparing Wan's weights in the shared cache, returning current status."""
    with _prepare_lock:
        if _prepare_status["state"] == "downloading":
            return "downloading"
        _prepare_status.update(state="downloading", error=None)

    def worker() -> None:
        try:
            _download_model_files()
            _find_sd_cli()
            with _prepare_lock:
                _prepare_status.update(state="ready", error=None)
        except Exception as exc:
            with _prepare_lock:
                _prepare_status.update(state="error", error=str(exc)[:1200])

    threading.Thread(target=worker, name="wan22-model-download", daemon=True).start()
    return "downloading"


def _container_model_path(host_path: Path) -> str:
    """Translate a cached host model path to Lemonade's shared HF mount."""
    try:
        relative = host_path.resolve().relative_to(HF_CACHE.resolve())
    except ValueError as exc:
        raise VideoGenerationError(
            "The Wan model cache is outside Lemonade's mounted Hugging Face cache. "
            "Set LEMONADE_HF_CACHE to the host path mounted at " + CONTAINER_HF_CACHE
        ) from exc
    return f"{CONTAINER_HF_CACHE}/{relative.as_posix()}"


def _download_model_files() -> tuple[str, str, str]:
    """Resolve the small Q4 Wan 2.2 recipe, downloading missing HF files once."""
    with _prepare_lock:
        _prepare_status.update(state="downloading", error=None)
    try:
        with _download_lock:
            paths = [hf_hub_download(repo, filename, cache_dir=str(HF_CACHE))
                     for repo, filename in _MODEL_FILES]
    except Exception as exc:
        with _prepare_lock:
            _prepare_status.update(state="error", error=str(exc)[:1200])
        raise VideoGenerationError(f"Could not download the Wan 2.2 model files: {exc}") from exc
    with _prepare_lock:
        _prepare_status.update(state="ready", error=None)
    return tuple(_container_model_path(Path(path)) for path in paths)


@lru_cache(maxsize=1)
def _find_sd_cli() -> str:
    command = [
        "docker", "exec", CONTAINER, "sh", "-lc",
        "find /opt/lemonade/.cache/lemonade/bin/sd-cpp -type f -name sd-cli -print -quit",
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=15)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise VideoGenerationError(
            "The Lemonade container with its sd.cpp Vulkan runtime is unavailable. "
            "Start or update Lemonade, then try again."
        ) from exc
    path = result.stdout.strip()
    if not path:
        raise VideoGenerationError("The Lemonade container does not include the sd.cpp video runtime.")
    return path


def generate_video(prompt: str) -> dict:
    """Generate a short Wan 2.2 clip and return its bytes and media metadata."""
    prompt = (prompt or "").strip()
    if not prompt:
        raise VideoGenerationError("A video prompt is required.")

    # sd.cpp uses shared Vulkan resources; serialize generations so two browser
    # requests cannot compete for the iGPU's shared memory.
    with _generation_lock:
        files = [_cached_file(repo, filename) for repo, filename in _MODEL_FILES]
        if any(path is None or not path.is_file() for path in files):
            raise VideoGenerationError("Prepare Wan 2.2 from the Models page before generating video.")
        model, vae, t5 = (_container_model_path(path) for path in files)
        cli = _find_sd_cli()
        token = uuid.uuid4().hex
        output = f"/opt/lemonade/.cache/lemonade/hearth-videos/{token}.webm"
        prepare = ["docker", "exec", CONTAINER, "mkdir", "-p", "/opt/lemonade/.cache/lemonade/hearth-videos"]
        try:
            subprocess.run(prepare, check=True, capture_output=True, timeout=15)
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise VideoGenerationError("Could not prepare Lemonade's video output directory.") from exc

        args = [
            "docker", "exec", CONTAINER, cli,
            "--mode", "vid_gen",
            "--diffusion-model", model,
            "--vae", vae,
            "--t5xxl", t5,
            "--output", output,
            "--prompt", prompt,
            "--width", os.environ.get("HEARTH_WAN22_WIDTH", "480"),
            "--height", os.environ.get("HEARTH_WAN22_HEIGHT", "320"),
            "--video-frames", os.environ.get("HEARTH_WAN22_FRAMES", "17"),
            "--fps", os.environ.get("HEARTH_WAN22_FPS", "16"),
            "--steps", os.environ.get("HEARTH_WAN22_STEPS", "4"),
            "--cfg-scale", "6.0",
            "--sampling-method", "euler",
            "--flow-shift", "3.0",
            "--offload-to-cpu",
            "--backend", os.environ.get(
                "HEARTH_WAN22_BACKEND", "te=cpu,diffusion=cpu,vae=cpu"
            ),
            "--diffusion-fa",
            "--vae-tiling",
            "--vae-conv-direct",
        ]
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=2400)
            if result.returncode:
                log = (result.stderr or result.stdout or "").strip()
                detail = log if len(log) <= 2400 else f"{log[:900]}\n…\n{log[-1400:]}"
                raise VideoGenerationError(f"Wan 2.2 generation failed: {detail or 'sd.cpp exited with an error.'}")
            local_output = Path("/tmp") / f"hearth-wan22-{token}.webm"
            subprocess.run(
                ["docker", "cp", f"{CONTAINER}:{output}", str(local_output)],
                check=True, capture_output=True, text=True, timeout=120,
            )
            data = local_output.read_bytes()
            local_output.unlink(missing_ok=True)
            if not data:
                raise VideoGenerationError("Wan 2.2 returned an empty video file.")
            return {"video_bytes": data, "model": "Wan 2.2 TI2V-5B Q4_K_S", "mime_type": "video/webm"}
        except subprocess.TimeoutExpired as exc:
            raise VideoGenerationError("Wan 2.2 generation exceeded the 40-minute limit.") from exc
        except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
            raise VideoGenerationError(f"Could not collect the generated Wan 2.2 video: {exc}") from exc
        finally:
            subprocess.run(
                ["docker", "exec", CONTAINER, "rm", "-f", "--", output],
                capture_output=True, timeout=15,
            )
