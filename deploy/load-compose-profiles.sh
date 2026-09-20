#!/usr/bin/env bash
# Source from deploy scripts after cd to repo root.
# Respects COMPOSE_PROFILES from .env.docker (e.g. bundled,https).
if [[ -f .env.docker ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env.docker
  set +a
fi
export COMPOSE_PROFILES="${COMPOSE_PROFILES:-bundled}"
