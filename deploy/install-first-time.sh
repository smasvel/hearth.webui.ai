#!/usr/bin/env bash
# Standard installation entry point. Host packages are installed only when missing.
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [[ ${1:-} == --help ]]; then
  echo 'Usage: bash deploy/install-first-time.sh [--skip-browser] [--no-service] [--rebuild-agent-image] [--skip-backend-broker] [--skip-lmstudio-server]'
  echo 'Additional options are passed to tools/first_time_setup.py (--help lists them).'
  exit 0
fi
if [[ $EUID -eq 0 ]]; then
  echo 'Run as your regular user, without sudo.' >&2
  exit 1
fi
install_browser=1
install_service=1
install_backend_broker=1
install_lmstudio_server=1
setup_args=()
for argument in "$@"; do
  if [[ $argument == --skip-browser ]]; then
    install_browser=0
  elif [[ $argument == --skip-backend-broker ]]; then
    install_backend_broker=0
  elif [[ $argument == --skip-lmstudio-server ]]; then
    install_lmstudio_server=0
  else
    if [[ $argument == --no-service ]]; then install_service=0; fi
    setup_args+=("$argument")
  fi
done
if [[ $install_service -eq 1 && ! -d /run/systemd/system ]]; then
  echo 'systemd is not running. Rerun with --no-service and start Hearth manually.' >&2
  exit 1
fi
needs_host_dependencies=0
command -v python3 >/dev/null || needs_host_dependencies=1
command -v docker >/dev/null || needs_host_dependencies=1
command -v ffmpeg >/dev/null || needs_host_dependencies=1
command -v setfacl >/dev/null || needs_host_dependencies=1
# A usable .venv is enough; the apt python3-venv package is only needed to create one.
if [[ ! -x .venv/bin/python ]] && ! dpkg-query -W -f='${Status}' python3-venv 2>/dev/null | grep -qx 'install ok installed'; then
  needs_host_dependencies=1
fi
if command -v docker >/dev/null && ! docker compose version >/dev/null 2>&1; then
  needs_host_dependencies=1
fi
if command -v docker >/dev/null && ! docker info >/dev/null 2>&1; then
  needs_host_dependencies=1
fi
if [[ $needs_host_dependencies -eq 1 ]]; then
  command -v sudo >/dev/null || { echo 'sudo is required to install missing Ubuntu host packages.' >&2; exit 1; }
  command -v apt-get >/dev/null || { echo 'Automatic host dependency setup currently requires Ubuntu or Debian.' >&2; exit 1; }
  sudo -v
  sudo apt-get update
  sudo apt-get install -y python3 python3-venv docker.io docker-compose-v2 acl curl ffmpeg
  if ! docker info >/dev/null 2>&1; then
    if [[ ! -d /run/systemd/system ]]; then
      echo 'Docker is unavailable and systemd is not running. Start Docker or provide an accessible Docker socket, then retry.' >&2
      exit 1
    fi
    sudo systemctl enable --now docker.service
    sudo usermod -aG docker "$(id -un)"
    sudo setfacl -m "u:$(id -un):rw" /var/run/docker.sock
  fi
fi
docker info >/dev/null
docker compose version >/dev/null
cd -- "$project_dir"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' || {
  echo 'Python 3.12 or newer is required. Install a supported Python version, then retry.' >&2
  exit 1
}
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -c 'import pypdf' || { echo 'PDF attachment support is unavailable: pypdf did not install.' >&2; exit 1; }
if [[ $install_browser -eq 1 ]]; then
  .venv/bin/python -m playwright install --with-deps chromium
fi
# The Python setup step applies the canonical SQL schema, migrates model metadata and
# legacy backend settings into PostgreSQL, and saves the encrypted application session
# key before enabling service.
.venv/bin/python tools/first_time_setup.py "${setup_args[@]}"
# Install the Docker backend-control broker so a containerized Hearth install can load
# and unload the shared host backends (vLLM / KoboldCPP / FastFlowLM). The unit is
# idempotent; pass --skip-backend-broker to opt out.
if [[ $install_service -eq 1 ]]; then
  if [[ $install_backend_broker -eq 0 ]]; then
    echo 'Skipping the Docker backend-control broker (--skip-backend-broker).'
  elif [[ -d /run/systemd/system ]]; then
    bash deploy/install-backend-broker.sh --skip-if-active
  fi
fi
# Keep LM Studio's API server reachable too: inventory / load / unload go through the
# shared `~/.lmstudio` CLI. When LM Studio is not installed the helper is a no-op, so
# installing the unit is harmless either way.
if [[ $install_service -eq 1 ]]; then
  if [[ $install_lmstudio_server -eq 0 ]]; then
    echo 'Skipping the LM Studio API bootstrap service (--skip-lmstudio-server).'
  elif [[ -d /run/systemd/system ]]; then
    bash deploy/install-lmstudio-server.sh --skip-if-active
  fi
fi
# Keep the desktop OpenCode config aligned with live backends every two minutes:
# model context limits and the compaction keep/buffer pair are re-derived from the
# selected model's advertised window (help_data.compaction_for_window), so changing
# where a model is loaded never leaves OpenCode with a stale budget. Idempotent and
# only writes when the generated config differs from the current file.
if [[ $install_service -eq 1 && -d /run/systemd/system ]]; then
  mkdir -p "$HOME/.config/systemd/user"
  # Hearth no longer auto-updates the local OpenCode config. The generated config
  # is shown in Help for manual copy/save.
  echo 'OpenCode config auto-sync is disabled; use Help to copy the generated config.'
fi
# When Caddy is installed (the full backend setup), generate the gateway bearer tokens and
# local CA too. The basic install does not install Caddy, so this is a no-op unless the
# administrator already provided it. Existing tokens are preserved.
if command -v caddy >/dev/null; then
  python3 tools/caddy_gateway_setup.py --state-file "$HOME/.local/share/hearth/Caddyfile" --owner "$(id -un)" || true
  if [[ ! -r /etc/caddy/Caddyfile ]] || ! grep -q 'Hearth authenticated backend proxies' /etc/caddy/Caddyfile; then
    if sudo -n true 2>/dev/null; then
      sudo -n python3 tools/caddy_gateway_setup.py \
        --state-file "$HOME/.local/share/hearth/Caddyfile" --owner "$(id -un)" \
        --system-file /etc/caddy/Caddyfile || true
    else
      echo "Publish the generated gateway keys with:"
      echo "  sudo python3 $project_dir/tools/caddy_gateway_setup.py --system-file /etc/caddy/Caddyfile"
    fi
  fi
fi
