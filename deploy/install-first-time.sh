#!/usr/bin/env bash
# Unprivileged app setup; install OS packages using install-host-deps.sh first.
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [[ ${1:-} == --help ]]; then
  echo 'Usage: bash deploy/install-first-time.sh [--skip-browser] [--no-service] [--admin-username NAME]'
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
command -v docker >/dev/null || { echo 'Install host dependencies first: sudo bash deploy/install-host-deps.sh --base-only' >&2; exit 1; }
docker info >/dev/null
cd -- "$project_dir"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
if [[ $install_browser -eq 1 ]]; then
  .venv/bin/python -m playwright install chromium
fi
.venv/bin/python tools/first_time_setup.py "${setup_args[@]}"
