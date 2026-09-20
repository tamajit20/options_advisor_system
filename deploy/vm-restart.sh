#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source "$(dirname "$0")/load-compose-profiles.sh"
docker compose build options_advisor
docker compose up -d
docker compose ps
