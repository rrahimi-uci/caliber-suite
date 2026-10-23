#!/usr/bin/env bash
#
# verify-knowledge-stack.sh — run the knowledge-base journey against the docker stack's own
# MinIO and PostgreSQL (alpha gap P1-23).
#
# Every other knowledge-base test uses moto and SQLite. This starts the stack's real MinIO and
# PostgreSQL as their own compose project, then runs tests/test_knowledge_stack_journey.py:
# create, build, version, query, activate and roll back, with the sources read from MinIO and
# the chunks stored in PostgreSQL. The embeddings are SIMULATED (a deterministic test double),
# so it proves the storage, the build and the routes, not retrieval quality.
#
# Usage:
#   scripts/verify-knowledge-stack.sh [--python PYTHON] [--keep]
#
#   --python PYTHON  interpreter that has caliber-suite installed with the dev, s3, postgres and
#                    knowledge extras (default: caliber/.venv/bin/python, else .venv/bin/python
#                    from `make setup`, else python3).
#   --keep           leave the two services running afterwards.
#
# It runs as its own compose project (caliber-kb-smoke), refuses to start while another CALIBER
# stack owns the fixed container names, and publishes on the stack's loopback ports (9000 MinIO,
# 5432 PostgreSQL), which must be free (override with MINIO_API_PORT / POSTGRES_PORT).
# Needs: Docker with the Compose v2 plugin and a running daemon, curl, and a Python 3.10-3.12
# environment with the extras named above. Exit status: 0 only when the journey passed, 1 for a
# failure or a missing prerequisite, 2 for a usage error. The only thing it removes is its own
# compose project (caliber-kb-smoke) and that project's volumes, unless --keep is given.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON=""
KEEP=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) [[ $# -ge 2 ]] || { echo "verify-knowledge-stack: --python needs an interpreter" >&2; exit 2; }; PYTHON="$2"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    -h|--help) awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 0 ;;
    *) echo "verify-knowledge-stack: unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$PYTHON" ]]; then
  if [[ -x "$REPO_ROOT/caliber/.venv/bin/python" ]]; then PYTHON="$REPO_ROOT/caliber/.venv/bin/python"
  elif [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then PYTHON="$REPO_ROOT/.venv/bin/python"
  else PYTHON="python3"; fi
fi
prereq() { printf 'verify-knowledge-stack: %s\n' "$*" >&2; exit 1; }
command -v "$PYTHON" >/dev/null 2>&1 || prereq "interpreter not found: $PYTHON. Create the environment ('make setup', or 'make install' in caliber/) or pass --python /path/to/python"
# The journey runs from caliber/, so a relative interpreter path has to be pinned first.
if [[ "$PYTHON" == */* ]]; then PYTHON="$(cd "$(dirname "$PYTHON")" && pwd)/$(basename "$PYTHON")"; fi
"$PYTHON" -c 'import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] <= (3, 12) else 1)' \
  || prereq "$PYTHON is not Python 3.10-3.12 (CALIBER does not support other versions yet)"
"$PYTHON" -c 'import pytest, caliber, boto3, psycopg' >/dev/null 2>&1 \
  || prereq "$PYTHON lacks caliber-suite or its dev, s3 and postgres extras. Install them: 'make setup' in the repository root, or 'make install' in caliber/ and then pip install -e 'caliber[s3,knowledge]'"
command -v docker >/dev/null 2>&1 || prereq "docker is required. Install Docker Desktop (macOS, Windows) or Docker Engine with the Compose plugin (Linux): https://docs.docker.com/get-docker/"
docker compose version >/dev/null 2>&1 || prereq "'docker compose' (Compose v2) is required. Install the Compose plugin: https://docs.docker.com/compose/install/ (the legacy 'docker-compose' is not supported)"
docker info >/dev/null 2>&1 || prereq "the Docker daemon is not reachable. Start Docker Desktop (or 'sudo systemctl start docker') and re-run"
command -v curl >/dev/null 2>&1 || prereq "curl is required (for example 'brew install curl' or 'sudo apt-get install curl')"

cd "$REPO_ROOT"
export COMPOSE_DISABLE_ENV_FILE=1
PROJECT="caliber-kb-smoke"
COMPOSE=(docker compose -p "$PROJECT" --env-file deploy/.env.example --env-file .env.example -f deploy/compose.yaml)

foreign="$(docker ps -a --filter "name=^caliber" \
  --format '{{.Names}} {{.Label "com.docker.compose.project"}}' | awk -v p="$PROJECT" '$2 != p {print $1}')"
if [[ -n "$foreign" ]]; then
  echo "verify-knowledge-stack: another CALIBER stack owns these containers: $(printf '%s' "$foreign" | tr '\n' ' ')" >&2
  echo "Stop it first, keeping its data:  docker compose -f deploy/compose.yaml --profile app --profile nats down" >&2
  exit 1
fi

# The two services publish on fixed loopback ports; say so up front rather than failing inside `docker compose up`.
# (Skipped when this project's own containers exist: they hold the ports and `up` reuses them.)
port_in_use() {
  "$PYTHON" - "$1" <<'PY'
import socket
import sys

with socket.socket() as s:
    s.settimeout(0.5)
    sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
}
if [[ -z "$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT")" ]]; then
  for port in "${MINIO_API_PORT:-9000}" "${POSTGRES_PORT:-5432}"; do
    if port_in_use "$port"; then
      prereq "host port $port is already in use (a local PostgreSQL on 5432? another MinIO on 9000?). Stop it, or pick other ports with MINIO_API_PORT=... POSTGRES_PORT=..."
    fi
  done
fi

STATUS=1
cleanup() {
  if [[ "$STATUS" != 0 ]]; then
    echo "---- container status ----" >&2
    "${COMPOSE[@]}" ps -a >&2 || true
    "${COMPOSE[@]}" logs --no-color --tail 40 minio postgres >&2 || true
  fi
  if [[ "$KEEP" == 1 ]]; then
    echo "services left running (--keep); remove with: docker compose -p $PROJECT -f deploy/compose.yaml down -v"
  else
    "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  fi
}
trap 'STATUS=$?; cleanup; exit $STATUS' EXIT
trap 'exit 130' INT TERM

printf '\n==> 1. Start MinIO and PostgreSQL\n'
"${COMPOSE[@]}" up -d --build postgres minio createbuckets

printf '\n==> 2. Wait for both\n'
for _ in $(seq 1 90); do
  if [[ "$("${COMPOSE[@]}" ps --format '{{.Health}}' postgres 2>/dev/null)" == "healthy" ]] \
     && curl -sf "http://127.0.0.1:${MINIO_API_PORT:-9000}/minio/health/live" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
curl -sf "http://127.0.0.1:${MINIO_API_PORT:-9000}/minio/health/live" >/dev/null \
  || { echo "verify-knowledge-stack: FAIL: MinIO did not become healthy" >&2; exit 1; }
[[ "$("${COMPOSE[@]}" ps --format '{{.Health}}' postgres)" == "healthy" ]] \
  || { echo "verify-knowledge-stack: FAIL: PostgreSQL did not become healthy" >&2; exit 1; }
echo "ok: MinIO and PostgreSQL are up"

printf '\n==> 3. Run the knowledge-base journey (simulated embeddings)\n'
export CALIBER_KB_STACK_S3_ENDPOINT="http://127.0.0.1:${MINIO_API_PORT:-9000}"
export CALIBER_KB_STACK_POSTGRES_URL="postgresql+psycopg://caliber:caliber@127.0.0.1:${POSTGRES_PORT:-5432}/caliber"
(cd caliber && "$PYTHON" -m pytest -o addopts= -p no:cacheprovider -q --tb=short -rs tests/test_knowledge_stack_journey.py)

printf '\nverify-knowledge-stack: OK\n'
