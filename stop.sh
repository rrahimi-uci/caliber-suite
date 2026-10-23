#!/usr/bin/env bash
# Stop the containerized CALIBER suite started by ./start.sh. The data volumes
# (Postgres, MinIO, NATS, ...) are KEPT; `make infra-reset` is the command that deletes them.
#
# Usage:  ./stop.sh [-h|--help]        (or: make stop)
set -euo pipefail

cd "$(dirname "$0")"

for arg in "$@"; do
  case "$arg" in
    -h|--help)
      awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
      exit 0
      ;;
    *)
      echo "stop.sh: unknown argument: $arg (see ./stop.sh --help)" >&2
      exit 2
      ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  echo "docker not found — nothing to stop." >&2
  exit 0
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "stop.sh: 'docker compose' (Compose v2) was not found. Install the Compose plugin: https://docs.docker.com/compose/install/" >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "the Docker daemon is not reachable — nothing is running, so there is nothing to stop." >&2
  exit 0
fi

echo "stopping caliber-suite (containers)..."
# Same env files as ./start.sh, so Compose resolves the same project and ports.
# (An empty array expanded with "${arr[@]}" is an unbound-variable error under `set -u`
# on macOS's bash 3.2, hence the ${arr[@]+...} form.)
ENV_ARGS=()
[[ -f deploy/.env ]] && ENV_ARGS+=(--env-file deploy/.env)
[[ -f .env ]] && ENV_ARGS+=(--env-file .env)
COMPOSE=(docker compose ${ENV_ARGS[@]+"${ENV_ARGS[@]}"} -f deploy/compose.yaml --profile nats --profile app)

"${COMPOSE[@]}" down
echo "done."
