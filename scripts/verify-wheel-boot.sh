#!/usr/bin/env bash
#
# verify-wheel-boot.sh — prove that a built caliber-suite wheel installs and boots.
#
# `python -m build` succeeding says nothing about whether the wheel works: a wheel that
# lost its bundled SPA or its Alembic migrations still builds, installs and passes
# `twine check`, then serves a placeholder page or fails every login (alpha gaps P0-5,
# P1-17). This script checks the artifact the way a user receives it:
#
#   1. the wheel's file list holds the SPA and the migrations   (fails fast, no install)
#   2. install it alone into a fresh venv, away from the checkout
#   3. caliber-migrate brings an empty SQLite database to head
#   4. uvicorn boots the app; /health is 200 with the database ok
#   5. /caliber/ serves the real SPA shell and one of its hashed assets
#
# Usage:
#   scripts/verify-wheel-boot.sh [--python PYTHON] WHEEL
#
#   --python PYTHON  interpreter for the venv (default: $PYTHON, else python3). Must be 3.10-3.12.
#
# Needs: a Python 3.10-3.12 interpreter, curl, and network access to PyPI (pip resolves the wheel's
# dependencies). Exit status: 0 only when every check passed, 1 for a failed check or missing
# prerequisite, 2 for a usage error. Nothing outside a scratch directory is
# modified and the server is stopped on exit. The full stack, with MLflow, is covered by
# scripts/verify-alpha-install.sh; this script exists to be fast and to need no MLflow.

set -euo pipefail

PYTHON="${PYTHON:-python3}"
WHEEL=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) PYTHON="${2:?--python needs an interpreter}"; shift 2 ;;
    -h|--help) awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 0 ;;
    -*) echo "verify-wheel-boot: unknown argument: $1" >&2; exit 2 ;;
    *) [[ -z "$WHEEL" ]] || { echo "verify-wheel-boot: one wheel only" >&2; exit 2; }; WHEEL="$1"; shift ;;
  esac
done

fail() { printf 'verify-wheel-boot: FAIL: %s\n' "$*" >&2; exit 1; }
step() { printf '\n==> %s\n' "$*"; }

[[ -n "$WHEEL" ]] || { echo "usage: verify-wheel-boot.sh [--python PYTHON] WHEEL" >&2; exit 2; }
[[ -f "$WHEEL" ]] || fail "no such wheel: $WHEEL"
command -v "$PYTHON" >/dev/null 2>&1 || fail "interpreter not found: $PYTHON (install Python 3.10-3.12, or pass --python /path/to/python3.12)"
# The script changes directory, so a relative interpreter path has to be pinned first.
if [[ "$PYTHON" == */* ]]; then PYTHON="$(cd "$(dirname "$PYTHON")" && pwd)/$(basename "$PYTHON")"; fi
WHEEL="$(cd "$(dirname "$WHEEL")" && pwd)/$(basename "$WHEEL")"

step "1. The wheel contains the SPA and the migrations"
"$PYTHON" - "$WHEEL" <<'PY' || fail "the wheel is missing required content (see above)"
import sys
import zipfile

with zipfile.ZipFile(sys.argv[1]) as archive:
    names = archive.namelist()

problems = []
if "caliber/ui/index.html" not in names:
    problems.append("bundled SPA: caliber/ui/index.html is missing")
if not any(n.startswith("caliber/ui/assets/") for n in names):
    problems.append("bundled SPA: caliber/ui/assets/ is empty")
if "caliber/db/migrations/env.py" not in names:
    problems.append("migrations: caliber/db/migrations/env.py is missing")
if not any(n.startswith("caliber/db/migrations/versions/") and n.endswith(".py") for n in names):
    problems.append("migrations: caliber/db/migrations/versions/ has no revisions")
for problem in problems:
    print(f"  missing {problem}", file=sys.stderr)
raise SystemExit(1 if problems else 0)
PY
echo "ok: SPA and migrations are in the wheel"

"$PYTHON" - <<'PY' || fail "Python 3.10, 3.11 or 3.12 is required (CALIBER does not support newer interpreters yet)"
import sys
raise SystemExit(0 if (3, 10) <= sys.version_info[:2] <= (3, 12) else 1)
PY

command -v curl >/dev/null 2>&1 || fail "curl is required (for example 'brew install curl' or 'sudo apt-get install curl')"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/caliber-wheel-boot.XXXXXX")"
SERVER_PID=""
kill_tree() { # a process and its children
  local child
  for child in $(pgrep -P "$1" 2>/dev/null || true); do kill_tree "$child"; done
  kill "$1" 2>/dev/null || true
}
cleanup() {
  trap - EXIT
  if [[ -n "$SERVER_PID" ]]; then
    kill_tree "$SERVER_PID"
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

step "2. Install the wheel into a fresh venv"
cd "$WORK"
"$PYTHON" -m venv venv
# shellcheck disable=SC1091
source venv/bin/activate
pip install -q "$WHEEL" || fail "pip could not install the wheel and its dependencies (is PyPI reachable? is the wheel built for this Python?)"
command -v caliber-migrate >/dev/null || fail "caliber-migrate was not installed by the wheel"

step "3. Migrate an empty database"
export CALIBER_DATABASE_URL="sqlite:///$WORK/caliber.db"
caliber-migrate >"$WORK/migrate.log" 2>&1 || { cat "$WORK/migrate.log" >&2; fail "caliber-migrate failed"; }
echo "ok: caliber-migrate reached head"

step "4. Boot the server"
PORT="$(python - <<'PY'
import socket
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    print(s.getsockname()[1])
PY
)"
export CALIBER_LOG_SINK=stderr
export CALIBER_AUTH_SESSION_COOKIE_SECURE=false
export CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT=true
uvicorn caliber.server:create_app --factory --host 127.0.0.1 --port "$PORT" >"$WORK/server.log" 2>&1 &
SERVER_PID=$!
BASE="http://127.0.0.1:$PORT"
for ((i = 0; i < 120; i++)); do
  if curl -sf "$BASE/ajax-api/2.0/mlflow/caliber/health" >/dev/null 2>&1; then break; fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then cat "$WORK/server.log" >&2; fail "the server exited during startup"; fi
  sleep 1
done
health="$(curl -sf "$BASE/ajax-api/2.0/mlflow/caliber/health")" || { cat "$WORK/server.log" >&2; fail "/health never answered"; }
python - "$health" <<'PY' || fail "/health did not report the database ok"
import json
import sys

body = json.loads(sys.argv[1])
raise SystemExit(0 if body.get("db") == "ok" else 1)
PY
echo "ok: /health is 200 with db ok"

step "5. The packaged SPA is served"
shell="$(curl -sf "$BASE/caliber/")" || fail "GET /caliber/ did not return 200"
grep -q '<div id="root"' <<<"$shell" || fail "/caliber/ is not the SPA shell (no root element); it served the placeholder page"
asset="$(grep -oE '/caliber/assets/[^"]+\.js' <<<"$shell" | head -1 || true)"
[[ -n "$asset" ]] || fail "the SPA shell references no /caliber/assets/*.js bundle"
code="$(curl -s -o /dev/null -w '%{http_code}' "$BASE$asset")"
[[ "$code" == "200" ]] || fail "$asset returned $code"
echo "ok: /caliber/ serves the SPA and $asset returns 200"

grep -q "no such table" "$WORK/server.log" && fail "the server logged 'no such table'"

printf '\nverify-wheel-boot: OK\n'
