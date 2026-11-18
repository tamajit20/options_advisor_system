#!/usr/bin/env bash
# Bring the Options Advisor compose stack up after reboot.
# Called by systemd options-advisor.service — must load .env.docker.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env.docker ]]; then
  echo "ERROR: $ROOT/.env.docker missing" >&2
  exit 1
fi

# shellcheck disable=SC1091
source "$(dirname "$0")/load-compose-profiles.sh"

# Wait until the Docker daemon accepts commands (boot race).
for _ in $(seq 1 30); do
  if docker info >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
if ! docker info >/dev/null 2>&1; then
  echo "ERROR: docker daemon not ready" >&2
  exit 1
fi

docker compose up -d
docker compose ps
