#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export COMPOSE_PROFILES=bundled
set -a
# shellcheck disable=SC1091
source .env.docker
set +a
docker compose build options_advisor
docker compose up -d
docker compose ps
