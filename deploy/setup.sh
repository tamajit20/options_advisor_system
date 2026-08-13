#!/usr/bin/env bash
# Deploy the Docker stack on a Linux VM (SQL Server + app + WS runner).
#
# Usage:
#   cp .env.docker.example .env.docker && nano .env.docker && ./deploy/setup.sh
#
# Database options (when OptionsAdvisorDB already exists):
#   ./deploy/setup.sh                      # interactive: fresh vs keep existing
#   ./deploy/setup.sh --fresh-db           # DROP + recreate empty database
#   ./deploy/setup.sh --use-existing-db    # keep data; run --init-db for schema only
#   DB_SETUP_MODE=fresh ./deploy/setup.sh  # non-interactive wipe
#
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
set -a
source .env.docker
set +a

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

# Database: create fresh, or keep existing data (see deploy/db-setup.sh).
# Schema migrations also run automatically on every container start (main.py).
# shellcheck disable=SC1091
source "$(dirname "$0")/db-setup.sh"
run_db_setup "$@"

echo "==> Starting full stack..."
docker compose up -d

echo ""
echo "Done. Dashboard: http://$(hostname -I 2>/dev/null | awk '{print $1}'):${OPT_DASHBOARD_PORT:-5001}"
echo ""
echo "Next steps:"
echo "  1. Open port ${OPT_DASHBOARD_PORT:-5001} in your cloud firewall (Azure NSG / Oracle security list)."
echo "  2. Each trading morning: docker compose exec options_advisor python main.py --zerodha-login"
echo "  3. Check health: docker compose ps && docker compose logs -f options_advisor"
