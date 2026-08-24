#!/usr/bin/env bash
# Bootstrap a fresh Ubuntu 22.04/24.04 VM to host Kubernetes Lite.
# Installs Docker Engine + compose plugin and enables it at boot.
#
# Run as a sudo-capable user:
#   bash deploy/bootstrap-server.sh
set -euo pipefail

if [[ $EUID -eq 0 ]]; then
  echo "Run this script as a normal sudo-capable user, not root." >&2
  exit 1
fi

echo "==> Installing Docker Engine (official convenience script)"
curl -fsSL https://get.docker.com | sh

echo "==> Enabling Docker at boot"
sudo systemctl enable --now docker

echo "==> Allowing current user to talk to the Docker daemon"
sudo usermod -aG docker "$USER"
# Make group membership effective for this shell when possible
if command -v sg >/dev/null; then
  echo "    (new group membership applies on next login)"
fi

echo "==> Opening firewall for the dashboard (if ufw is active)"
if command -v ufw >/dev/null && sudo ufw status | grep -q "Status: active"; then
  sudo ufw allow 5000/tcp
fi

echo
echo "Done. Log out and back in (or run 'newgrp docker'), then verify with:"
echo "  docker run --rm hello-world"
