#!/usr/bin/env bash
#
# verify-compose-stack.sh — build and boot the documented docker stack, then use it.
#
# `docker compose config --quiet` only proves the YAML resolves. The stack broke twice
# without a commit to this repository, once through an unpinned MLflow dependency and once
# through a MinIO image change, because nothing built the images or started them (alpha
# gaps P0-5, P1-17). This script builds BOTH application images (CALIBER and MLflow), starts
# the `app` and `nats` profiles with the checked-in example environment, and then checks:
#
#   1. MLflow answers a real, database-backed API call (not just /health)
#   2. CALIBER's /health reports the database ok and /readiness reports ready
#   3. the packaged SPA is served at /caliber/
#   4. admin/admin logs in, a project and a prompt can be created, and the prompt landed in
#      the MLflow registry
#
# Usage:
#   scripts/verify-compose-stack.sh [--no-build] [--keep] [--timeout SECONDS]
#
#   --no-build        reuse the images already on this machine instead of rebuilding.
#   --keep            leave the stack running afterwards (default: remove it and its volumes).
#   --timeout SECONDS how long to wait for CALIBER to become healthy (default: 900).
#
# It uses deploy/.env.example and .env.example, never a developer's own .env, and runs as its
# own compose project (caliber-stack-smoke) with its own volumes, so removing the stack at the
# end never touches a developer's data. The stack's containers have fixed names and it
# publishes on the default loopback ports (5000 MLflow, 5001 CALIBER), so the script refuses to
# start while another CALIBER stack exists; stop it first (without -v, to keep its data).
# Exit status is 0 only when every check passed. On failure the container logs are printed.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD=1
KEEP=0
TIMEOUT=900

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-build) BUILD=0; shift ;;
    --keep) KEEP=1; shift ;;
    --timeout) TIMEOUT="${2:?--timeout needs a number of seconds}"; shift 2 ;;
    -h|--help) sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "verify-compose-stack: unknown argument: $1" >&2; exit 2 ;;
  esac
done

step() { printf '\n==> %s\n' "$*"; }

cd "$REPO_ROOT"
command -v docker >/dev/null 2>&1 || { echo "verify-compose-stack: docker is required" >&2; exit 1; }

# Example environment only: the point is to prove what a fresh clone gets.
export COMPOSE_DISABLE_ENV_FILE=1
PROJECT="caliber-stack-smoke"
COMPOSE=(docker compose -p "$PROJECT" --env-file deploy/.env.example --env-file .env.example
  -f deploy/compose.yaml --profile app --profile nats)

MLFLOW_URL="http://127.0.0.1:${MLFLOW_PORT:-5000}"
CALIBER_URL="http://127.0.0.1:${CALIBER_PORT:-5001}"
API="$CALIBER_URL/ajax-api/2.0/mlflow/caliber"

# The containers have fixed names, so a developer's stack would collide with this one, and
# tearing ours down must never be mistaken for tearing theirs down.
foreign="$(docker ps -a --filter "name=^caliber" \
  --format '{{.Names}} {{.Label "com.docker.compose.project"}}' | awk -v p="$PROJECT" '$2 != p {print $1}')"
if [[ -n "$foreign" ]]; then
  echo "verify-compose-stack: another CALIBER stack owns these containers: $(echo $foreign)" >&2
  echo "Stop it first, keeping its data:  docker compose -f deploy/compose.yaml --profile app --profile nats down" >&2
  exit 1
fi

dump_logs() {
  echo "---- container status ----" >&2
  "${COMPOSE[@]}" ps -a >&2 || true
  for service in caliber mlflow; do
    echo "---- last logs: $service ----" >&2
    "${COMPOSE[@]}" logs --no-color --tail 80 "$service" >&2 || true
  done
}

STATUS=1
cleanup() {
  if [[ "$STATUS" != 0 ]]; then dump_logs; fi
  if [[ "$KEEP" == 1 ]]; then
    echo "stack left running (--keep); remove it and its volumes with: docker compose -p $PROJECT -f deploy/compose.yaml --profile app --profile nats down -v"
  else
    "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  fi
}
trap 'STATUS=$?; cleanup; exit $STATUS' EXIT

fail() { printf 'verify-compose-stack: FAIL: %s\n' "$*" >&2; exit 1; }

step "1. Build and start the stack ($([[ "$BUILD" == 1 ]] && echo "building the CALIBER and MLflow images" || echo "reusing existing images"))"
if [[ "$BUILD" == 1 ]]; then
  "${COMPOSE[@]}" up -d --build
else
  "${COMPOSE[@]}" up -d
fi

step "2. MLflow answers a real API call"
code=000
for _ in $(seq 1 60); do
  code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$MLFLOW_URL/api/2.0/mlflow/experiments/search" \
    -H 'Content-Type: application/json' -d '{}')" || code=000
  # 400 is a real, database-backed rejection of an empty body; 500 or 000 means the store is broken.
  [[ "$code" == 400 ]] && break
  sleep 2
done
[[ "$code" == 400 ]] || fail "MLflow never answered a database-backed API call (last status $code)"
echo "ok: MLflow API answered (400 for an empty search body, as a healthy store does)"

step "3. Wait for CALIBER to become healthy"
deadline=$((SECONDS + TIMEOUT))
until curl -sf "$API/health" >/dev/null 2>&1; do
  (( SECONDS < deadline )) || fail "CALIBER did not become healthy within ${TIMEOUT}s"
  sleep 3
done
echo "ok: CALIBER /health answered after about $((TIMEOUT - (deadline - SECONDS)))s"

step "4. Readiness, the SPA, login, project and prompt"
python3 - "$API" "$CALIBER_URL" "$MLFLOW_URL" <<'PY'
import http.cookiejar
import json
import re
import sys
import urllib.error
import urllib.request

api, caliber, mlflow = sys.argv[1:4]
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def call(method, url, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url, data=data, method=method, headers={"content-type": "application/json"}
    )
    try:
        with opener.open(request, timeout=60) as response:
            raw = response.read()
            return response.status, raw
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def check(condition, message):
    if not condition:
        raise SystemExit(f"verify-compose-stack: FAIL: {message}")
    print(f"ok: {message}")


def parsed(raw):
    return json.loads(raw or b"null")


status, raw = call("GET", f"{api}/health")
check(status == 200 and parsed(raw).get("db") == "ok", "/health is 200 with db ok")

status, raw = call("GET", f"{api}/readiness")
readiness = parsed(raw)["data"]
blockers = readiness.get("blockers", [])
check(status == 200 and readiness["ready"] is True, f"/readiness reports ready (blockers: {blockers})")
check(readiness["providers"]["promoter"] == "mlflow", "the release promoter is the real MLflow one")

status, raw = call("GET", f"{caliber}/caliber/")
shell = raw.decode("utf-8", "replace")
check(status == 200 and '<div id="root"' in shell, "/caliber/ serves the SPA shell")
asset = re.search(r"/caliber/assets/[^\"]+\.js", shell)
check(asset is not None, "the SPA shell references a JavaScript bundle")
status, _ = call("GET", f"{caliber}{asset.group(0)}")
check(status == 200, f"{asset.group(0)} returns 200")

status, _ = call("POST", f"{api}/auth/login", {"user_id": "admin", "password": "admin"})
check(status == 200, "admin/admin logs in")
status, raw = call("GET", f"{api}/auth/session")
check(status == 200 and parsed(raw)["data"]["is_admin"] is True, "the session is an admin session")

status, _ = call("POST", f"{api}/projects", {"name": "compose-stack-check"})
check(status in (200, 201), f"POST /projects returned {status}")
status, _ = call(
    "POST",
    f"{api}/prompts",
    {"name": "compose-stack-check-prompt", "template": "Answer briefly: {{question}}"},
)
check(status in (200, 201), f"POST /prompts returned {status}")

status, raw = call(
    "GET",
    f"{mlflow}/ajax-api/2.0/mlflow/registered-models/get?name=compose-stack-check-prompt",
)
check(
    status == 200 and parsed(raw)["registered_model"]["name"] == "compose-stack-check-prompt",
    "the prompt is in the MLflow registry",
)
PY

if "${COMPOSE[@]}" logs --no-color caliber 2>&1 | grep -q "no such table\|relation .* does not exist"; then
  fail "CALIBER logged a missing-table error (the migration did not cover the schema)"
fi

printf '\nverify-compose-stack: OK\n'
