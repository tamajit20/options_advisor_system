#!/usr/bin/env bash
# Routine deploy: pull latest code, rebuild images, restart stack.
#
# Database schema (new tables + column migrations) is applied automatically
# when containers start — no manual `python main.py --init-db` required.
#
# Usage (on the VM, from repo root):
#   ./deploy/update.sh
#
# First-time install still uses ./deploy/setup.sh or ./deploy/vm-install-deploy.sh
# (creates .env.docker, waits for SQL Server, optional fresh DB prompt).
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export COMPOSE_PROFILES=bundled
BRANCH="${REPO_BRANCH:-master}"

if [[ ! -f .env.docker ]]; then
  echo "ERROR: .env.docker missing. Run ./deploy/setup.sh for first-time install."
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env.docker
set +a

echo "==> Pulling latest code (${BRANCH})..."
git fetch origin "${BRANCH}"
git checkout "${BRANCH}"
git pull origin "${BRANCH}"

echo "==> Rebuilding application image..."
docker compose build options_advisor

echo "==> Restarting stack (schema migrations run on container startup)..."
docker compose up -d

echo ""
echo "Deploy complete. Check logs:"
echo "  docker compose logs -f options_advisor | grep -i schema"
echo ""
