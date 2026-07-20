#!/usr/bin/env bash
# One-command deploy for Oracle Cloud (or any Linux VM with Docker).
# Usage:  cp .env.docker.example .env.docker && nano .env.docker && ./deploy/setup.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export COMPOSE_PROFILES=bundled

if [[ ! -f .env.docker ]]; then
  echo "ERROR: .env.docker missing. Run:  cp .env.docker.example .env.docker"
  echo "       Edit MSSQL_SA_PASSWORD, OPT_DB_PASSWORD (must match), and Zerodha keys."
  exit 1
fi

# shellcheck disable=SC1091
source .env.docker

if [[ -z "${MSSQL_SA_PASSWORD:-}" || "${MSSQL_SA_PASSWORD}" == "ChangeMe!Str0ng#Pass" ]]; then
  echo "ERROR: Set a real MSSQL_SA_PASSWORD in .env.docker (and matching OPT_DB_PASSWORD)."
  exit 1
fi

echo "==> Building app image..."
docker compose build options_advisor

echo "==> Starting SQL Server..."
docker compose up -d sqlserver

echo "==> Waiting for SQL Server to become healthy (up to ~2 min)..."
for i in $(seq 1 24); do
  if docker compose ps sqlserver 2>/dev/null | grep -q "(healthy)"; then
    echo "    SQL Server is healthy."
    break
  fi
  if [[ $i -eq 24 ]]; then
    echo "ERROR: SQL Server did not become healthy. Check: docker compose logs sqlserver"
    exit 1
  fi
  sleep 5
done

echo "==> Initialising database (safe to re-run)..."
docker compose run --rm options_advisor python main.py --init-db

echo "==> Starting full stack..."
docker compose up -d

echo ""
echo "Done. Dashboard: http://$(hostname -I 2>/dev/null | awk '{print $1}'):${OPT_DASHBOARD_PORT:-5001}"
echo ""
echo "Next steps:"
echo "  1. Open port ${OPT_DASHBOARD_PORT:-5001} in Oracle Cloud security list (or use Tailscale)."
echo "  2. Each trading morning: docker compose exec options_advisor python main.py --zerodha-login"
echo "  3. Check health: docker compose ps && docker compose logs -f options_advisor"
