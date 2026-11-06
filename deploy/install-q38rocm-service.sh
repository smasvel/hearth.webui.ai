#!/usr/bin/env bash
# Install a user systemd service for Hearth's local q38rocm backend.
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
repo_dir="$project_dir/data/q38rocm"
model_path="$repo_dir/models/Qwen3.8-27B-Q3_K_M.gguf"
context_tokens=${Q38ROCM_CONTEXT:-131072}
if [[ ! "$context_tokens" =~ ^[0-9]+$ ]] || ((context_tokens < 1024 || context_tokens > 262144)); then
  echo 'Q38ROCM_CONTEXT must be between 1024 and 262144 tokens.' >&2
  exit 1
fi
server_path="$repo_dir/engine/bin/llama-server"

[[ -x "$server_path" ]] || { echo "Build q38rocm first: bash deploy/build-q38rocm-vulkan.sh" >&2; exit 1; }
[[ -f "$model_path" ]] || { echo "Model file not found: $model_path" >&2; exit 1; }

unit_dir="$HOME/.config/systemd/user"
mkdir -p "$unit_dir"
escaped_repo=${repo_dir//%/%%}
escaped_model=${model_path//%/%%}
# This host's Radeon 890M build is Vulkan-only. Upstream reports ~0% MTP
# acceptance on Vulkan0, so use the speed profile's batching/cache settings but
# disable MTP; the measured MTP gains apply to ROCm0/Strix Halo configurations.
# The pinned Vulkan engine corrupts this qwen35 model with Flash Attention on,
# including with FP16 KV. FP16 KV + Flash Attention off passes the tool contract.
# Keep the checkpoint budget bounded alongside the larger FP16 KV allocation.
cat > "$unit_dir/q38rocm.service" <<UNIT
[Unit]
Description=q38rocm Qwen 3.8 27B Vulkan backend for Hearth
After=network.target

[Service]
Type=simple
WorkingDirectory=${escaped_repo}
Environment=SKIP_ROCM_CHECK=1
Environment=DEVICE=Vulkan0
Environment=PROFILE=speed
Environment=CTX=${context_tokens}
Environment=CACHE_RAM_MIB=2048
ExecStart=/bin/bash ${escaped_repo}/run_server.sh --port 8008 --host 127.0.0.1 --device Vulkan0 --profile speed --no-mtp --no-reasoning --kv-k f16 --kv-v f16 --ubatch 64 --ctx ${context_tokens} ${escaped_model} --flash-attn off
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
echo "Installed q38rocm.service. Hearth starts it on demand from Models; it won't load at login."
