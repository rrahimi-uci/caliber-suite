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
#                    knowledge extras (default: caliber/.venv/bin/python, else python3).
#   --keep           leave the two services running afterwards.
#
# It runs as its own compose project (caliber-kb-smoke), refuses to start while another CALIBER
# stack owns the fixed container names, and publishes on the stack's loopback ports (9000 MinIO,
# 5432 PostgreSQL), which must be free. Exit status is 0 only when the journey passed.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON=""
KEEP=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) PYTHON="${2:?--python needs an interpreter}"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "verify-knowledge-stack: unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$PYTHON" ]]; then
  if [[ -x "$REPO_ROOT/caliber/.venv/bin/python" ]]; then PYTHON="$REPO_ROOT/caliber/.venv/bin/python"; else PYTHON="python3"; fi
fi
command -v docker >/dev/null 2>&1 || { echo "verify-knowledge-stack: docker is required" >&2; exit 1; }

cd "$REPO_ROOT"
export COMPOSE_DISABLE_ENV_FILE=1
PROJECT="caliber-kb-smoke"
COMPOSE=(docker compose -p "$PROJECT" --env-file deploy/.env.example --env-file .env.example -f deploy/compose.yaml)

foreign="$(docker ps -a --filter "name=^caliber" \
  --format '{{.Names}} {{.Label "com.docker.compose.project"}}' | awk -v p="$PROJECT" '$2 != p {print $1}')"
if [[ -n "$foreign" ]]; then
  echo "verify-knowledge-stack: another CALIBER stack owns these containers: $(echo $foreign)" >&2
  echo "Stop it first, keeping its data:  docker compose -f deploy/compose.yaml --profile app --profile nats down" >&2
  exit 1
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
