#!/usr/bin/env bash
# Database setup helpers for deploy/setup.sh and vm-install-deploy.sh.
#
# Modes:
#   fresh    — DROP DATABASE + --init-db (empty database)
#   existing — --init-db only (keeps data; creates any missing tables/migrations)
#
# Note: routine upgrades can use ./deploy/update.sh instead — schema still
# applies automatically when options_advisor / ws_runner containers restart.
#
# shellcheck disable=SC1091
set -euo pipefail

_db_name() {
  echo "${OPT_DB_NAME:-OptionsAdvisorDB}"
}

_sqlcmd() {
  docker compose exec -T sqlserver /opt/mssql-tools18/bin/sqlcmd \
    -S localhost -U sa -P "${MSSQL_SA_PASSWORD}" -C -b "$@"
}

db_exists() {
  local name
  name="$(_db_name)"
  local result
  result="$(_sqlcmd -Q "SET NOCOUNT ON; SELECT CASE WHEN DB_ID(N'${name}') IS NULL THEN 0 ELSE 1 END" -h -1 | tr -d '[:space:]')"
  [[ "${result:-0}" == "1" ]]
}

db_has_trade_data() {
  local name
  name="$(_db_name)"
  if ! db_exists; then
    return 1
  fi
  local result
  result="$(_sqlcmd -d "${name}" -Q "
    SET NOCOUNT ON;
    IF OBJECT_ID('options_trades', 'U') IS NULL SELECT 0
    ELSE SELECT CASE WHEN EXISTS (SELECT 1 FROM options_trades) THEN 1 ELSE 0 END
  " -h -1 | tr -d '[:space:]')"
  [[ "${result:-0}" == "1" ]]
}

drop_database() {
  local name
  name="$(_db_name)"
  echo "==> Dropping database [${name}]..."
  docker compose stop options_advisor ws_runner 2>/dev/null || true
  _sqlcmd -Q "
    IF DB_ID(N'${name}') IS NOT NULL
    BEGIN
      ALTER DATABASE [${name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
      DROP DATABASE [${name}];
    END"
  echo "    Database dropped."
}

init_database() {
  echo "==> Ensuring database schema (--init-db)..."
  docker compose run --rm options_advisor python main.py --init-db
}

# Resolve mode into DB_SETUP_MODE variable: fresh | existing
# Honors: --fresh-db | --use-existing-db | DB_SETUP_MODE env | interactive prompt
resolve_db_setup_mode() {
  local flag_fresh=false flag_existing=false
  for arg in "$@"; do
    case "$arg" in
      --fresh-db) flag_fresh=true ;;
      --use-existing-db) flag_existing=true ;;
    esac
  done

  if $flag_fresh && $flag_existing; then
    echo "ERROR: Use only one of --fresh-db or --use-existing-db." >&2
    exit 1
  fi
  if $flag_fresh; then
    DB_SETUP_MODE=fresh
    return
  fi
  if $flag_existing; then
    DB_SETUP_MODE=existing
    return
  fi
  if [[ -n "${DB_SETUP_MODE:-}" ]]; then
    case "${DB_SETUP_MODE}" in
      fresh|existing) return ;;
      *) echo "ERROR: DB_SETUP_MODE must be 'fresh' or 'existing'." >&2; exit 1 ;;
    esac
  fi

  if ! db_exists; then
    DB_SETUP_MODE=fresh
    return
  fi

  # Database already on disk — ask operator unless non-interactive.
  echo ""
  echo "================================================================="
  echo " Existing SQL Server database detected: $(_db_name)"
  if db_has_trade_data; then
    echo " (contains trade data in options_trades)"
  else
    echo " (empty or no trade rows yet)"
  fi
  echo ""
  echo "  [1] Delete database and create FRESH empty database"
  echo "  [2] Keep EXISTING database and data (schema upgrade only)"
  echo "================================================================="
  if [[ ! -t 0 ]]; then
    echo ""
    echo "Non-interactive session — keeping existing database (safe default)."
    echo "To wipe and recreate:  DB_SETUP_MODE=fresh ./deploy/setup.sh"
    echo "To force explicitly:   ./deploy/setup.sh --use-existing-db"
    DB_SETUP_MODE=existing
    return
  fi
  while true; do
    read -r -p "Choose [1/2] (default 2): " choice
    choice="${choice:-2}"
    case "$choice" in
      1) DB_SETUP_MODE=fresh; return ;;
      2) DB_SETUP_MODE=existing; return ;;
      *) echo "Enter 1 or 2." ;;
    esac
  done
}

run_db_setup() {
  resolve_db_setup_mode "$@"
  case "${DB_SETUP_MODE}" in
    fresh)
      if db_exists; then
        drop_database
      fi
      init_database
      ;;
    existing)
      init_database
      ;;
    *)
      echo "ERROR: Unknown DB_SETUP_MODE=${DB_SETUP_MODE}" >&2
      exit 1
      ;;
  esac
}
