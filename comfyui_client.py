"""Thin client for a local ComfyUI instance (text-to-image via SD-Turbo).

ComfyUI's API is graph-based: submit a "workflow" (a dict of numbered nodes), poll
/history for the queued prompt_id, then fetch the resulting image bytes from /view.
This module hardcodes a minimal SD-Turbo txt2img graph rather than exposing ComfyUI's
full node system — this app only ever needs "prompt in, PNG out".
"""

from __future__ import annotations

import os
import random
import re
import subprocess
import tempfile
import time
import uuid

import requests

COMFYUI_BASE_URL = os.environ.get("COMFYUI_BASE_URL", "http://127.0.0.1:8188").rstrip("/")
CHECKPOINT = os.environ.get("COMFYUI_CHECKPOINT", "sd_turbo.safetensors")
VIDEO_MODEL = os.environ.get("COMFYUI_VIDEO_MODEL", "wan2.1_t2v_1.3B_bf16.safetensors")
VIDEO_ENCODER = os.environ.get("COMFYUI_VIDEO_ENCODER", "umt5_xxl_fp8_e4m3fn_scaled.safetensors")
VIDEO_VAE = os.environ.get("COMFYUI_VIDEO_VAE", "wan_2.1_vae.safetensors")
COMFYUI_MODELS_DIR = os.environ.get(
    "COMFYUI_MODELS_DIR", os.path.expanduser("~/.local/share/hearth/comfyui/models")
)


class ComfyUIError(RuntimeError):
    pass


def video_model_info() -> dict:
    """Inventory entry for the Models/Downloads pages.

    Wan is a three-file ComfyUI pipeline, so expose it as one usable model instead of
    three misleading independent cards. ComfyUI loads it on demand and retains it in
    its cache; "loaded" here means every artifact is installed and the serving process
    is ready, matching what users need to know before submitting a video request.
    """
    root = os.path.expanduser(COMFYUI_MODELS_DIR)
    components = [
        ("diffusion_models", VIDEO_MODEL),
        ("text_encoders", VIDEO_ENCODER),
        ("vae", VIDEO_VAE),
    ]
    paths = [os.path.join(root, folder, name) for folder, name in components]
    installed = all(os.path.isfile(path) and os.path.getsize(path) > 0 for path in paths)
    size = sum(os.path.getsize(path) for path in paths if os.path.isfile(path))
    ready = False
    if installed:
        try:
            ready = requests.get(f"{COMFYUI_BASE_URL}/system_stats", timeout=2).ok
        except requests.RequestException:
            pass
    return {
        "id": "Wan 2.1 T2V 1.3B",
        "backend": "comfyui",
        "publisher": "Alibaba / Comfy-Org",
        "quantization": "BF16 + FP8 encoder",
        "type": "video",
        "size_gb": round(size / (1024 ** 3), 2),
        "state": "loaded" if ready else "not-loaded",
        "downloaded": installed,
        "components": [name for _, name in components],
    }


def owns_model(identifier: str) -> bool:
    """Route Models-panel unload/delete requests here for the Wan video card."""
    return identifier == "Wan 2.1 T2V 1.3B"


def unload_model(identifier: str) -> None:
    """Free the video pipeline from VRAM via ComfyUI's own management API.

    Unlike the other backends, ComfyUI doesn't stop a process or swap a service.env -
    it just holds whatever it last ran in its internal model cache, so "unload" here
    means asking ComfyUI itself to drop that cache and release VRAM. Confirmed live:
    this was never wired into /api/models/unload at all (every other backend is), so
    clicking Unload on this card silently fell through to the LM Studio branch and
    failed instead of doing anything.
    """
    try:
        resp = requests.post(
            f"{COMFYUI_BASE_URL}/free",
            json={"unload_models": True, "free_memory": True},
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise ComfyUIError(f"Failed to unload '{identifier}': {exc}") from exc


# --- Intent detection: "is this chat message actually an image request?" ---
#
# Mirrors web_search_client's looks_like_weather_query()/looks_like_news_query() style:
# pragmatic keyword/regex heuristics, not real NLP, tuned against concrete phrasings
# rather than guessed. "draw"/"paint"/"doodle" are unambiguous enough on their own to
# fire directly; "generate"/"create"/"make"/... need a nearby image-flavored noun since
# those verbs are just as often used for "generate a report" or "make a plan". A small
# idiom exclusion list keeps "draw a conclusion/comparison/attention/..." from false-
# triggering, since those are extremely common in normal chat and share the same verb.
_IMAGE_NOUNS = (
    "image", "images", "picture", "pictures", "photo", "photos", "photograph", "photographs",
    "drawing", "drawings", "painting", "paintings", "illustration", "illustrations",
    "artwork", "art", "wallpaper", "wallpapers", "logo", "logos", "icon", "icons",
    "avatar", "avatars", "portrait", "portraits", "poster", "posters", "meme", "memes",
    "sticker", "stickers", "graphic", "graphics", "thumbnail", "thumbnails", "sketch", "sketches",
)
_IMAGE_VERBS_UNAMBIGUOUS = ("draw", "paint", "doodle", "sketch")
_IMAGE_VERBS_NEEDING_NOUN = (
    "generate", "create", "make", "design", "render", "produce", "illustrate",
    "show me", "give me", "draw me", "paint me",
)

_IMAGE_IDIOM_EXCLUDE_RE = re.compile(
    r"\bdraw\s+(?:(?:a|an|my|your|the)\s+)?(?:\w+\s+){0,2}"
    r"(?:conclusion|comparison|attention|blank|line|parallel|distinction|analogy|inference)s?\b"
    r"|\bsketch\s+(?:out\s+)?(?:(?:a|an|my|your|the|some)\s+)?(?:\w+\s+){0,2}"
    r"(?:plan|outline|idea|strategy|approach|solution|roadmap|framework|budget|script|"
    r"letter|email|response|message|list|agenda)s?\b",
    re.IGNORECASE,
)
_IMAGE_REQUEST_RE = re.compile(
    r"\b(?:" + "|".join(_IMAGE_VERBS_UNAMBIGUOUS) + r")\b"
    r"|\b(?:" + "|".join(v.replace(" ", r"\s+") for v in _IMAGE_VERBS_NEEDING_NOUN) + r")\b"
    r"[^.!?\n]{0,40}?\b(?:" + "|".join(_IMAGE_NOUNS) + r")s?\b"
    r"|\b(?:" + "|".join(_IMAGE_NOUNS) + r")\s+(?:of|showing|depicting|featuring)\b",
    re.IGNORECASE,
)


def looks_like_image_request(text: str | None) -> bool:
    """True if a chat message reads like a request to generate an image, not just talk
    about one. Used to auto-route /api/chat into image generation instead of the normal
    LLM reply, so the user doesn't have to remember to press the separate 'Generate
    image' button."""
    if not text:
        return False
    if _IMAGE_IDIOM_EXCLUDE_RE.search(text):
        return False
    return bool(_IMAGE_REQUEST_RE.search(text))


_VIDEO_REQUEST_RE = re.compile(
    r"\b(?:generate|create|make|render|produce)\b[^.!?\n]{0,50}\b(?:video|movie|clip|animation)\b"
    r"|\b(?:video|movie|clip|animation)\s+(?:of|showing|depicting|featuring)\b",
    re.IGNORECASE,
)


def looks_like_video_request(text: str | None) -> bool:
    return bool(text and _VIDEO_REQUEST_RE.search(text))


# Iterating on a just-generated image ("that's blurry", "the face is wrong", "make it
# sharper") almost never contains a drawing verb or an image noun, so looks_like_image_request()
# correctly says no to these on their own - "that's wrong" or "not clear" said in a normal
# coding conversation must NOT auto-route into image generation. This vocabulary is
# deliberately much broader/leakier than the primary detector above, and is only ever
# checked by the caller (app.py) *in addition to* confirming the immediately preceding
# assistant turn was itself a generated image - that gate is what keeps "that's wrong" from
# false-triggering everywhere else, not this regex alone.
_IMAGE_FOLLOWUP_RE = re.compile(
    r"\bnot\s+(?:clear|sharp|good|right|accurate)\b"
    r"|\bisn'?t\s+(?:clear|sharp|right|working)\b"
    r"|\b(?:blurry|blurred|unclear|pixelated|distorted|deformed)\b"
    r"|\btoo\s+(?:blurry|dark|small|low\s+res(?:olution)?)\b"
    r"|\b(?:that'?s|this\s+is|it'?s|\w+\s+(?:is|looks?))\s+wrong\b"
    r"|\bwrong\s+(?:face|hand|hands|pose|proportions?|colou?rs?)\b"
    r"|\b(?:redo|regenerate|re-?generate)\s*(?:it|this)?\b"
    r"|\btry\s+again\b"
    r"|\bone\s+more\s+(?:try|attempt|go)\b"
    r"|\bfix\s+(?:it|this|the)\b"
    r"|\b(?:make|render)\s+it\s+(?:sharper|clearer|better|more\s+\w+)\b"
    r"|\bmore\s+(?:detailed|accurate|realistic)\b"
    r"|\bhigher\s+(?:quality|resolution)\b"
    r"|\bdifferent\s+(?:pose|style|angle|background)\b",
    re.IGNORECASE,
)


def looks_like_image_followup(text: str | None) -> bool:
    """Deliberately broad revision/complaint vocabulary for iterating on a generated
    image ("not clear", "that's wrong", "try again"). NOT safe to call on its own - the
    caller must also confirm the previous assistant turn was actually a generated image
    before trusting this, since on its own this regex fires on plenty of unrelated chat
    ("that's wrong, use a different approach")."""
    if not text:
        return False
    return bool(_IMAGE_FOLLOWUP_RE.search(text))


def _workflow(prompt: str, negative_prompt: str, steps: int, width: int, height: int, seed: int, cfg: float) -> dict:
    return {
        "checkpoint": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": CHECKPOINT},
        },
        "positive": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["checkpoint", 1]},
        },
        "negative": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative_prompt, "clip": ["checkpoint", 1]},
        },
        "latent": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "sampler": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": "euler_ancestral",
                "scheduler": "normal",
                "denoise": 1.0,
                "model": ["checkpoint", 0],
                "positive": ["positive", 0],
                "negative": ["negative", 0],
                "latent_image": ["latent", 0],
            },
        },
        "decode": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["sampler", 0], "vae": ["checkpoint", 2]},
        },
        "save": {
            "class_type": "SaveImage",
            "inputs": {"images": ["decode", 0], "filename_prefix": "ai_webui"},
        },
    }


def generate_image(
    prompt: str,
    negative_prompt: str = "",
    steps: int = 2,
    width: int = 512,
    height: int = 512,
    seed: int | None = None,
    timeout: int = 600,
) -> dict:
    """Submit a txt2img job and block until the PNG bytes are ready. Returns
    {"image_bytes": ..., "seed": ..., "cfg": ...} rather than raw bytes — the seed is
    worth handing back so a result can be intentionally reproduced later (random by
    default, same as before, just no longer thrown away).

    SD-Turbo is CPU-friendly *for a diffusion model*, but this is still CPU inference
    on an integrated GPU-less box — expect tens of seconds, not hosted-API speed.

    cfg (classifier-free guidance) is deliberately not a caller-facing parameter: SD-Turbo
    is distilled specifically for guidance-free sampling at cfg=1.0, where a negative
    prompt has *zero* effect by construction (cfg=1.0 means the negative branch is never
    blended in at all) — this isn't a missing feature, it's what the model was trained
    for. A negative prompt only does anything at cfg > 1, which pushes this particular
    model outside its trained regime and tends to produce artifacts rather than cleanly
    honoring the negative prompt. Splitting the difference: cfg is auto-bumped to a mild
    1.8 only when a negative prompt is actually supplied, left at the model's native 1.0
    otherwise — so leaving the field blank keeps today's exact fast/clean behavior.
    """
    client_id = uuid.uuid4().hex
    seed = seed if seed is not None else random.randint(0, 2**32 - 1)
    cfg = 1.8 if negative_prompt.strip() else 1.0
    workflow = _workflow(prompt, negative_prompt, steps, width, height, seed, cfg)

    # ComfyUI can be mid-model-swap or briefly unresponsive right after a restart —
    # same "don't fail on the first transient hiccup" reasoning as the LM Studio
    # load-retry logic elsewhere in this app, just applied to job submission instead.
    last_exc: Exception | None = None
    prompt_id = None
    for attempt in range(3):
        try:
            resp = requests.post(
                f"{COMFYUI_BASE_URL}/prompt",
                json={"prompt": workflow, "client_id": client_id},
                timeout=10,
            )
            resp.raise_for_status()
            prompt_id = resp.json()["prompt_id"]
            last_exc = None
            break
        except requests.RequestException as exc:
            last_exc = ComfyUIError(f"Could not reach ComfyUI at {COMFYUI_BASE_URL}: {exc}")
        except (KeyError, ValueError):
            # A malformed/rejected workflow will fail the same way every retry —
            # no point burning attempts on it.
            raise ComfyUIError(f"ComfyUI rejected the workflow: {resp.text[:500]}")
        if attempt < 2:
            time.sleep(2)
    if last_exc is not None:
        raise last_exc

    deadline = time.monotonic() + timeout
    consecutive_poll_failures = 0
    while time.monotonic() < deadline:
        try:
            hist_resp = requests.get(f"{COMFYUI_BASE_URL}/history/{prompt_id}", timeout=10)
            hist_resp.raise_for_status()
            history = hist_resp.json()
            consecutive_poll_failures = 0
        except requests.RequestException as exc:
            # One flaky poll shouldn't abort a generation that's still running fine
            # server-side — only give up after several polls in a row fail.
            consecutive_poll_failures += 1
            if consecutive_poll_failures >= 5:
                raise ComfyUIError(f"ComfyUI history request failed repeatedly: {exc}") from exc
            time.sleep(1)
            continue

        entry = history.get(prompt_id)
        if entry:
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                raise ComfyUIError(f"ComfyUI generation failed: {status}")
            images = entry.get("outputs", {}).get("save", {}).get("images")
            if images:
                image = images[0]
                try:
                    img_resp = requests.get(
                        f"{COMFYUI_BASE_URL}/view",
                        params={
                            "filename": image["filename"],
                            "subfolder": image.get("subfolder", ""),
                            "type": image.get("type", "output"),
                        },
                        timeout=30,
                    )
                    img_resp.raise_for_status()
                except requests.RequestException as exc:
                    raise ComfyUIError(f"Could not fetch generated image: {exc}") from exc
                return {"image_bytes": img_resp.content, "seed": seed, "cfg": cfg}
        time.sleep(1)

    raise ComfyUIError(f"Timed out after {timeout}s waiting for ComfyUI to generate the image")


def _video_workflow(prompt: str, negative_prompt: str, width: int, height: int,
                    frames: int, fps: int, steps: int, seed: int) -> dict:
    return {
        "model": {"class_type": "UNETLoader", "inputs": {
            "unet_name": VIDEO_MODEL, "weight_dtype": "default"}},
        "sampling": {"class_type": "ModelSamplingSD3", "inputs": {
            "model": ["model", 0], "shift": 8.0}},
        "clip": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": VIDEO_ENCODER, "type": "wan", "device": "cpu"}},
        "positive": {"class_type": "CLIPTextEncode", "inputs": {
            "text": prompt, "clip": ["clip", 0]}},
        "negative": {"class_type": "CLIPTextEncode", "inputs": {
            "text": negative_prompt, "clip": ["clip", 0]}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": VIDEO_VAE}},
        "latent": {"class_type": "EmptyHunyuanLatentVideo", "inputs": {
            "width": width, "height": height, "length": frames, "batch_size": 1}},
        "sampler": {"class_type": "KSampler", "inputs": {
            "seed": seed, "steps": steps, "cfg": 6.0, "sampler_name": "uni_pc",
            "scheduler": "simple", "denoise": 1.0, "model": ["sampling", 0],
            "positive": ["positive", 0], "negative": ["negative", 0],
            "latent_image": ["latent", 0]}},
        "decode": {"class_type": "VAEDecode", "inputs": {
            "samples": ["sampler", 0], "vae": ["vae", 0]}},
        "video": {"class_type": "CreateVideo", "inputs": {
            "images": ["decode", 0], "fps": float(fps)}},
        "save": {"class_type": "SaveVideo", "inputs": {
            "video": ["video", 0], "filename_prefix": "ai_webui/video",
            "format": "mp4", "codec": "h264"}},
    }


def generate_video(prompt: str, negative_prompt: str = "", width: int = 256,
                   height: int = 144, frames: int = 9, fps: int = 6,
                   steps: int = 12, seed: int | None = None, timeout: int = 3600) -> dict:
    """Generate a short MP4 with the compact Wan 2.1 T2V model.

    Defaults are intentionally conservative because this host runs ComfyUI on CPU.
    """
    width = max(256, min(480, (int(width) // 16) * 16))
    height = max(144, min(320, (int(height) // 16) * 16))
    frames = max(5, min(33, 1 + ((int(frames) - 1) // 4) * 4))
    fps = max(4, min(16, int(fps)))
    steps = max(12, min(24, int(steps)))
    seed = seed if seed is not None else random.randint(0, 2**32 - 1)
    workflow = _video_workflow(prompt, negative_prompt, width, height, frames, fps, steps, seed)
    try:
        resp = requests.post(f"{COMFYUI_BASE_URL}/prompt", json={
            "prompt": workflow, "client_id": uuid.uuid4().hex}, timeout=20)
        if not resp.ok:
            raise ComfyUIError(f"ComfyUI rejected the video workflow: {resp.text[:700]}")
        prompt_id = resp.json()["prompt_id"]
    except requests.RequestException as exc:
        raise ComfyUIError(f"Could not reach ComfyUI at {COMFYUI_BASE_URL}: {exc}") from exc

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            history = requests.get(f"{COMFYUI_BASE_URL}/history/{prompt_id}", timeout=15).json()
        except requests.RequestException:
            time.sleep(2)
            continue
        entry = history.get(prompt_id)
        if entry:
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                messages = status.get("messages", [])
                raise ComfyUIError(f"ComfyUI video generation failed: {messages[-1:] or status}")
            output = entry.get("outputs", {}).get("save", {})
            files = output.get("videos") or output.get("gifs") or output.get("images") or []
            if files:
                item = files[0]
                try:
                    media = requests.get(f"{COMFYUI_BASE_URL}/view", params={
                        "filename": item["filename"], "subfolder": item.get("subfolder", ""),
                        "type": item.get("type", "output")}, timeout=120)
                    media.raise_for_status()
                except requests.RequestException as exc:
                    raise ComfyUIError(f"Could not fetch generated video: {exc}") from exc
                return {"video_bytes": media.content, "seed": seed, "width": width,
                        "height": height, "frames": frames, "fps": fps}
        time.sleep(2)
    raise ComfyUIError(f"Timed out after {timeout}s waiting for ComfyUI to generate the video")


def generate_fast_video(prompt: str, negative_prompt: str = "", duration: int = 3,
                        seed: int | None = None) -> dict:
    """Create a coherent short MP4 from an AI-generated SD-Turbo scene.

    Wan T2V is retained for capable hardware, but on this CPU it forces a bad choice
    between color-blob output and very long renders. SD-Turbo reliably creates a clear
    512px scene here in seconds; a restrained Ken Burns move turns it into a universally
    playable H.264 clip without another large inference pass.
    """
    image = generate_image(prompt, negative_prompt=negative_prompt, steps=2,
                           width=512, height=512, seed=seed, timeout=600)
    duration = max(2, min(6, int(duration)))
    frame_count = duration * 24
    with tempfile.TemporaryDirectory(prefix="hearth-video-") as temp_dir:
        image_path = os.path.join(temp_dir, "scene.png")
        video_path = os.path.join(temp_dir, "video.mp4")
        with open(image_path, "wb") as output:
            output.write(image["image_bytes"])
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-loop", "1",
            "-i", image_path, "-vf",
            (
                # Zoom on a large intermediate canvas. At 640px, zoompan rounds the
                # crop origin back and forth between adjacent pixels, which appears as
                # obvious shaking on mobile. A 4096px canvas makes that rounding
                # sub-pixel at the final 512px output while retaining a gentle move.
                "scale=4096:4096:flags=lanczos,"
                f"zoompan=z='min(zoom+0.0008,1.06)':x='(iw-iw/zoom)/2':"
                f"y='(ih-ih/zoom)/2':d={frame_count}:s=512x512:fps=24,"
                "format=yuv420p"
            ),
            "-frames:v", str(frame_count), "-c:v", "libx264", "-preset", "veryfast",
            "-movflags", "+faststart", video_path,
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=60)
            with open(video_path, "rb") as result_file:
                video_bytes = result_file.read()
        except (OSError, subprocess.SubprocessError) as exc:
            detail = getattr(exc, "stderr", b"")
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", errors="replace")
            raise ComfyUIError(f"Could not encode the generated scene as video: {detail or exc}") from exc
    return {"video_bytes": video_bytes, "seed": image["seed"], "width": 512,
            "height": 512, "frames": frame_count, "fps": 24}
