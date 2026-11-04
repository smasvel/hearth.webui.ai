#!/usr/bin/env bash
# Install a user systemd service for Hearth's local q38rocm backend.
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
repo_dir="$project_dir/data/q38rocm"
model_path="$repo_dir/models/Qwen3.8-27B-Q3_K_M.gguf"
server_path="$repo_dir/engine/bin/llama-server"

[[ -x "$server_path" ]] || { echo "Build q38rocm first: bash deploy/build-q38rocm-vulkan.sh" >&2; exit 1; }
[[ -f "$model_path" ]] || { echo "Model file not found: $model_path" >&2; exit 1; }

unit_dir="$HOME/.config/systemd/user"
mkdir -p "$unit_dir"
escaped_repo=${repo_dir//%/%%}
escaped_model=${model_path//%/%%}
cat > "$unit_dir/q38rocm.service" <<UNIT
[Unit]
Description=q38rocm Qwen 3.8 27B Vulkan backend for Hearth
After=network.target

[Service]
Type=simple
WorkingDirectory=${escaped_repo}
Environment=SKIP_ROCM_CHECK=1
Environment=DEVICE=Vulkan0
Environment=PROFILE=safe
Environment=CTX=262144
ExecStart=/bin/bash ${escaped_repo}/run_server.sh --port 8008 --host 127.0.0.1 --device Vulkan0 --profile safe --ctx 262144 ${escaped_model}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
echo "Installed q38rocm.service. Hearth starts it on demand from Models; it won't load at login."
