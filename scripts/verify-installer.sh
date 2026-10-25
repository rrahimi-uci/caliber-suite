#!/usr/bin/env bash
#
# verify-installer.sh — run scripts/install.sh the way a user does and check the result.
#
# Builds the three wheels from this checkout (with the bundled UI unless --no-ui), installs
# them with scripts/install.sh into a scratch prefix, and asserts: the services start, the
# generated admin password logs in while admin/admin is refused, a project can be created,
# the UI shell is served, re-running the installer keeps the password and data, and
# `caliber down` leaves no process behind.
#
# Usage:
#   scripts/verify-installer.sh [--python PYTHON] [--release VERSION] [--no-ui] [--keep]
#
#   --python PYTHON    interpreter for the install (default: $PYTHON, else python3); 3.10-3.12
#   --release VERSION  install that published release from GitHub instead of local wheels
#   --no-ui            skip building the web UI (faster; the UI check is then skipped)
#   --keep             keep the scratch directory and print its path
#
# Needs: bash, curl, Python 3.10-3.12, network access to PyPI (and to GitHub with --release),
# and npm unless --no-ui. Exit status: 0 only when every assertion passed.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
RELEASE=""
UI=1
KEEP=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) PYTHON="${2:?--python needs an interpreter}"; shift 2 ;;
    --release) RELEASE="${2:?--release needs a version}"; shift 2 ;;
    --no-ui) UI=0; shift ;;
    --keep) KEEP=1; shift ;;
    -h|--help) awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 0 ;;
    *) echo "verify-installer: unknown argument: $1" >&2; exit 2 ;;
  esac
done

fail() { printf 'verify-installer: FAIL: %s\n' "$*" >&2; exit 1; }
ok() { printf 'ok: %s\n' "$*"; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/caliber-installer.XXXXXX")"
PREFIX="$WORK/home"
CALIBER="$PREFIX/bin/caliber"
cleanup() {
  [[ -x "$CALIBER" ]] && "$CALIBER" down >/dev/null 2>&1 || true
  if [[ "$KEEP" == 1 ]]; then echo "scratch directory kept: $WORK"; else rm -rf "$WORK"; fi
}
trap cleanup EXIT

args=(--prefix "$PREFIX" --bin-dir "$WORK/bin" --python "$PYTHON")
version="$(sed -n 's/^version = "\(.*\)"/\1/p' "$REPO_ROOT/caliber/pyproject.toml" | head -1)"
if [[ -n "$RELEASE" ]]; then
  args+=(--version "$RELEASE")
else
  printf '\n==> Building the wheels from this checkout (%s)\n' "$version"
  if [[ "$UI" == 1 ]]; then
    npm --prefix "$REPO_ROOT/caliber/caliber-ui" ci --silent
    npm --prefix "$REPO_ROOT/caliber/caliber-ui" run build --silent
    rm -rf "$REPO_ROOT/caliber/src/caliber/ui" && mkdir -p "$REPO_ROOT/caliber/src/caliber/ui"
    cp -R "$REPO_ROOT/caliber/caliber-ui/dist/." "$REPO_ROOT/caliber/src/caliber/ui/"
  fi
  "$PYTHON" -m venv "$WORK/build-venv"
  "$WORK/build-venv/bin/pip" install -q build || fail "pip could not install 'build'"
  for pkg in sdk/caliber-sdk sdk/caliber-cli caliber; do
    "$WORK/build-venv/bin/python" -m build --wheel --outdir "$WORK/wheels" "$REPO_ROOT/$pkg" >/dev/null
  done
  args+=(--version "$version" --wheel-dir "$WORK/wheels")
fi

printf '\n==> scripts/install.sh %s\n' "${args[*]}"
"$REPO_ROOT/scripts/install.sh" "${args[@]}" || fail "the installer failed"

printf '\n==> Checking the running install\n'
"$CALIBER" status >/dev/null || fail "caliber status says the services are not running"
ok "caliber status: running"
port="$(sed -n 's/^CALIBER_PORT=//p' "$PREFIX/caliber.env")"
password="$("$CALIBER" password)"
[[ ${#password} -ge 20 ]] || fail "the generated password is shorter than 20 characters"
[[ "$(stat -c %a "$PREFIX/admin-password" 2>/dev/null || stat -f %Lp "$PREFIX/admin-password")" == 600 ]] \
  || fail "admin-password is not mode 600"
ok "generated password is ${#password} characters, mode 600"

"$PYTHON" - "http://127.0.0.1:$port" "$password" "$UI" <<'PY'
import http.cookiejar, json, sys, urllib.error, urllib.request

base, password, ui = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
api = f"{base}/ajax-api/2.0/mlflow/caliber"
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

def call(method, url, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"content-type": "application/json"})
    try:
        with opener.open(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

def check(cond, msg):
    if not cond:
        raise SystemExit(f"verify-installer: FAIL: {msg}")
    print(f"ok: {msg}")

status, body = call("GET", f"{api}/readiness")
check(status == 200 and json.loads(body)["data"]["ready"] is True, "/readiness reports ready")
status, _ = call("POST", f"{api}/auth/login", {"user_id": "admin", "password": "admin"})
check(status == 401, "admin/admin is refused")
status, _ = call("POST", f"{api}/auth/login", {"user_id": "admin", "password": password})
check(status == 200, "the generated password logs in")
status, body = call("GET", f"{api}/auth/session")
check(status == 200 and json.loads(body)["data"]["is_admin"] is True, "the session is an admin session")
status, _ = call("POST", f"{api}/projects", {"name": "installer-check"})
check(status in (200, 201), f"POST /projects returned {status}")
if ui:
    status, body = call("GET", f"{base}/caliber/")
    check(status == 200 and b'<div id="root"' in body, "/caliber/ serves the UI shell")
PY

printf '\n==> Re-running the installer (upgrade in place)\n'
"$CALIBER" down
# The re-install links into a directory that already holds someone else's `caliber`:
# the installer must leave that file alone.
mkdir -p "$WORK/other-bin" && printf '#!/bin/sh\necho not-ours\n' >"$WORK/other-bin/caliber"
chmod +x "$WORK/other-bin/caliber"
"$REPO_ROOT/scripts/install.sh" "${args[@]}" --bin-dir "$WORK/other-bin" --no-start >"$WORK/reinstall.log" 2>&1 \
  || { cat "$WORK/reinstall.log" >&2; fail "the second install failed"; }
[[ "$("$WORK/other-bin/caliber")" == "not-ours" ]] || fail "the installer replaced an unrelated 'caliber' program"
grep -q "left it alone" "$WORK/reinstall.log" || fail "the installer did not say it left the existing 'caliber' alone"
ok "an unrelated 'caliber' on the PATH is left alone"
[[ "$("$CALIBER" password)" == "$password" ]] || fail "re-installing changed the admin password"
ok "re-install kept the password"
"$CALIBER" up >/dev/null || fail "caliber up failed after the re-install"
"$PYTHON" - "http://127.0.0.1:$port" "$password" <<'PY'
import http.cookiejar, json, sys, urllib.request

base, password = sys.argv[1], sys.argv[2]
api = f"{base}/ajax-api/2.0/mlflow/caliber"
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
login = urllib.request.Request(f"{api}/auth/login", data=json.dumps({"user_id": "admin", "password": password}).encode(),
                               method="POST", headers={"content-type": "application/json"})
opener.open(login, timeout=60)
with opener.open(f"{api}/projects", timeout=60) as r:
    text = r.read().decode()
if "installer-check" not in text:
    raise SystemExit("verify-installer: FAIL: the project created before the re-install is gone")
print("ok: data survived the re-install")
PY

printf '\n==> Stopping\n'
"$CALIBER" down
if "$CALIBER" status >/dev/null 2>&1; then fail "caliber status still reports running after down"; fi
if pgrep -f "$PREFIX/venv" >/dev/null 2>&1; then fail "processes from the install survived 'caliber down'"; fi
ok "caliber down stopped everything"

printf '\nverify-installer: OK\n'
