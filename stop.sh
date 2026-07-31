#!/usr/bin/env bash
# Stop the containerized CALIBER suite (keeps data volumes).
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker not found — nothing to stop." >&2
  exit 0
fi

echo "stopping caliber-suite (containers)..."
ENV_ARGS=()
[[ -f deploy/.env ]] && ENV_ARGS+=(--env-file deploy/.env)
[[ -f .env ]] && ENV_ARGS+=(--env-file .env)
COMPOSE=(docker compose "${ENV_ARGS[@]}" -f deploy/compose.yaml --profile nats --profile app)

"${COMPOSE[@]}" down
echo "done."
