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
# The app reads the host's tailnet identity through the Tailscale CLI. A container has
# neither the binary nor the daemon socket, so mount both when the host runs Tailscale;
# without this the Help panel reports Tailscale as not detected inside Docker.
compose=(docker compose --env-file .env.docker -f compose.docker.yaml)
tailscale_bin=$(command -v tailscale || true)
tailscale_socket_dir=''
if [[ -n $tailscale_bin ]]; then
  for candidate in /run/tailscale /var/run/tailscale; do
    if [[ -S $candidate/tailscaled.sock ]]; then
      tailscale_socket_dir=$candidate
      break
    fi
  done
fi
if [[ -n $tailscale_port ]] && ! tailscale status >/dev/null 2>&1; then
  echo 'Tailscale is installed but not connected. Connect it before requesting trusted HTTPS.' >&2
  exit 1
fi
if [[ -n $tailscale_bin && -n $tailscale_socket_dir ]]; then
  export TAILSCALE_BIN=$tailscale_bin TAILSCALE_SOCKET_DIR=$tailscale_socket_dir
  compose+=(-f compose.tailscale.yaml)
fi
# The model gateway is host-level Caddy. Generate its random bearer tokens and local CA
# when Caddy is installed, publish the managed block when sudo can run without a prompt,
# and mount the host Caddyfile read-only so Help shows the real keys instead of a
# placeholder. Caddy and its CA remain on the host.
caddy_file=/etc/caddy/Caddyfile
if command -v caddy >/dev/null; then
  python3 "$project_dir/tools/caddy_gateway_setup.py" \
    --state-file "$HOME/.local/share/hearth/Caddyfile" --owner "$(id -un)" || true
  if [[ ! -r $caddy_file ]] || ! grep -q 'Hearth authenticated backend proxies' "$caddy_file"; then
    if sudo -n true 2>/dev/null; then
      sudo -n python3 "$project_dir/tools/caddy_gateway_setup.py" \
        --state-file "$HOME/.local/share/hearth/Caddyfile" --owner "$(id -un)" \
        --system-file "$caddy_file" || true
    else
      echo "Host Caddy is installed. Publish the generated gateway keys with:"
      echo "  sudo python3 $project_dir/tools/caddy_gateway_setup.py --system-file $caddy_file"
    fi
  fi
fi
if [[ -r $caddy_file ]]; then
  compose+=(-f compose.caddy.yaml)
fi
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
state_dir=$("${compose[@]}" config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["hearth"]["environment"]["HEARTH_AGENT_RUNTIME_DIR"])')
mkdir -p "$state_dir"
chmod 700 "$state_dir"
"${compose[@]}" up --build --detach --wait --wait-timeout 600
app_port=$("${compose[@]}" config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["hearth"]["environment"]["PORT"])')
printf 'Hearth Docker is ready: https://localhost:%s\nCreate the administrator account on first visit.\n' "$app_port"
if [[ -n $tailscale_socket_dir ]]; then
  printf 'Tailscale detected on the host; Help shows the tailnet address and Serve route.\n'
fi
if [[ -n $tailscale_port ]]; then
  tailscale serve --bg --https="$tailscale_port" "https+insecure://127.0.0.1:$app_port"
else
  echo 'Local HTTPS uses a self-signed certificate. For trusted HTTPS, rerun with --tailscale-https-port PORT on a connected Tailscale host.'
fi
