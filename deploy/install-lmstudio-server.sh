#!/usr/bin/env bash
# Install the LM Studio API-server systemd user unit for Hearth.
#
# Hearth needs LM Studio's OpenAI-compatible API on localhost:1234 to inventory,
# load/unload and download models. The daemon and the API server can get out of sync
# (daemon up, API stopped), so the installed unit runs the idempotent helper
# `deploy/ensure-lmstudio-server.sh` at boot and every 30 seconds; it starts what is missing.
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
  echo 'so the LM Studio API server is checked at boot and every 30 seconds. Requires systemd.'
  echo '--skip-if-active exits when the health-check timer is already running.'
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
if [[ $skip_if_active -eq 1 ]] && systemctl --user is-active --quiet lmstudio-server.timer 2>/dev/null; then
  echo 'lmstudio-server timer already active; skipping install.'
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
# `deploy/ensure-lmstudio-server.sh` starts the daemon/API when missing and exits 0 when
# both are already reachable, so this is safe on every timer run.
ExecStart=/bin/bash ${escaped_dir}/deploy/ensure-lmstudio-server.sh
Restart=on-failure
RestartSec=3
TimeoutStartSec=120

[Install]
WantedBy=default.target
UNIT

cat > "$user_dir/lmstudio-server.timer" << 'UNIT'
[Unit]
Description=Check LM Studio API health for Hearth

[Timer]
OnBootSec=10s
OnUnitInactiveSec=30s
AccuracySec=5s
Unit=lmstudio-server.service

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload
systemctl --user disable lmstudio-server.service >/dev/null 2>&1 || true
systemctl --user enable --now lmstudio-server.timer
systemctl --user restart lmstudio-server.service

status=$(systemctl --user is-active lmstudio-server.timer 2>/dev/null || true)
if [[ $status == active ]] && curl --silent --max-time 4 --output /dev/null --fail http://127.0.0.1:1234/api/v0/models; then
  echo 'lmstudio-server timer enabled; LM Studio API on port 1234 is healthy.'
else
  echo 'lmstudio-server could not be started; check:'
  echo '  systemctl --user status lmstudio-server.service'
  exit 1
fi
