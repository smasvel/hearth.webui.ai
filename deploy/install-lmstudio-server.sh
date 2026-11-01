#!/usr/bin/env bash
# Install the LM Studio API-server systemd user unit for Hearth.
#
# Hearth needs LM Studio's OpenAI-compatible API on localhost:1234 to inventory,
# load/unload and download models. The daemon and the API server can get out of sync
# (daemon up, API stopped), so the installed unit runs the idempotent helper
# `deploy/ensure-lmstudio-server.sh` at boot and on login; it starts what is missing.
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
skip_if_active=0
if [[ ${1:-} == --skip-if-active ]]; then
  skip_if_active=1
  shift
fi
if [[ ${1:-} == --help || $# -gt 0 ]]; then
  echo 'Usage: bash deploy/install-lmstudio-server.sh [--skip-if-active]'
  echo 'Installs ~/.config/systemd/user/lmstudio-server.service for the Hearth host'
  echo 'so the LM Studio API server is brought up at boot. Requires systemd.'
  echo '--skip-if-active exits silently when the service is already running.'
  exit 0
fi
if ! command -v systemctl >/dev/null; then
  echo 'This requires systemd (Linux host).' >&2
  exit 1
fi

user_dir=$HOME/.config/systemd/user
mkdir -p "$user_dir"

# `systemctl --user is-active` is the condition the caller already told us about; place
# the check *before* any write so an existing, healthy install is untouched.
if [[ $skip_if_active -eq 1 ]] && systemctl --user is-active --quiet lmstudio-server.service 2>/dev/null; then
  echo 'lmstudio-server already active; skipping install.'
  exit 0
fi

unit=$user_dir/lmstudio-server.service
# `systemd` file-specifier placeholders (%n, %i, ...) would interpret the path, so
# escape every literal % in the project directory before writing the unit.
escaped_dir="${project_dir//%/%%}"
cat > "$unit" << UNIT
[Unit]
Description=Hearth LM Studio API server bootstrap
After=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
# `deploy/ensure-lmstudio-server.sh` starts the daemon/API when missing and exits 0 when
# both are already reachable, so this is safe on every boot and login.
ExecStart=/bin/bash ${escaped_dir}/deploy/ensure-lmstudio-server.sh
Restart=on-failure
RestartSec=3
TimeoutStartSec=120

[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now lmstudio-server.service

status=$(systemctl --user is-active lmstudio-server.service 2>/dev/null || true)
if [[ $status == active ]]; then
  echo 'lmstudio-server service enabled; LM Studio API on port 1234 is healthy.'
else
  echo 'lmstudio-server could not be started; check:'
  echo '  systemctl --user status lmstudio-server.service'
  exit 1
fi
