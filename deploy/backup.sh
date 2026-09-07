#!/usr/bin/env bash
# Backup OptionsAdvisorDB from the bundled sqlserver container.
#
# Host/VM helper (needs docker compose). The scheduler db_backup job does NOT
# call this — it uses lifecycle/sql_backup.py over pyodbc.
#
# Usage:  ./deploy/backup.sh
# Output: ./backups/OptionsAdvisorDB-YYYYMMDD-HHMMSS.bak
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export COMPOSE_PROFILES=bundled

# Compose injects .env.docker as the container environment. The file itself
# is dockerignored, so source it only when present (host / VM shell).
if [[ -f .env.docker ]]; then
  # shellcheck disable=SC1091
  set -a
  source .env.docker
  set +a
elif [[ -z "${MSSQL_SA_PASSWORD:-}" ]]; then
  echo "ERROR: .env.docker missing and MSSQL_SA_PASSWORD is not set."
  exit 1
fi

DB="${OPT_DB_NAME:-OptionsAdvisorDB}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="backups/${DB}-${STAMP}.bak"
BIND_DIR="/var/opt/mssql/host-backups"
INTERNAL_DIR="/var/opt/mssql/backup"

mkdir -p backups
chmod 777 backups 2>/dev/null || true

if ! docker compose ps sqlserver 2>/dev/null | grep -q "Up"; then
  echo "ERROR: sqlserver container is not running. Start with: COMPOSE_PROFILES=bundled docker compose up -d sqlserver"
  exit 1
fi

echo "==> Backing up ${DB}..."
if docker compose exec -T sqlserver bash -c "test -w '${BIND_DIR}'"; then
  CONTAINER_PATH="${BIND_DIR}/${DB}-${STAMP}.bak"
  docker compose exec -T sqlserver /opt/mssql-tools18/bin/sqlcmd \
    -S localhost -U sa -P "${MSSQL_SA_PASSWORD}" -C -b -Q \
    "BACKUP DATABASE [${DB}] TO DISK = N'${CONTAINER_PATH}' WITH INIT, STATS = 10"
  if [[ ! -f "${OUT}" ]]; then
    echo "ERROR: backup finished but ${OUT} is missing (bind-mount ./backups)."
    exit 1
  fi
else
  CONTAINER_PATH="${INTERNAL_DIR}/${DB}-${STAMP}.bak"
  docker compose exec -T sqlserver /opt/mssql-tools18/bin/sqlcmd \
    -S localhost -U sa -P "${MSSQL_SA_PASSWORD}" -C -b -Q \
    "BACKUP DATABASE [${DB}] TO DISK = N'${CONTAINER_PATH}' WITH INIT, STATS = 10"
  docker cp "options_sqlserver:${CONTAINER_PATH}" "${OUT}"
fi

echo "==> Done: ${OUT} ($(du -h "${OUT}" | awk '{print $1}'))"
