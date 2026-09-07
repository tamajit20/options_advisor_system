#!/usr/bin/env bash
# Restore OptionsAdvisorDB from a .bak file into the bundled sqlserver container.
#
# Usage:  ./deploy/restore.sh backups/OptionsAdvisorDB-20260712-120000.bak
#
# WARNING: overwrites the current database (REPLACE).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export COMPOSE_PROFILES=bundled

BAK="${1:-}"
if [[ -z "${BAK}" || ! -f "${BAK}" ]]; then
  echo "Usage: $0 path/to/OptionsAdvisorDB-YYYYMMDD-HHMMSS.bak"
  exit 1
fi

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
BASENAME="$(basename "${BAK}")"
INTERNAL_PATH="/var/opt/mssql/backup/${BASENAME}"
CONTAINER_PATH="${INTERNAL_PATH}"

if ! docker compose ps sqlserver 2>/dev/null | grep -q "Up"; then
  echo "ERROR: sqlserver container is not running."
  exit 1
fi

echo "==> Stopping app containers (so nothing holds DB connections)..."
docker compose stop options_advisor ws_runner 2>/dev/null || true

# Prefer the host bind-mount when the .bak already lives under ./backups.
BAK_ABS="$(cd "$(dirname "$BAK")" && pwd)/$(basename "$BAK")"
BAK_REL="${BAK_ABS#"$ROOT"/}"
BIND_REL=""
if [[ "$BAK_REL" == backups/* ]]; then
  BIND_REL="${BAK_REL#backups/}"
fi
if [[ -n "$BIND_REL" ]] && docker compose exec -T sqlserver test -f "/var/opt/mssql/host-backups/${BIND_REL}"; then
  CONTAINER_PATH="/var/opt/mssql/host-backups/${BIND_REL}"
  echo "==> Using bind-mounted backup ${BIND_REL}"
else
  echo "==> Copying backup into container..."
  docker compose exec -T sqlserver mkdir -p /var/opt/mssql/backup
  docker cp "${BAK}" "options_sqlserver:${INTERNAL_PATH}"
  CONTAINER_PATH="${INTERNAL_PATH}"
fi

echo "==> Restoring ${DB} from ${BASENAME}..."
# Laptop SQLEXPRESS backups store Windows paths; MOVE files into Linux container data dir.
DATA_DIR="/var/opt/mssql/data"
docker compose exec -T sqlserver /opt/mssql-tools18/bin/sqlcmd \
  -S localhost -U sa -P "${MSSQL_SA_PASSWORD}" -C -b -Q \
  "ALTER DATABASE [${DB}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
   RESTORE DATABASE [${DB}] FROM DISK = N'${CONTAINER_PATH}' WITH REPLACE, RECOVERY,
     MOVE N'${DB}' TO N'${DATA_DIR}/${DB}.mdf',
     MOVE N'${DB}_log' TO N'${DATA_DIR}/${DB}_log.ldf';
   ALTER DATABASE [${DB}] SET MULTI_USER;"

echo "==> Restarting app..."
docker compose up -d options_advisor ws_runner

echo "==> Restore complete."
