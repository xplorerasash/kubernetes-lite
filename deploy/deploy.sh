#!/usr/bin/env bash
# Deploy (or update) Kubernetes Lite on a VM from a registry image.
# Safe to re-run: only recreates the container when the image changed.
#
#   IMAGE=dockerhubuser/kubernetes-lite:1.0.0 bash deploy/deploy.sh
set -euo pipefail

IMAGE="${K8SLITE_IMAGE:-dockerhubuser/kubernetes-lite:latest}"
COMPOSE_FILE="deploy/docker-compose.prod.yml"
cd "$(dirname "$0")/.."

echo "==> Pulling ${IMAGE}"
docker pull "${IMAGE}"

echo "==> Starting stack"
K8SLITE_IMAGE="${IMAGE}" docker compose -f "${COMPOSE_FILE}" up -d

echo "==> Waiting for health check"
for i in $(seq 1 30); do
  if curl -sf http://localhost:5000/api/health >/dev/null; then
    echo "Healthy."
    curl -s http://localhost:5000/api/health; echo
    break
  fi
  sleep 2
done

echo "==> Cleaning up old images"
docker image prune -f >/dev/null

echo "Deploy complete: http://$(curl -s ifconfig.me):5000"
