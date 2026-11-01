"""Client side of the optional backend control broker.

Hearth inside Docker has no systemd: vLLM / KoboldCPP / FastFlowLM load/unload calls end
up failing with ``FileNotFoundError('systemctl')``. Those three backends remain host-managed
by design, but the host also exposes a small local broker (``deploy/backend_broker.py``) that
Docker Hearth can call over HTTP for exactly the systemctl verbs Hearth needs. Setting
``HEARTH_BACKEND_BROKER_URL`` and ``HEARTH_BACKEND_BROKER_TOKEN`` (see
``deploy/install-backend-broker.sh``) makes the bridge active; without them the prior
fail-soft behavior is unchanged.
"""
from __future__ import annotations

import os
import shutil

import requests


class BackendBrokerError(RuntimeError):
    """Raised when the broker is configured but the request fails."""


def broker_configured() -> bool:
    """True when Docker Hearth should route systemctl through the host broker.

    Requires both the broker URL and a token file. Without either, the previous
    behavior is preserved so host installs are unaffected."""
    return bool(broker_src_url() and broker_token())


def broker_src_url() -> str:
    return os.environ.get("HEARTH_BACKEND_BROKER_URL", "")


def broker_token() -> str:
    return os.environ.get("HEARTH_BACKEND_BROKER_TOKEN", "")


def systemctl_via_broker(
    args: list[str],
    service: str,
    *,
    scope: str = "user",
    timeout: int = 120,
) -> str | None:
    """Try to run ``systemctl [--user] <args...> <service>`` through the host broker.

    Returns ``None`` to fall back to the existing local behavior. Returns the command's
    stdout only when the broker is configured and the request succeeds."""
    if not broker_configured() or shutil.which("systemctl"):
        return None
    url = os.environ["HEARTH_BACKEND_BROKER_URL"].rstrip("/")
    token = os.environ["HEARTH_BACKEND_BROKER_TOKEN"]
    try:
        resp = requests.post(
            f"{url}/systemd",
            json={"args": args, "service": service, "scope": scope},
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise BackendBrokerError(f"backend broker request failed: {exc}") from exc
    if resp.status_code >= 400:
        try:
            message = resp.json().get("error") or str(resp.status_code)
        except ValueError:
            message = str(resp.status_code)
        raise BackendBrokerError(f"backend broker rejected '{args[0] if args else ''} {service}': {message}")
    try:
        payload = resp.json()
        returncode = int(payload.get("returncode", 0))
        stdout = str(payload.get("stdout") or "")
        stderr = str(payload.get("stderr") or "")
    except (ValueError, TypeError) as exc:
        raise BackendBrokerError(f"backend broker returned a malformed response: {exc}") from exc
    if returncode != 0 and "is-active" not in args:
        raise BackendBrokerError((stderr or stdout or "systemctl command failed").strip())
    return stdout
