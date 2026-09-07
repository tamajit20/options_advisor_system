#!/usr/bin/env bash
# Host/VM helper: run the same archive export as the scheduler job.
# The scheduler calls lifecycle/archive_export.py directly (no docker CLI).
#
# Usage: ./deploy/archive-export.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export COMPOSE_PROFILES=bundled

mkdir -p backups/archive
chmod 777 backups backups/archive 2>/dev/null || true

if ! docker compose ps options_advisor 2>/dev/null | grep -qE 'Up|healthy'; then
  echo "ERROR: options_advisor container is not running."
  exit 1
fi

docker compose exec -T options_advisor python - <<'PY'
from database.connection import SQLServerConnection
from lifecycle.archive_export import run_archive_export

db = SQLServerConnection()
db.connect()
try:
    n = run_archive_export(db)
    db.commit()
    print(f"archive_export ok flag={n}")
finally:
    db.close()
PY
