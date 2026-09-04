#!/usr/bin/env bash
# Build OptionsAdvisorDB_ArchiveExport from pending *_Archive rows, write .bak + PENDING.json.
#
# Usage: ./deploy/archive-export.sh
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

MAIN_DB="${OPT_DB_NAME:-OptionsAdvisorDB}"
EXPORT_DB="${OPT_ARCHIVE_EXPORT_DB_NAME:-OptionsAdvisorDB_ArchiveExport}"
STAMP="$(date +%Y%m%d-%H%M%S)"
ARCH_DIR="backups/archive"
mkdir -p "$ARCH_DIR"

if ! docker compose ps sqlserver 2>/dev/null | grep -qE 'Up|healthy'; then
  echo "ERROR: sqlserver container is not running."
  exit 1
fi

SQLCMD=(docker compose exec -T sqlserver /opt/mssql-tools18/bin/sqlcmd
  -S localhost -U sa -P "${MSSQL_SA_PASSWORD}" -C -b)

echo "==> Ensuring export database ${EXPORT_DB}..."
"${SQLCMD[@]}" -Q "
IF NOT EXISTS (SELECT 1 FROM sys.databases WHERE name = N'${EXPORT_DB}')
  CREATE DATABASE [${EXPORT_DB}];
"

# Copy each *_Archive table from main → export (replace chunk for this week).
TABLES=$("${SQLCMD[@]}" -h -1 -W -Q "
SET NOCOUNT ON;
SELECT name FROM ${MAIN_DB}.sys.tables
WHERE name LIKE '%\_Archive' ESCAPE '\\' ORDER BY name;
")

BAK_NAME="${EXPORT_DB}-${STAMP}.bak"
CONTAINER_PATH="/var/opt/mssql/backup/${BAK_NAME}"
LOCAL_BAK="${ARCH_DIR}/${BAK_NAME}"

for T in $TABLES; do
  [[ -z "$T" ]] && continue
  COUNT=$("${SQLCMD[@]}" -h -1 -W -Q "SET NOCOUNT ON; SELECT COUNT(*) FROM [${MAIN_DB}].[dbo].[${T}]")
  COUNT="${COUNT// /}"
  if [[ "$COUNT" == "0" ]]; then
    echo "    skip ${T} (empty)"
    continue
  fi
  echo "    copy ${T} (${COUNT} rows)..."
  "${SQLCMD[@]}" -Q "
SET NOCOUNT ON;
IF OBJECT_ID(N'[${EXPORT_DB}].[dbo].[${T}]', N'U') IS NOT NULL
  DROP TABLE [${EXPORT_DB}].[dbo].[${T}];
SELECT * INTO [${EXPORT_DB}].[dbo].[${T}] FROM [${MAIN_DB}].[dbo].[${T}];
"
done

# Abort if export DB has no archive tables with data.
HAS=$("${SQLCMD[@]}" -h -1 -W -Q "
SET NOCOUNT ON;
SELECT COUNT(*) FROM ${EXPORT_DB}.sys.tables WHERE name LIKE '%\_Archive' ESCAPE '\\';
")
HAS="${HAS// /}"
if [[ "$HAS" == "0" ]]; then
  echo "==> No pending archive data — nothing to export."
  rm -f "${ARCH_DIR}/PENDING.json"
  exit 0
fi

echo "==> BACKUP ${EXPORT_DB}..."
"${SQLCMD[@]}" -Q "
BACKUP DATABASE [${EXPORT_DB}] TO DISK = N'${CONTAINER_PATH}' WITH INIT, COMPRESSION, STATS = 10;
"

docker cp "options_sqlserver:${CONTAINER_PATH}" "${LOCAL_BAK}"

python3 - <<PY
import json
from pathlib import Path
manifest = {
    "bak_file": "${LOCAL_BAK}",
    "bak_name": "${BAK_NAME}",
    "export_db": "${EXPORT_DB}",
    "main_db": "${MAIN_DB}",
    "stamp": "${STAMP}",
}
Path("${ARCH_DIR}/PENDING.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
print("Wrote ${ARCH_DIR}/PENDING.json")
PY

echo "==> Done: ${LOCAL_BAK} ($(du -h "${LOCAL_BAK}" | awk '{print $1}'))"
