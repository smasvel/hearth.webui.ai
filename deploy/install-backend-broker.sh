#!/usr/bin/env bash
# Install (or refresh) the Hearth backend-control broker on the Hearth host.
#
# The broker lets the containerized Hearth app call host systemd units for vLLM,
# KoboldCPP and FastFlowLM load/unload, and run the `flm` CLI on the host without the
# container having systemd or the FastFlowLM binary installed. It binds to 127.0.0.1
# only and requires a bearer token, and only accepts systemctl verbs Hearth already uses.
#
# Prerequisites: Docker Hearth already deployed with `bash deploy/install-docker.sh`
# on the same host, so `~/.config/systemd/user` can service a user unit.
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# Parse the single supported flag before the help check so a real invocation with
# `--skip-if-active` is not mistaken for a documentation request.
skip_if_active=0
if [[ ${1:-} == --skip-if-active ]]; then
  skip_if_active=1
  shift
fi
if [[ ${1:-} == --help || $# -gt 0 ]]; then
  echo 'Usage: bash deploy/install-backend-broker.sh [--skip-if-active]'
  echo 'Installs ~/.config/systemd/user/hearth-backend-broker.service on the Hearth host'
  echo 'and writes the shared bearer token to ~/.config/hearth/backend-broker/token (mode 600).'
  echo '--skip-if-active exits silently when the broker is already running and current.'
  exit 0
fi

already_active=0
if systemctl --user is-active --quiet hearth-backend-broker.service 2>/dev/null; then
  already_active=1
fi

# Nothing else to do when the service is already current; the caller can still
# force a refresh by rerunning with `systemctl --user restart hearth-backend-broker`.
if [[ $skip_if_active -eq 1 && $already_active -eq 1 ]]; then
  echo 'hearth-backend-broker already active; skipping install.'
  exit 0
fi
command -v systemctl >/dev/null || { echo 'This broker requires systemd (Linux host).' >&2; exit 1; }
command -v python3 >/dev/null || { echo 'python3 is required.' >&2; exit 1; }

user_dir=$HOME/.config/systemd/user
token_dir=$HOME/.config/hearth/backend-broker
mkdir -p "$user_dir" "$token_dir"
chmod 700 "$token_dir"
token_file=$token_dir/token
if [[ ! -s $token_file ]]; then
  (umask 077; od -An -N32 -tx1 /dev/urandom | tr -d ' \n' > "$token_file")
elif [[ $(stat -c '%a' "$token_file") =~ [^6] ]]; then
  chmod 600 "$token_file"
fi

unit=$user_dir/hearth-backend-broker.service
# `systemd` file-specifier placeholders (%n, %i, ...) would interpret the path, so escape
# every literal % in the project directory before writing the unit.
escaped_dir="${project_dir//%/%%}"
cat > "$unit" <<UNIT
[Unit]
Description=Hearth backend control broker (scoped systemctl access)
After=network.target

[Service]
ExecStart=/usr/bin/env python3 ${escaped_dir}/deploy/backend_broker.py
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now hearth-backend-broker.service

# Tell the running container how to reach it on the next install-docker run.
env_file=$project_dir/.env.docker
if [[ -f $env_file ]]; then
  [[ $(stat -c '%a' "$env_file") == "600" ]] || chmod 600 "$env_file"
  grep -q '^HEARTH_BACKEND_BROKER_URL=' "$env_file" || \
    printf 'HEARTH_BACKEND_BROKER_URL=http://127.0.0.1:8765\n' >> "$env_file"
  grep -q '^HEARTH_BACKEND_BROKER_TOKEN=' "$env_file" || \
    printf 'HEARTH_BACKEND_BROKER_TOKEN=%s\n' "$(cat "$token_file")" >> "$env_file"
  chmod 600 "$env_file"
fi

status=$(systemctl --user is-active hearth-backend-broker.service 2>/dev/null || true)
if [[ $status == active ]]; then
  echo "hearth-backend-broker is active on 127.0.0.1:8765."
  echo "Rebuild the Docker app so the container picks up the new broker settings:"
  echo "  bash deploy/install-docker.sh"
else
  echo "hearth-backend-broker could not be started; check:"
  echo "  systemctl --user status hearth-backend-broker.service"
  exit 1
fi
