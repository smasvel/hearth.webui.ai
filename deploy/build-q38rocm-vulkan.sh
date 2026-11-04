#!/usr/bin/env bash
# Build the q38rocm fork for Vulkan-only use on gfx1150-class Radeon 890M hardware.
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
repo_dir="$project_dir/data/q38rocm"

if [[ ! -f "$repo_dir/build_engine.sh" ]]; then
  git clone --depth 1 https://github.com/julianmb/q38rocm.git "$repo_dir"
fi

# q38rocm's upstream build targets Strix Halo gfx1151 and enables HIP unconditionally.
# This desktop has gfx1150 and the supported local path is the included RADV Vulkan engine.
if rg -q -- '-DGGML_HIP=ON' "$repo_dir/build_engine.sh"; then
  sed -i 's/-DGGML_HIP=ON/-DGGML_HIP=OFF/' "$repo_dir/build_engine.sh"
fi
sed -i '/-DAMDGPU_TARGETS=gfx1151/d' "$repo_dir/build_engine.sh"

build_jobs=${JOBS:-8}
echo "Building q38rocm's pinned llama.cpp fork with Vulkan, using ${build_jobs} jobs."
(cd "$repo_dir" && JOBS="$build_jobs" bash build_engine.sh)
