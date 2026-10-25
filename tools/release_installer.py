"""Interactive installer used by the Windows, macOS, and Linux release bundles.

It installs Hearth and its database, then saves the selected inference endpoint.
Inference engines and their model weights remain separate platform-specific installs.
"""
from __future__ import annotations

import argparse
import getpass
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BACKENDS = {
    "lmstudio": ("LM Studio", "http://localhost:1234", {"Windows", "Darwin", "Linux"}),
    "kobold": ("KoboldCPP", "http://127.0.0.1:5001", {"Windows", "Darwin", "Linux"}),
    "vllm": ("vLLM API server", "http://localhost:8000", {"Windows", "Darwin", "Linux"}),
    "sglang": ("SGLang API server", "http://127.0.0.1:30000", {"Windows", "Darwin", "Linux"}),
    "lemonade": ("Lemonade Server", "http://localhost:13305", {"Linux"}),
    "fastflowlm": ("FastFlowLM (AMD Ryzen AI NPU)", "http://127.0.0.1:52625", {"Linux"}),
}


def choose_backend(system=None, input_fn=input):
    system = system or platform.system()
    options = [(key, *value[:2]) for key, value in BACKENDS.items() if system in value[2]]
    print("Choose the backend Hearth should connect to:")
    for index, (key, label, url) in enumerate(options, 1):
        print(f"  {index}. {label} ({url})")
    while True:
        answer = input_fn(f"Backend [1]: ").strip() or "1"
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            key, label, url = options[int(answer) - 1]
            break
        print("Enter one of the listed numbers.")
    endpoint = input_fn(f"{label} URL [{url}]: ").strip() or url
    return key, endpoint


def _run(command, *, cwd=ROOT):
    subprocess.run(command, cwd=cwd, check=True)


def install(args=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=BACKENDS, help="Backend key to configure instead of asking")
    parser.add_argument("--backend-url", help="Backend URL; defaults to its local endpoint")
    parser.add_argument("--postgres-port", type=int, default=55432)
    parser.add_argument("--skip-browser", action="store_true", help="Skip Playwright Chromium installation")
    parser.add_argument("--skip-agent-image", action="store_true", help="Skip the Docker image used by coding agents")
    options = parser.parse_args(args)
    system = platform.system()
    if system not in {"Windows", "Darwin", "Linux"}:
        parser.error(f"Unsupported operating system: {system}")
    if sys.version_info < (3, 12):
        raise SystemExit("Hearth needs Python 3.12 or newer. Install Python, then run this installer again.")
    if not shutil.which("docker"):
        raise SystemExit("Docker is required. Install Docker Desktop or Docker Engine, start it, and retry.")
    _run(["docker", "info"], cwd=ROOT)
    _run(["docker", "compose", "version"], cwd=ROOT)

    backend = options.backend
    backend_url = options.backend_url
    if backend is None:
        backend, prompted_url = choose_backend(system)
        backend_url = backend_url or prompted_url
    else:
        supported = BACKENDS.get(backend)
        if not supported or system not in supported[2]:
            raise SystemExit(f"{backend} is not offered for {system} in this installer.")
        backend_url = backend_url or supported[1]

    venv_python = ROOT / (".venv/Scripts/python.exe" if system == "Windows" else ".venv/bin/python")
    if os.environ.get("HEARTH_RELEASE_INSTALL_IN_VENV") != "1":
        if not venv_python.exists():
            _run([sys.executable, "-m", "venv", ".venv"])
        _run([str(venv_python), "-m", "pip", "install", "--upgrade", "pip"])
        _run([str(venv_python), "-m", "pip", "install", "-r", "requirements.txt"])
        if not options.skip_browser:
            _run([str(venv_python), "-m", "playwright", "install", "chromium"])
        child_env = os.environ.copy()
        child_env["HEARTH_RELEASE_INSTALL_IN_VENV"] = "1"
        forwarded = list(sys.argv[1:] if args is None else args)
        if "--backend" not in forwarded:
            forwarded += ["--backend", backend]
        if "--backend-url" not in forwarded:
            forwarded += ["--backend-url", backend_url]
        subprocess.run([str(venv_python), str(Path(__file__).resolve()), *forwarded],
                       cwd=ROOT, env=child_env, check=True)
        return

    sys.path.insert(0, str(ROOT))
    from tools import first_time_setup as setup

    if not options.skip_agent_image and system == "Linux":
        setup.install_agent_runtime()
    elif system != "Linux":
        print("The isolated coding-agent image is supported by the Linux installer; agent execution is unavailable on this platform.")
    values = setup.prepare_environment(options.postgres_port)
    os.environ["DATABASE_URL"] = values["DATABASE_URL"]
    compose = ["docker", "compose", "--env-file", str(ROOT / ".env.postgres"), "-f", str(ROOT / "compose.yaml")]
    _run(compose + ["up", "-d", "--wait", "postgres"])
    setup.initialize_application_storage()

    import settings_store
    settings_store.set(f"{backend}_base_url", backend_url)
    secret = getpass.getpass(f"{BACKENDS[backend][0]} API key (leave blank if none): ")
    settings_store.set(f"{backend}_api_key", secret)
    print(f"\nHearth is installed and configured for {BACKENDS[backend][0]} at {backend_url}.")
    print("The backend runtime and model weights are separate; install and start that backend before chatting.")
    print("Start Hearth with deploy/start-hearth.ps1 on Windows, or bash deploy/start-hearth.sh on macOS/Linux.")
    print("Then open https://localhost:5443 and create the administrator account on first use.")


if __name__ == "__main__":
    install()
