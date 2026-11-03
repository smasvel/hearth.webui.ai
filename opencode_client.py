"""opencode.ai remote OpenAI-compatible backend.

opencode.ai is an externally managed hosted gateway:
models are discovered from the opencode.ai model-list endpoint and chat
completions are proxied straight to the OpenAI-compatible inference endpoint
with the configured API key. Model ids are namespaced with ``opencode/`` inside
Hearth so they cannot collide with models reported by another backend, and they
match the provider id OpenCode itself uses in opencode.jsonc
(``opencode/<model>``), so the agentic framework picks them up from the same
generated config without a special case.
"""

import copy
import uuid
import requests
import threading
import time

import model_test_profiles

import settings_store


class OpenCodeError(RuntimeError):
    pass


_DEFAULT_ROOT_URL = "https://opencode.ai/inference"
_DEFAULT_BASE_URL = _DEFAULT_ROOT_URL + "/openai"
_PREFIX = "opencode/"
_USER_AGENT = "Hearth-local-webui/1.0 (OpenCode Console client)"


def plan() -> str:
    """Which billing surface requests hit: 'payg' (per-credit pay-as-you-go Console,
    base …/inference/openai) or 'go' (OpenCode Go subscription, base …/inference/go/openai).
    With the wrong plan selected, Go subscribers see HTTP 402 because pay-as-you-go
    falls back to their unused credit balance."""
    value = (settings_store.get("opencode_api_plan") or "payg").strip().lower()
    return "go" if value == "go" else "payg"


def _root_url() -> str:
    """The …/inference root. An admin's explicit base-URL override wins and is
    honored as-is (advanced use only); otherwise plan chooses the product path."""
    override = (settings_store.get("opencode_api_base_url") or "").rstrip("/")
    if override:
        return override
    default_root = _DEFAULT_ROOT_URL
    if plan() == "go":
        return default_root + "/go/openai"
    return default_root + "/openai"


def base_url() -> str:
    override = (settings_store.get("opencode_api_base_url") or "").rstrip("/")
    if override:
        return override
    return _root_url()


def _models_url() -> str:
    """Model-list endpoint derived from the base: opencode.ai/inference/openai ->
    opencode.ai/inference/v1/models, or with the Go plan …/inference/go/v1/models
    (the documented discovery endpoints)."""
    override = (settings_store.get("opencode_api_base_url") or "").rstrip("/")
    if override:
        base = override
        marker = "/inference"
        position = base.find(marker)
        root = base if position < 0 else base[:position + len(marker)]
        return root + "/v1/models"
    root = _DEFAULT_ROOT_URL
    return root + ("/go/v1/models" if plan() == "go" else "/v1/models")


def api_key() -> str:
    return settings_store.get("opencode_api_key") or ""


def _headers() -> dict[str, str]:
    key = api_key()
    headers = {"User-Agent": _USER_AGENT}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _served_id(model: str) -> str:
    return model[len(_PREFIX):] if model.startswith(_PREFIX) else model


def owns_model(model: str) -> bool:
    return isinstance(model, str) and model.startswith(_PREFIX)


def _context_chain(item: dict):
    """opencode.ai model entries carry a limit block ({context, output}); fall back to
    the flat keys OpenAI-compatible deployments return."""
    if not isinstance(item, dict):
        return None, None
    limit = item.get("limit") if isinstance(item.get("limit"), dict) else {}
    context = next((item.get(key) for key in ("context_limit", "context_length", "max_model_len")
                    if isinstance(item.get(key), int) and item.get(key) > 0), None)
    if context is None:
        context = limit.get("context")
    output = limit.get("output") if isinstance(limit.get("output"), int) else None
    return context, output


_MODELS_CACHE_SECONDS = 60.0
_models_cache: dict = {'loaded_at': 0.0, 'models': []}
_models_cache_lock = threading.Lock()


def list_models() -> list[dict]:
    """Chat models served by opencode.ai — requires an API key. Without one there is
    nothing to reach, so return an empty list (a backend being unconfigured is
    normal, not an error); with a key, an upstream failure raises so the UI can
    show the cause. Results are cached for a minute: listing is called on every
    help/config snapshot build and must not turn into per-request upstream traffic."""
    if not api_key():
        return []
    with _models_cache_lock:
        cached, at = _models_cache['models'], _models_cache['loaded_at']
        if cached and time.monotonic() - at < _MODELS_CACHE_SECONDS:
            return copy.deepcopy(cached)
    try:
        response = requests.get(_models_url(), headers=_headers(), timeout=15)
        if response.status_code >= 400:
            raise OpenCodeError(_http_error(response))
        items = response.json().get("data", [])
    except (requests.RequestException, ValueError, AttributeError) as exc:
        if cached:
            # Stale-list failure-safety: an upstream hiccup must not wipe known
            # models from every panel that consults this inventory.
            return copy.deepcopy(cached)
        if isinstance(exc, requests.RequestException):
            raise OpenCodeError(f"Could not reach opencode.ai at {_models_url()}: {exc}") from exc
        raise OpenCodeError("opencode.ai returned an invalid model list.") from exc
    models = []
    for item in items:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not model_id or model_id.startswith("jev"):
            # Jev models are structured evaluators, not chat models.
            continue
        name = item.get("name") or item.get("display_name")
        context, output = _context_chain(item)
        models.append({
            "id": f"{_PREFIX}{model_id}",
            "publisher": "opencode.ai",
            "type": "llm",
            "state": "loaded",
            "quantization": "remote opencode.ai (" + ("free" if str(model_id).endswith("-free") else ("Go plan" if plan() == "go" else "pay-as-you-go")) + ")",
            "externally_managed": True,
            "deletable": False,
            "max_context_length": context,
            "configured_context_length": context,
            "output_tokens_limit": output,
            "name": name if isinstance(name, str) else model_id,
        })
    with _models_cache_lock:
        _models_cache.update(loaded_at=time.monotonic(), models=models)
    return models


def verify_model(model: str) -> None:
    if not owns_model(model):
        raise OpenCodeError(f"Not an opencode.ai model id: {model}")
    if not api_key():
        raise OpenCodeError(
            "No opencode.ai API key is configured. "
            "Set it in Settings before selecting an opencode.ai model.")
    if model not in {item["id"] for item in list_models()}:
        raise OpenCodeError(
            f"{model} is not currently served by opencode.ai. "
            "Check the model id against https://opencode.ai/models first.")


def _v1_url() -> str:
    """OpenAI-compatible chat base: the documented form appends /v1 to the OpenAI base."""
    return base_url().rstrip("/") + "/v1"


def chat_stream(
    messages: list[dict],
    model: str,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    tools: list[dict] | None = None,
    session_id: str | None = None,
):
    payload = {
        "model": _served_id(model),
        "messages": messages,
        "temperature": temperature,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    headers = _headers()
    # The Go surface requires a stable per-conversation routing header; without it
    # requests fail with HTTP 400 "missing x-opencode-session and cannot be routed".
    headers['x-opencode-session'] = (session_id or uuid.uuid4().hex)[:120]
    try:
        with requests.post(
            f"{_v1_url()}/chat/completions",
            json=model_test_profiles.apply(payload, "opencode"),
            headers=headers,
            stream=True,
            # opencode.ai is a cloud gateway far beyond the LAN; a slow first token
            # from a distant provider must survive long enough to stream at all.
            timeout=(15, 1800),
        ) as response:
            if response.status_code >= 400:
                raise OpenCodeError(_http_error(response))
            for line in response.iter_lines(chunk_size=1):
                if line:
                    yield line.decode("utf-8", errors="replace")
    except requests.RequestException as exc:
        raise OpenCodeError(f"opencode.ai chat request failed: {exc}") from exc


def _http_error(response) -> str:
    detail = ""
    try:
        body = response.json()
        error = body.get("error")
        detail = error.get("message", "") if isinstance(error, dict) else str(error or "")
    except (ValueError, AttributeError):
        detail = (response.text or "").strip()
    suffix = f": {detail}" if detail else ""
    if response.status_code in (401, 403):
        return (f"opencode.ai rejected the request with HTTP {response.status_code} "
                f"(check the API key in Settings){suffix}")
    if response.status_code == 402:
        return ("This opencode.ai account has no credit available (HTTP 402). If you are "
                "subscribed to OpenCode Go, set the account plan to OpenCode Go in "
                "Settings (Go traffic uses a dedicated endpoint); otherwise add credits "
                "at https://opencode.ai/console or pick a free opencode.ai model "
                "(the ids ending in -free).")
    return f"opencode.ai request failed with HTTP {response.status_code}{suffix}"
