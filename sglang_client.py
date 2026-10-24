"""Remote SGLang OpenAI-compatible backend.

SGLang deployments are externally managed: this client discovers whatever the
configured server already serves and proxies chat completions.  Model ids are
namespaced with ``sglang/`` inside Hearth so they cannot collide with models
reported by another OpenAI-compatible backend.
"""

import json
import os

import requests
import model_test_profiles

import settings_store


class SGLangError(RuntimeError):
    pass


_DEFAULT_BASE_URL = os.environ.get("SGLANG_BASE_URL", "http://127.0.0.1:30000")
_DEFAULT_API_KEY = os.environ.get("SGLANG_API_KEY", "")
_PREFIX = "sglang/"


def base_url() -> str:
    return (settings_store.get("sglang_base_url") or _DEFAULT_BASE_URL).rstrip("/")


def api_key() -> str:
    return settings_store.get("sglang_api_key") or _DEFAULT_API_KEY


def _headers() -> dict[str, str]:
    key = api_key()
    return {"Authorization": f"Bearer {key}"} if key else {}


def _served_id(model: str) -> str:
    return model[len(_PREFIX):] if model.startswith(_PREFIX) else model


def owns_model(model: str) -> bool:
    return isinstance(model, str) and model.startswith(_PREFIX)


def list_models() -> list[dict]:
    try:
        response = requests.get(f"{base_url()}/v1/models", headers=_headers(), timeout=8)
        if response.status_code >= 400:
            raise SGLangError(_http_error(response))
        data = response.json().get("data", [])
    except requests.RequestException as exc:
        raise SGLangError(f"Could not reach SGLang at {base_url()}: {exc}") from exc
    except (ValueError, AttributeError) as exc:
        raise SGLangError("SGLang returned an invalid /v1/models response.") from exc

    models = []
    for item in data:
        model_id = item.get("id") if isinstance(item, dict) else None
        if not model_id:
            continue
        models.append({
            "id": f"{_PREFIX}{model_id}",
            "publisher": "SGLang",
            "type": "llm",
            "state": "loaded",
            "quantization": "remote SGLang",
            "externally_managed": True,
            "deletable": False,
            "max_context_length": item.get("max_context_length"),
            "configured_context_length": next((
                item.get(key) for key in ("max_model_len", "context_length")
                if isinstance(item.get(key), int) and item.get(key) > 0
            ), None),
        })
    return models


def verify_model(model: str) -> None:
    if not owns_model(model):
        raise SGLangError(f"Not an SGLang model id: {model}")
    if model not in {item["id"] for item in list_models()}:
        raise SGLangError(
            f"{model} is not currently served by SGLang at {base_url()}. "
            "Start or reconfigure the external SGLang server first."
        )


def chat_stream(
    messages: list[dict],
    model: str,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    frequency_penalty: float | None = 0.3,
    tools: list[dict] | None = None,
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
    if frequency_penalty is not None:
        payload["frequency_penalty"] = frequency_penalty
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    try:
        with requests.post(
            f"{base_url()}/v1/chat/completions",
            json=model_test_profiles.apply(payload, "sglang"),
            headers=_headers(),
            stream=True,
            timeout=(10, 900),
        ) as response:
            if response.status_code >= 400:
                raise SGLangError(_http_error(response))
            for line in response.iter_lines(chunk_size=1):
                if line:
                    yield line.decode("utf-8", errors="replace")
    except requests.RequestException as exc:
        raise SGLangError(f"SGLang chat request failed: {exc}") from exc


def _http_error(response) -> str:
    detail = ""
    try:
        body = response.json()
        error = body.get("error")
        detail = error.get("message", "") if isinstance(error, dict) else str(error or "")
    except (ValueError, AttributeError):
        detail = (response.text or "").strip()
    suffix = f": {detail}" if detail else ""
    return f"SGLang request failed with HTTP {response.status_code}{suffix}"
