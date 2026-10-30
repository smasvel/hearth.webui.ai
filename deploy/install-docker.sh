#!/usr/bin/env bash
# Linux Docker deployment alongside an existing host installation.
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [[ ${1:-} == --help ]]; then
  echo 'Usage: bash deploy/install-docker.sh [--tailscale-https-port PORT]'
  echo 'Linux only. Creates .env.docker once; edit it before rerunning to customize ports.'
  exit 0
fi
tailscale_port=''
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tailscale-https-port)
      [[ $# -ge 2 && $2 =~ ^[0-9]+$ && ${#2} -le 5 ]] || { echo 'Provide a valid --tailscale-https-port (1-65535).' >&2; exit 1; }
      tailscale_port=$((10#$2))
      (( tailscale_port >= 1 && tailscale_port <= 65535 )) || { echo 'HTTPS port must be 1-65535.' >&2; exit 1; }
      command -v tailscale >/dev/null || { echo 'Install and connect Tailscale before requesting trusted HTTPS.' >&2; exit 1; }
      shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done
[[ $(uname -s) == Linux ]] || { echo 'This Docker deployment requires Linux host networking.' >&2; exit 1; }
docker info >/dev/null
docker compose version >/dev/null
cd "$project_dir"
if [[ ! -f .env.docker ]]; then
  state_dir="$project_dir/data/docker-hearth"
  ports=$(python3 -c 'import socket

def free_port(first, host):
    for port in range(first, first + 100):
        with socket.socket() as probe:
            try:
                probe.bind((host, port))
            except OSError:
                continue
            return port
    raise SystemExit("No free Docker setup port found")
print(free_port(55434, "127.0.0.1"), free_port(5444, "0.0.0.0"))')
  postgres_port=${ports% *}
  hearth_port=${ports#* }
  password=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
  (umask 077; set -o noclobber; printf 'POSTGRES_PASSWORD=%s\nPOSTGRES_PORT=%s\nHEARTH_PORT=%s\nHEARTH_DOCKER_STATE_DIR=%s\n' "$password" "$postgres_port" "$hearth_port" "$state_dir" > .env.docker)
fi
# Compose resolves the persistent runtime path used by both the app and agent containers.
state_dir=$(docker compose --env-file .env.docker -f compose.docker.yaml config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["hearth"]["environment"]["HEARTH_AGENT_RUNTIME_DIR"])')
mkdir -p "$state_dir"
chmod 700 "$state_dir"
docker compose --env-file .env.docker -f compose.docker.yaml up --build --detach --wait --wait-timeout 600
app_port=$(docker compose --env-file .env.docker -f compose.docker.yaml config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["hearth"]["environment"]["PORT"])')
printf 'Hearth Docker is ready: https://localhost:%s\nCreate the administrator account on first visit.\n' "$app_port"
if [[ -n $tailscale_port ]]; then
  tailscale serve --bg --https="$tailscale_port" "https+insecure://127.0.0.1:$app_port"
else
  echo 'Local HTTPS uses a self-signed certificate. For trusted HTTPS, rerun with --tailscale-https-port PORT on a connected Tailscale host.'
fi
