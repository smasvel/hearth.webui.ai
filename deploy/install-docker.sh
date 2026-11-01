#!/usr/bin/env bash
# Linux Docker deployment alongside an existing host installation.
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [[ ${1:-} == --help ]]; then
  echo 'Usage: bash deploy/install-docker.sh [--tailscale-https-port PORT] [--skip-lmstudio-server] [--skip-backend-broker]'
  echo 'Linux only. Creates .env.docker once; edit it before rerunning to customize ports.'
  exit 0
fi
skip_lmstudio_server=0
skip_backend_broker=0
tailscale_port=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tailscale-https-port)
      [[ $# -ge 2 && $2 =~ ^[0-9]+$ && ${#2} -le 5 ]] || { echo 'Provide a valid --tailscale-https-port (1-65535).' >&2; exit 1; }
      tailscale_port=$((10#$2))
      (( tailscale_port >= 1 && tailscale_port <= 65535 )) || { echo 'HTTPS port must be 1-65535.' >&2; exit 1; }
      command -v tailscale >/dev/null || { echo 'Install and connect Tailscale before requesting trusted HTTPS.' >&2; exit 1; }
      shift 2 ;;
    --skip-lmstudio-server)
      skip_lmstudio_server=1
      shift ;;
    --skip-backend-broker)
      skip_backend_broker=1
      shift ;;
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
state_dir="$project_dir/data/docker-hearth"
local_models_dir="$project_dir/data/local-models"
if [[ ! -f .env.docker ]]; then
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
  (umask 077; set -o noclobber; printf 'POSTGRES_PASSWORD=%s\nPOSTGRES_PORT=%s\nHEARTH_PORT=%s\nHEARTH_DOCKER_STATE_DIR=%s\nHEARTH_HOST_HOME=%s\nHEARTH_LOCAL_MODELS_DIR=%s\n' \
    "$password" "$postgres_port" "$hearth_port" "$state_dir" "$HOME" "$local_models_dir" > .env.docker)
fi
# Existing .env.docker files from older installs predate the shared model paths. Add the
# defaults without touching any ports, passwords, or user edits already present.
ensure_env() {
  local key=$1 value=$2
  grep -q "^${key}=" .env.docker || printf '%s=%s\n' "$key" "$value" >> .env.docker
}
ensure_env HEARTH_HOST_HOME "$HOME"
ensure_env HEARTH_LOCAL_MODELS_DIR "$local_models_dir"
# The host Hearth install runs as this user; the Docker container chowns shared state
# (FastFlowLM paused/active files) to it so both installs can drive the same backend.
ensure_env HEARTH_HOST_UID "$(id -u)"
ensure_env HEARTH_HOST_GID "$(id -g)"
chmod 600 .env.docker
# Compose resolves the persistent runtime path used by both the app and agent containers.
state_dir=$("${compose[@]}" config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["hearth"]["environment"]["HEARTH_AGENT_RUNTIME_DIR"])')
mkdir -p "$state_dir" "$local_models_dir" "$HOME/.lmstudio" "$HOME/.cache/huggingface"
chmod 700 "$state_dir"
# Install the Docker backend-control broker on this host, so the containerized Hearth app can
# start/stop the shared host backends (vLLM / KoboldCPP / FastFlowLM) and run `flm` on
# the host. Idempotent; skip when another install already created it fresh.
if [[ $skip_backend_broker -eq 1 ]]; then
  echo 'Skipping the backend-control broker (--skip-backend-broker).'
elif command -v systemctl >/dev/null && [[ -d /run/systemd/system ]]; then
  bash "$project_dir/deploy/install-backend-broker.sh" --skip-if-active || true
else
  echo 'Skipping the backend-control broker: systemd user services are not available here.'
fi
# Keep LM Studio's API server reachable too: inventory / load / unload go through the
# shared `~/.lmstudio` CLI. Idempotent boot/start helper behind a systemd user unit.
if [[ $skip_lmstudio_server -eq 0 ]]; then
  if command -v systemctl >/dev/null && [[ -d /run/systemd/system ]]; then
    # When LM Studio isn't installed the helper is a no-op, so the unit is harmless.
    bash "$project_dir/deploy/install-lmstudio-server.sh" --skip-if-active || true
  fi
else
  echo 'Skipping the LM Studio API bootstrap service (--skip-lmstudio-server).'
fi
# Mount the host's systemd-managed backend state directories into the container at the
# same absolute path so the *inventory* (registry, service.env, FLM active-file) is shared
# with the Docker Hearth install. The **broker** (deploy/install-backend-broker.sh) runs
# the actual systemctl commands on the Hearth host; the mounts are what make the
# registration and service files visible to the running app.
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
