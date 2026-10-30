#!/usr/bin/env bash
# Standard installation entry point. Host packages are installed only when missing.
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [[ ${1:-} == --help ]]; then
  echo 'Usage: bash deploy/install-first-time.sh [--skip-browser] [--no-service] [--rebuild-agent-image]'
  echo 'Additional options are passed to tools/first_time_setup.py (--help lists them).'
  exit 0
fi
if [[ $EUID -eq 0 ]]; then
  echo 'Run as your regular user, without sudo.' >&2
  exit 1
fi
install_browser=1
setup_args=()
for argument in "$@"; do
  if [[ $argument == --skip-browser ]]; then
    install_browser=0
  else
    setup_args+=("$argument")
  fi
done
needs_host_dependencies=0
command -v python3 >/dev/null || needs_host_dependencies=1
command -v docker >/dev/null || needs_host_dependencies=1
command -v ffmpeg >/dev/null || needs_host_dependencies=1
command -v setfacl >/dev/null || needs_host_dependencies=1
if ! dpkg-query -W -f='${Status}' python3-venv 2>/dev/null | grep -qx 'install ok installed'; then
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
  sudo systemctl enable --now docker.service
  sudo usermod -aG docker "$(id -un)"
  sudo setfacl -m "u:$(id -un):rw" /var/run/docker.sock
fi
docker info >/dev/null
docker compose version >/dev/null
cd -- "$project_dir"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -c 'import pypdf' || { echo 'PDF attachment support is unavailable: pypdf did not install.' >&2; exit 1; }
if [[ $install_browser -eq 1 ]]; then
  .venv/bin/python -m playwright install chromium
fi
# The Python setup step applies the canonical SQL schema, migrates model metadata and
# legacy backend settings into PostgreSQL, and saves the encrypted application session
# key before enabling service.
.venv/bin/python tools/first_time_setup.py "${setup_args[@]}"
