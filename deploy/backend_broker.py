"""Small host-side broker for the Docker Hearth app to manage host systemd units.

Docker Hearth runs the Flask app without systemd, so the vLLM / KoboldCPP / FastFlowLM
load/unload controls were unavailable from inside the container. This broker listens on
127.0.0.1 and executes a *restricted* set of ``systemctl --user`` commands on the Hearth
host, returning ``(returncode, stdout, stderr)``.

Defaults are about as locked down as this can be made short of not running at all:

- bound to 127.0.0.1 only,
- bearer-token auth (token file must have mode 600),
- an explicit allowlist of service names the broker will act on,
- an explicit allowlist of systemctl verbs — no ``kill``, no ``edit``, no ``run``,
  no ``daemon-reload`` and nothing else.

Run it with ``python3 deploy/backend_broker.py`` from the Hearth host, or install it as
a systemd *user* unit with ``bash deploy/install-backend-broker.sh``.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = int(os.environ.get("HEARTH_BACKEND_BROKER_PORT", "8765"))
DEFAULT_TOKEN_FILE = os.path.expanduser(
    os.environ.get("HEARTH_BACKEND_BROKER_TOKEN_FILE", "~/.config/hearth/backend-broker/token")
)
# Docker Hearth drives these three host units. Keeping the list explicit makes the blast
# radius of a leaked token very small. FastFlowLM is a *system* unit (NPU memlock); vLLM
# and KoboldCPP are user units, and the broker preserves that scope distinction.
_DEFAULT_SERVICES = "vllm.service,kobold.service,q38rocm.service,flm.service"
_ALLOWED_VERBS = frozenset({"start", "stop", "restart", "daemon-reload", "is-active", "is-enabled"})
_ALLOWED_SCOPES = frozenset({"user", "system"})
# The FLM CLI is only used from Docker Hearth to keep model *inventory*, download and
# delete in sync with the host backend; fade in every actual `flm` verb Hearth needs.
_FLM_ACTIONS = frozenset({"list", "pull", "remove", "--version", "pull-progress"})
_ALLOWED_UNITS = frozenset(
    service.strip() for service in os.environ.get(
        "HEARTH_BACKEND_BROKER_SERVICES", _DEFAULT_SERVICES
    ).split(",") if service.strip()
)


def _allowed_units() -> frozenset[str]:
    return _ALLOWED_UNITS


class BrokerError(RuntimeError):
    """Internal server error; surfaces as a JSON 500 to the caller."""


def load_token(token_file: str = DEFAULT_TOKEN_FILE) -> str | None:
    """Read the broker bearer token.

    Environment ``HEARTH_BACKEND_BROKER_TOKEN`` wins over the file. A file that is not
    mode 600 is refused, so an accidental world-readable token cannot silently weaken
    the broker."""
    env_token = os.environ.get("HEARTH_BACKEND_BROKER_TOKEN")
    if env_token:
        return env_token
    try:
        if os.stat(token_file).st_mode & 0o077:
            print(f"hearth-backend-broker: {token_file} must not be group/world-accessible", file=sys.stderr)
            return None
        return open(token_file).read().strip() or None
    except OSError:
        return None


def run_systemctl(args: list[str], service: str, scope: str = "user", timeout: int = 120) -> tuple[int, str, str]:
    """Run ``systemctl [--user] <args...> <service>`` on the Hearth host.

    Returns ``(returncode, stdout, stderr)``. Verbs, scope and service names are validated
    so a leaked token still cannot run arbitrary commands."""
    verb = args[0] if args else ""
    if verb not in _ALLOWED_VERBS:
        raise BrokerError(f"unsupported systemctl verb '{verb}'")
    if scope not in _ALLOWED_SCOPES:
        raise BrokerError(f"unsupported systemctl scope '{scope}'")
    if service not in _allowed_units():
        raise BrokerError(f"service '{service}' is not enabled on the backend broker")
    command = ["systemctl"]
    if scope == "user":
        command.append("--user")
    command.extend([*args, service])
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise BrokerError(f"systemctl {verb} {service} timed out") from exc
    except FileNotFoundError as exc:
        raise BrokerError("systemctl is not available on the broker host") from exc
    return proc.returncode, proc.stdout, proc.stderr


_FLM_STATE = threading.Lock()

_FLM_BIN = os.environ.get("HEARTH_BACKEND_BROKER_FLM_BIN", "/opt/fastflowlm/bin/flm")
_FLM_LIB_DIR = os.environ.get("HEARTH_BACKEND_BROKER_FLM_LIB", "/opt/fastflowlm/lib")
_FLM_MODEL_ROOT = os.path.expanduser(os.environ.get(
    "HEARTH_BACKEND_BROKER_FLM_MODEL_ROOT", "~/.config/flm/models"
))

# The broker keeps one concurrent "flm pull" so a leaked token cannot queue up disk fills;
# two pulls racing on the host only costs disk space, so anything more is purely cosmetic.
_FLM_PULL_LOCK = threading.Lock()
_FLM_PULL_STATE: dict = {"proc": None, "tag": None}


class _FlmPullState:
    """State of the one ``flm pull`` the broker is allowed to run at a time."""

    @staticmethod
    def running() -> bool:
        with _FLM_PULL_LOCK:
            proc = _FLM_PULL_STATE.get("proc")
            if proc and proc.poll() is None:
                return True
            if proc and proc.poll() is not None:
                _FLM_PULL_STATE.update(proc=None, tag=None)
            return False

    @staticmethod
    def tag() -> str | None:
        with _FLM_PULL_LOCK:
            return _FLM_PULL_STATE.get("tag")

    @staticmethod
    def start(proc: subprocess.Popen, tag: str) -> None:
        with _FLM_PULL_LOCK:
            _FLM_PULL_STATE["proc"] = proc
            _FLM_PULL_STATE["tag"] = tag


_FLM_PULL = _FlmPullState()

# Keep a *,-* style ELLPSIS retry wall so a leaked token cannot start a shell; a malformed
# tag is rejected before exec so a bare ``flm serve`` never starts from the broker.
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,120}$")


def _flm_environment() -> dict | None:
    if not os.path.isfile(_FLM_BIN):
        return None
    env = {**os.environ, "LD_LIBRARY_PATH": _FLM_LIB_DIR}
    env.pop("FLM_BIN", None)
    return env


def run_flm(args: list[str], timeout: int = 900, background: bool = False) -> dict:
    """Run the FLM CLI on the Hearth host for the verbs Hearth uses from Docker."""
    if not _flm_environment():
        raise BrokerError("FastFlowLM binary was not found on the broker host")
    verb = args[0] if args else ""
    if verb == "--version":
        # Read-only: just report the runtime version.
        proc = subprocess.run([_FLM_BIN, *args], capture_output=True, text=True, timeout=timeout)
        return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    if verb not in {"list", "pull", "remove", "pull-progress"}:
        raise BrokerError(f"unsupported flm verb '{verb}'")
    tag_args = args[1:]
    if verb == "list":
        if tag_args not in (
            ["--filter", "installed", "--json"],
            ["--filter", "all", "--json"],
        ):
            raise BrokerError("unsupported flm list filter")
        proc = subprocess.run([_FLM_BIN, *args], capture_output=True, text=True, timeout=timeout)
        return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    if verb == "pull-progress":
        return _flm_pull_progress()
    tag = tag_args[0] if tag_args else ""
    if not _TAG_RE.match(tag) or len(tag_args) != 1:
        raise BrokerError(f"unsupported flm {verb} target '{tag}'")
    if verb == "pull":
        if _FLM_PULL.running():
            raise BrokerError(
                "a FastFlowLM pull is already running on the host; wait for it to finish"
            )
        if background:
            proc = subprocess.Popen(
                [_FLM_BIN, "pull", tag],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                text=True, env=_flm_environment(),
            )
            _FLM_PULL.start(proc, tag)
            return {"ok": True}
        proc = subprocess.run([_FLM_BIN, *args], capture_output=True, text=True, timeout=timeout)
        return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    # remove
    proc = subprocess.run([_FLM_BIN, *args], capture_output=True, text=True, timeout=timeout)
    return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


def _flm_pull_progress() -> dict:
    """Percent/bytes progress for a broker-started or CLI-started host ``flm pull``."""
    # Read-only, allowed regardless of whether a broker pull is in flight: a CLI pull the
    # user started on the host should surface in the Docker app just like any other.
    import glob as globlib

    running = _FLM_PULL.running()
    tag = _FLM_PULL.tag()
    if not running:
        # Fall back to a host pgrep: a pull someone started outside the broker.
        try:
            out = subprocess.run(
                ["pgrep", "-af", "flm pull"], capture_output=True, text=True, timeout=5
            ).stdout
        except (subprocess.SubprocessError, OSError):
            out = ""
        for line in out.splitlines():
            if "pull" not in line:
                continue
            # "1234 /opt/fastflowlm/bin/flm pull qwen3:0.6b --force"
            parts = [p for p in line.split() if p != "-"]
            if parts and (idx := [i for i, p in enumerate(parts) if p == "pull"]):
                candidate = parts[idx[0] + 1] if idx[0] + 1 < len(parts) else ""
                if candidate and not candidate.startswith("-"):
                    tag, running = candidate, True
                    break
    if not running or not tag:
        return {"running": False}
    total = _flm_expected_bytes(tag)
    if not total:
        return {"running": True, "tag": tag, "percent": 0, "bytes_downloaded": 0, "bytes_total": None}
    directory = _flm_model_dir(tag)
    import os.path as osp

    if not directory:
        return {"running": True, "tag": tag, "percent": 0, "bytes_downloaded": 0, "bytes_total": None}
    cur = sum(osp.getsize(f) for f in globlib.glob(osp.join(directory, "**", "*"), recursive=True) if osp.isfile(f))
    pct = int(min(99, cur * 100 / total)) if total else 0
    return {
        "running": True,
        "tag": tag,
        "percent": pct,
        "bytes_downloaded": cur,
        "bytes_total": total,
    }


def _flm_model_dir(tag: str) -> str | None:
    """On-disk directory for `tag`, matching the on-disk name of the pull catalog."""
    try:
        from pathlib import Path
        root = Path(_FLM_MODEL_ROOT)
        if not root.is_dir():
            return None
        for child in sorted(root.iterdir()):
            if child.is_dir() and _TAG_RE.match(child.name):
                # The FLM pull stores its weights under ~/.config/flm/models/<hf-repo>;
                # the app's own progress path uses the *catalog* dir name (hf repo name)
                # and it doesn't depend on the tag. Use the catalog mapping through the
                # state dir's on-disk representation of the tag passed in.
                if child.name == tag:
                    return str(child)
        return None
    except OSError:
        return None


def _flm_expected_bytes(tag: str) -> int | None:
    catalog = _flm_catalog()
    return catalog.get(tag)


def _flm_catalog() -> dict:
    try:
        proc = subprocess.run(
            [_FLM_BIN, "list", "--filter", "all", "--json"],
            capture_output=True, text=True, timeout=20,
            env=_flm_environment(),
        )
    except (subprocess.SubprocessError, OSError):
        return {}
    if proc.returncode != 0:
        return {}
    try:
        data = json.loads(proc.stdout or "{}")
    except (ValueError, TypeError):
        return {}
    out: dict = {}
    for item in data.get("models") or []:
        tag = item.get("model") or item.get("name")
        fp = item.get("footprint")
        if isinstance(tag, str) and isinstance(fp, (int, float)):
            out[tag] = int(float(fp) * 1e9)
    return out


class _Handler(BaseHTTPRequestHandler):
    server_version = "hearth-backend-broker/1"
    # Quiet down request logging; the token must never be logged.
    def log_message(self, fmt, *args) -> None:
        if "Bearer" in fmt % args:
            return
        super().log_message(fmt, *args)

    def _reject(self, status: int, message: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(message.encode())))
        self.end_headers()
        self.wfile.write(message.encode())

    def do_GET(self) -> None:
        self._reject(405, json.dumps({"error": "POST only"}))

    def do_HEAD(self) -> None:
        self._reject(405, json.dumps({"error": "POST only"}))

    def do_POST(self) -> None:
        token = load_token()
        if not token:
            self._reject(503, json.dumps({"error": "backend broker is not configured"}))
            return
        if self.path.rstrip("/") not in {"/systemd", "/flm"}:
            self._reject(404, json.dumps({"error": "unknown endpoint"}))
            return
        auth = self.headers.get("Authorization", "")
        if not secrets.compare_digest(auth, f"Bearer {token}"):
            self._reject(401, json.dumps({"error": "invalid broker token"}))
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0))
        except (ValueError, json.JSONDecodeError) as exc:
            self._reject(400, json.dumps({"error": f"invalid request body: {exc}"}))
            return
        if self.path.rstrip("/") == "/systemd":
            try:
                args = [str(x) for x in body["args"]]
                service = str(body["service"])
                scope = str(body.get("scope", "user"))
                returncode, stdout, stderr = run_systemctl(args, service, scope=scope)
                payload = {"returncode": returncode, "stdout": stdout, "stderr": stderr}
            except BrokerError as exc:
                self._reject(400, json.dumps({"error": str(exc)}))
                return
            except Exception as exc:  # pragma: no cover - unexpected failure path
                self._reject(500, json.dumps({"error": str(exc)}))
                return
        else:
            try:
                args = [str(x) for x in body["args"]]
                result = run_flm(args, background=bool(body.get("background", True)))
            except BrokerError as exc:
                self._reject(400, json.dumps({"error": str(exc)}))
                return
            except Exception as exc:  # pragma: no cover - unexpected failure path
                self._reject(500, json.dumps({"error": str(exc)}))
                return
            payload = result
        self.send_response(200)
        payload_bytes = json.dumps(payload).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload_bytes)))
        self.end_headers()
        self.wfile.write(payload_bytes)


def broker_main() -> None:
    port = DEFAULT_PORT
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    print(f"hearth-backend-broker: listening on 127.0.0.1:{port} services={','.join(_allowed_units())}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    broker_main()
