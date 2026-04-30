#!/usr/bin/env bash
# Deploy script for the Series Writer server.
# Run this on the VPS after `git pull` (or as part of CI).
#
# Idempotent: pulls latest code, rebuilds image, restarts only what changed.
set -euo pipefail

cd "$(dirname "$0")"

echo "→ Fetching latest code..."
git fetch --all
git reset --hard origin/main

if [[ ! -f .env ]]; then
  echo "✗ .env not found. Copy .env.example → .env and fill in values."
  exit 1
fi

echo "→ Rebuilding app container..."
docker compose build app

echo "→ Restarting services..."
docker compose up -d

echo "→ Pruning old images..."
docker image prune -f

echo "→ Health check..."
sleep 5
if docker compose exec -T app curl -fsS http://localhost:8080/healthz >/dev/null; then
  echo "✓ Healthy"
else
  echo "✗ Health check failed — see logs:"
  docker compose logs --tail=50 app
  exit 1
fi

echo "✓ Deploy done. Tail logs with: docker compose logs -f"
