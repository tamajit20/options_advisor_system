#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export COMPOSE_PROFILES=bundled
# shellcheck disable=SC1091
set -a
source .env.docker
set +a

docker compose exec -T options_advisor python - <<'PY'
from database.connection import SQLServerConnection
from lifecycle.archive_export import acknowledge_export

db = SQLServerConnection()
try:
    n = acknowledge_export(db)
    print(f"ACK_OK rows_cleared={n}")
finally:
    db.close()
PY
