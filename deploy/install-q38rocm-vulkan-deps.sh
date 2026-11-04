#!/usr/bin/env bash
# Install the build dependencies for q38rocm's Vulkan engine on Ubuntu/Debian.
# Run from the Hearth repository: bash deploy/install-q38rocm-vulkan-deps.sh
set -euo pipefail

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This setup script requires apt-get (Ubuntu or Debian)." >&2
  exit 1
fi

sudo apt-get update
sudo apt-get install -y \
  build-essential \
  cmake \
  git \
  glslc \
  libvulkan-dev \
  libvulkan1 \
  mesa-vulkan-drivers \
  spirv-headers \
  vulkan-tools

if ! vulkaninfo --summary 2>/dev/null | grep -Eqi 'deviceName.*(AMD|Radeon)'; then
  echo 'Vulkan is installed, but vulkaninfo did not report an AMD/Radeon device.' >&2
  echo 'Check the Mesa RADV driver and GPU permissions before building q38rocm.' >&2
else
  echo 'AMD/Radeon Vulkan device detected; q38rocm will use hardware acceleration.'
fi
