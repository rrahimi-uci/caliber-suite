#!/usr/bin/env bash
#
# verify-alpha-install.sh — execute docs/start/alpha-install.md in a scratch directory.
#
# The page is only worth publishing if a second person can follow it verbatim, so this
# script performs the same steps against freshly built wheels, with no repository
# checkout on the import path, and asserts the outcome the page promises:
#
#   install the wheels -> caliber-migrate -> native MLflow server -> uvicorn
#   -> /readiness ready -> admin/admin logs in as an admin -> a project and a prompt
#   can be created -> the prompt really landed in the MLflow registry.
#
# Usage:
#   scripts/verify-alpha-install.sh [--wheel-dir DIR] [--python PYTHON] [--keep]
#
#   --wheel-dir DIR  directory holding the caliber-sdk, caliber-cli and caliber-suite
#                    wheels (for example the release assets). Without it the wheels are
#                    built from this checkout.
#   --python PYTHON  interpreter for the venvs (default: python3). Must be 3.10-3.12.
#   --keep           keep the scratch directory and print its path (for debugging).
#
# Exit status is 0 only when every assertion passed. Nothing outside the scratch
# directory is modified, and every process the script starts is stopped on exit.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
WHEEL_DIR=""
KEEP=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --wheel-dir) WHEEL_DIR="${2:?--wheel-dir needs a directory}"; shift 2 ;;
    --python) PYTHON="${2:?--python needs an interpreter}"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    -h|--help) sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "verify-alpha-install: unknown argument: $1" >&2; exit 2 ;;
  esac
done

step() { printf '\n==> %s\n' "$*"; }
fail() { printf 'verify-alpha-install: FAIL: %s\n' "$*" >&2; exit 1; }

command -v "$PYTHON" >/dev/null 2>&1 || fail "interpreter not found: $PYTHON"
"$PYTHON" - <<'PY' || fail "Python 3.10, 3.11 or 3.12 is required (CALIBER does not support newer interpreters yet)"
import sys
raise SystemExit(0 if (3, 10) <= sys.version_info[:2] <= (3, 12) else 1)
PY

WORK="$(mktemp -d "${TMPDIR:-/tmp}/caliber-alpha-install.XXXXXX")"
PIDS=()
cleanup() {
  local pid
  for pid in "${PIDS[@]:-}"; do
    if [[ -n "$pid" ]]; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
  done
  if [[ "$KEEP" == 1 ]]; then
    echo "scratch directory kept: $WORK"
  else
    rm -rf "$WORK"
  fi
}
trap cleanup EXIT

free_port() {
  "$PYTHON" - <<'PY'
import socket
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    print(s.getsockname()[1])
PY
}

wait_for() { # wait_for URL SECONDS LABEL
  local url="$1" seconds="$2" label="$3" i
  for ((i = 0; i < seconds; i++)); do
    if curl -sf "$url" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  fail "$label did not become healthy at $url within ${seconds}s"
}

# ---------------------------------------------------------------- wheels
if [[ -z "$WHEEL_DIR" ]]; then
  step "Building the wheels from this checkout"
  WHEEL_DIR="$WORK/wheels"
  mkdir -p "$WHEEL_DIR"
  "$PYTHON" -m venv "$WORK/build-venv"
  "$WORK/build-venv/bin/pip" install -q build
  for pkg in sdk/caliber-sdk sdk/caliber-cli caliber; do
    "$WORK/build-venv/bin/python" -m build --wheel --outdir "$WHEEL_DIR" "$REPO_ROOT/$pkg" >/dev/null
  done
fi
for name in caliber_sdk caliber_cli caliber_suite; do
  compgen -G "$WHEEL_DIR/${name}-*.whl" >/dev/null || fail "no $name wheel in $WHEEL_DIR"
done

# ------------------------------------------- the page's steps, in its order
step "1. Create a virtual environment and install the wheels (sdk, cli, server)"
cd "$WORK"
"$PYTHON" -m venv alpha
# shellcheck disable=SC1091
source alpha/bin/activate
pip install -q --find-links "$WHEEL_DIR" caliber-sdk caliber-cli caliber-suite

# The checkout must not be on the import path: this proves the wheel is self-contained.
python - <<PY
import caliber, pathlib
root = pathlib.Path(r"$REPO_ROOT").resolve()
location = pathlib.Path(caliber.__file__).resolve()
assert root not in location.parents, f"caliber imported from the checkout: {location}"
print("caliber imported from", location.parent)
PY

step "2. Migrate the CALIBER database"
export CALIBER_DATABASE_URL="sqlite:///$WORK/caliber.db"
caliber-migrate

step "3. Start MLflow on its own SQLite file"
MLFLOW_PORT="$(free_port)"
mlflow server \
  --backend-store-uri "sqlite:///$WORK/mlflow.db" \
  --default-artifact-root "$WORK/mlflow-artifacts" \
  --host 127.0.0.1 --port "$MLFLOW_PORT" >"$WORK/mlflow.log" 2>&1 &
PIDS+=("$!")
wait_for "http://127.0.0.1:$MLFLOW_PORT/health" 120 "MLflow"

step "4. Configure and start CALIBER"
CALIBER_PORT="$(free_port)"
export MLFLOW_TRACKING_URI="http://127.0.0.1:$MLFLOW_PORT"
export CALIBER_ADMIN_USERS=admin
export CALIBER_OPERATOR_USERS=admin
export CALIBER_APPROVER_USERS=admin
export CALIBER_PROMOTER_PROVIDER=mlflow
export CALIBER_ARTIFACT_STORE_PROVIDER=mlflow
export CALIBER_EVAL_PROVIDER=fake
export CALIBER_LOG_SINK=stderr
export CALIBER_AUTH_SESSION_COOKIE_SECURE=false
export CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT=true
uvicorn caliber.server:create_app --factory --host 127.0.0.1 --port "$CALIBER_PORT" \
  >"$WORK/caliber.log" 2>&1 &
PIDS+=("$!")
API="http://127.0.0.1:$CALIBER_PORT/ajax-api/2.0/mlflow/caliber"
wait_for "$API/health" 180 "CALIBER"

step "5. Verify readiness, login, project and prompt creation"
python - "$API" "$MLFLOW_PORT" <<'PY'
import http.cookiejar
import json
import sys
import urllib.error
import urllib.request

api, mlflow_port = sys.argv[1], sys.argv[2]
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def call(method, url, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url, data=data, method=method, headers={"content-type": "application/json"}
    )
    try:
        with opener.open(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"null")


def check(condition, message):
    if not condition:
        raise SystemExit(f"verify-alpha-install: FAIL: {message}")
    print(f"ok: {message}")


status, health = call("GET", f"{api}/health")
check(status == 200 and health.get("db") == "ok", "/health is 200 with db ok")

status, readiness = call("GET", f"{api}/readiness")
data = readiness["data"]
check(status == 200 and data["ready"] is True, "/readiness reports ready")
simulated = set(data["simulated"])
check(
    simulated <= {"llm", "eval"},
    f"only the LLM and evaluation providers are simulated (got {sorted(simulated)})",
)
check(data["providers"]["promoter"] == "mlflow", "the release promoter is the real MLflow one")

status, login = call("POST", f"{api}/auth/login", {"user_id": "admin", "password": "admin"})
check(status == 200, "admin/admin logs in")

status, session = call("GET", f"{api}/auth/session")
check(status == 200 and session["data"]["is_admin"] is True, "the session is an admin session")

status, project = call("POST", f"{api}/projects", {"name": "alpha-install-check"})
check(status in (200, 201), f"POST /projects returned {status}")

status, prompt = call(
    "POST",
    f"{api}/prompts",
    {"name": "alpha-install-check-prompt", "template": "Answer briefly: {{question}}"},
)
check(status in (200, 201), f"POST /prompts returned {status}")

registry = (
    f"http://127.0.0.1:{mlflow_port}/ajax-api/2.0/mlflow/registered-models/get"
    "?name=alpha-install-check-prompt"
)
with urllib.request.urlopen(registry, timeout=30) as response:
    registered = json.loads(response.read())
check(
    registered["registered_model"]["name"] == "alpha-install-check-prompt",
    "the prompt is in the native MLflow registry",
)
PY

step "6. Check the logs for the failure the page exists to prevent"
if grep -q "no such table" "$WORK/caliber.log"; then
  fail "the server logged 'no such table' (migration did not cover the schema)"
fi

step "7. The CLI entry point runs"
caliberctl --help >/dev/null

printf '\nverify-alpha-install: OK\n'
