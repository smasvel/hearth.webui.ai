#!/usr/bin/env bash
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
  echo 'Run this script with sudo in your terminal.' >&2
  exit 1
fi
if [[ ${1:-} == --base-only ]]; then
  target_user=${SUDO_USER:-}
  if [[ -z $target_user || $target_user == root ]]; then
    echo 'Run with sudo from the account that will run Hearth.' >&2
    exit 1
  fi
  apt-get update
  apt-get install -y python3 python3-venv docker.io docker-compose-v2 acl curl ffmpeg
  systemctl enable --now docker.service
  usermod -aG docker "$target_user"
  echo "Base dependencies installed. Log out and back in before running deploy/install-first-time.sh."
  exit 0
fi
if [[ $# -ne 0 ]]; then
  echo 'Usage: sudo bash deploy/install-host-deps.sh [--base-only]' >&2
  exit 1
fi
# The remaining full-backend setup is specific to the original example-host host.
apt-get update
apt-get install -y python3 python3-venv docker.io docker-compose-v2 acl curl git ffmpeg lynis caddy build-essential cmake pkg-config libatomic1 libvulkan1 mesa-vulkan-drivers vulkan-tools libsndfile1 espeak-ng software-properties-common libtcmalloc-minimal4t64
systemctl enable --now docker.service
if ! snap list lemonade-server >/dev/null 2>&1 && ! dpkg-query -W -f='${Status}' lemonade-server 2>/dev/null | grep -q 'install ok installed'; then
  apt-get install -y lemonade-server
fi
apt-get install -y /home/YOUR_USERNAME/.local/share/hearth/downloads/fastflowlm.deb
usermod -aG render,video,docker HEARTH_USER
install -o root -g root -m 0644 /path/to/ai-webui/flm.service /etc/systemd/system/flm.service
install -o root -g root -m 0644 /path/to/ai-webui/50-flm.rules /etc/polkit-1/rules.d/50-flm.rules
systemctl daemon-reload
if [[ -f /home/YOUR_USERNAME/.local/share/hearth/Caddyfile ]]; then
  if ! grep -qx '# Hearth authenticated backend proxies' /etc/caddy/Caddyfile; then
    cp -a /etc/caddy/Caddyfile /etc/caddy/Caddyfile.before-hearth
    printf '\n# Hearth authenticated backend proxies\n' >> /etc/caddy/Caddyfile
    cat /home/YOUR_USERNAME/.local/share/hearth/Caddyfile >> /etc/caddy/Caddyfile
  fi
  install -d -o caddy -g caddy -m 0755 /var/log/caddy
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
  systemctl reload caddy
fi
# Ubuntu 26.04 includes Lemonade in its own package repository.
/path/to/ai-webui/.venv/bin/python -m playwright install-deps chromium
install -o root -g root -m 0440 /path/to/ai-webui/deploy/hearth-lynis-sudoers /etc/sudoers.d/hearth-lynis
visudo -cf /etc/sudoers.d/hearth-lynis
loginctl enable-linger HEARTH_USER
tailscale serve --bg --https=8443 https+insecure://127.0.0.1:5443
