#!/usr/bin/env bash
# Backup OptionsAdvisorDB from the bundled sqlserver container.
#
# Usage:  ./deploy/backup.sh
# Output: ./backups/OptionsAdvisorDB-YYYYMMDD-HHMMSS.bak
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export COMPOSE_PROFILES=bundled

if [[ ! -f .env.docker ]]; then
  echo "ERROR: .env.docker missing."
  exit 1
fi

# shellcheck disable=SC1091
source .env.docker

DB="${OPT_DB_NAME:-OptionsAdvisorDB}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="backups/${DB}-${STAMP}.bak"
CONTAINER_PATH="/var/opt/mssql/backup/${DB}-${STAMP}.bak"

mkdir -p backups

if ! docker compose ps sqlserver 2>/dev/null | grep -q "Up"; then
  echo "ERROR: sqlserver container is not running. Start with: COMPOSE_PROFILES=bundled docker compose up -d sqlserver"
  exit 1
fi

echo "==> Backing up ${DB}..."
docker compose exec -T sqlserver /opt/mssql-tools18/bin/sqlcmd \
  -S localhost -U sa -P "${MSSQL_SA_PASSWORD}" -C -b -Q \
  "BACKUP DATABASE [${DB}] TO DISK = N'${CONTAINER_PATH}' WITH INIT, COMPRESSION, STATS = 10"

docker cp "options_sqlserver:${CONTAINER_PATH}" "${OUT}"

echo "==> Done: ${OUT} ($(du -h "${OUT}" | awk '{print $1}'))"
