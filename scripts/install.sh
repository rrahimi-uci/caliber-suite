#!/usr/bin/env bash
#
# install.sh — install CALIBER from a GitHub release and start it, in one command.
#
#   curl -fsSL https://raw.githubusercontent.com/rrahimi-uci/caliber-suite/main/scripts/install.sh | bash
#
# It installs the release's wheels into a private virtual environment, migrates the
# database, writes a configuration file and a generated admin password, adds a `caliber`
# command (up, down, status, logs, password, open) and starts CALIBER with a native MLflow.
# No Docker and no checkout. Everything lives under one directory (default ~/.caliber);
# running the script again upgrades in place and keeps your data, configuration and password.
#
# Usage:
#   install.sh [--version VERSION] [--prefix DIR] [--bin-dir DIR] [--python PYTHON]
#              [--port PORT] [--mlflow-port PORT] [--with-llm] [--no-start]
#              [--wheel-dir DIR] [--base-url URL]
#
#   --version VERSION  release to install (default: 0.1.0a1, tag caliber-v0.1.0a1)
#   --prefix DIR       install directory (default: ~/.caliber, or $CALIBER_HOME)
#   --bin-dir DIR      where the `caliber` command is linked (default: ~/.local/bin)
#   --python PYTHON    interpreter for the virtual environment; must be 3.10-3.12
#                      (default: the first of python3.12, python3.11, python3.10, python3)
#   --port PORT        CALIBER's port (default 5001, or the next free one on a first install)
#   --mlflow-port PORT MLflow's port (default 5000, or the next free one on a first install)
#   --with-llm         also install the OpenAI/Anthropic extras; with OPENAI_API_KEY in
#                      ~/.caliber/caliber.env the LLM and evaluator become real
#   --no-start         install and configure, but do not start the services
#   --wheel-dir DIR    install from wheels already on disk instead of downloading
#   --base-url URL     download from this mirror instead of GitHub releases (it must hold
#                      caliber-v<VERSION>/<asset> paths, as GitHub does)
#
# Needs: bash, curl, and Python 3.10-3.12 with network access to PyPI (pip resolves MLflow
# and the other dependencies). Exit status: 0 on success, 1 on a failed step, 2 on a usage
# error. Remove everything with: caliber down && rm -rf ~/.caliber ~/.local/bin/caliber

set -euo pipefail

VERSION="${CALIBER_VERSION:-0.1.0a1}"
PREFIX="${CALIBER_HOME:-$HOME/.caliber}"
BIN_DIR="$HOME/.local/bin"
PYTHON=""
PORT=""
MLFLOW_PORT=""
WITH_LLM=0
START=1
WHEEL_DIR=""
BASE_URL="https://github.com/rrahimi-uci/caliber-suite/releases/download"

usage() { # the header above, or a pointer to it when the script arrives on stdin (curl | bash)
  if [[ -f "${BASH_SOURCE[0]:-}" ]]; then
    awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}"
  else
    echo "Options: --version --prefix --bin-dir --python --port --mlflow-port --with-llm --no-start --wheel-dir --base-url"
    echo "Details: https://github.com/rrahimi-uci/caliber-suite/blob/main/scripts/install.sh"
  fi
}
need_arg() { [[ $# -ge 2 && -n "$2" ]] || { echo "install.sh: $1 needs a value" >&2; exit 2; }; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) need_arg "$@"; VERSION="$2"; shift 2 ;;
    --prefix) need_arg "$@"; PREFIX="$2"; shift 2 ;;
    --bin-dir) need_arg "$@"; BIN_DIR="$2"; shift 2 ;;
    --python) need_arg "$@"; PYTHON="$2"; shift 2 ;;
    --port) need_arg "$@"; PORT="$2"; shift 2 ;;
    --mlflow-port) need_arg "$@"; MLFLOW_PORT="$2"; shift 2 ;;
    --with-llm) WITH_LLM=1; shift ;;
    --no-start) START=0; shift ;;
    --wheel-dir) need_arg "$@"; WHEEL_DIR="$2"; shift 2 ;;
    --base-url) need_arg "$@"; BASE_URL="${2%/}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "install.sh: unknown argument: $1 (see --help)" >&2; exit 2 ;;
  esac
done

for port in "$PORT" "$MLFLOW_PORT"; do
  [[ -z "$port" || "$port" =~ ^[0-9]+$ ]] || { echo "install.sh: not a port number: $port" >&2; exit 2; }
done
[[ "$VERSION" =~ ^[0-9A-Za-z.+-]+$ ]] || { echo "install.sh: not a version: $VERSION" >&2; exit 2; }

step() { printf '\n==> %s\n' "$*"; }
fail() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------------ prerequisites
command -v curl >/dev/null 2>&1 || fail "curl is required (for example 'brew install curl' or 'sudo apt-get install curl')"

supported() { "$1" -c 'import sys; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] <= (3, 12) else 1)' 2>/dev/null; }
if [[ -n "$PYTHON" ]]; then
  command -v "$PYTHON" >/dev/null 2>&1 || fail "interpreter not found: $PYTHON"
  supported "$PYTHON" || fail "$PYTHON is not Python 3.10-3.12 (CALIBER does not support 3.13 yet)"
else
  for candidate in python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && supported "$candidate"; then PYTHON="$candidate"; break; fi
  done
  [[ -n "$PYTHON" ]] || fail "no Python 3.10-3.12 found. Install one (for example 'brew install python@3.12' or 'sudo apt-get install python3.12-venv') or pass --python"
fi
PYTHON="$(command -v "$PYTHON")"

if command -v shasum >/dev/null 2>&1; then SHA256=(shasum -a 256)
elif command -v sha256sum >/dev/null 2>&1; then SHA256=(sha256sum)
else fail "shasum or sha256sum is required to verify the download"; fi

mkdir -p "$PREFIX"
PREFIX="$(cd "$PREFIX" && pwd)"
if [[ -n "$WHEEL_DIR" ]]; then
  [[ -d "$WHEEL_DIR" ]] || fail "no such directory: $WHEEL_DIR"
  WHEEL_DIR="$(cd "$WHEEL_DIR" && pwd)"
fi

if [[ -x "$PREFIX/bin/caliber" ]] && "$PREFIX/bin/caliber" status >/dev/null 2>&1; then
  fail "CALIBER is running from $PREFIX. Stop it with 'caliber down', then run the installer again."
fi

# ------------------------------------------------------------------ wheels
PACKAGES=(caliber_sdk caliber_cli caliber_suite)
if [[ -z "$WHEEL_DIR" ]]; then
  step "Downloading CALIBER $VERSION"
  WHEEL_DIR="$PREFIX/downloads/$VERSION"
  mkdir -p "$WHEEL_DIR"
  url="$BASE_URL/caliber-v$VERSION"
  curl -fsSL --retry 3 -o "$WHEEL_DIR/SHA256SUMS" "$url/SHA256SUMS" \
    || fail "could not download $url/SHA256SUMS (is the version right? see https://github.com/rrahimi-uci/caliber-suite/releases)"
  for name in "${PACKAGES[@]}"; do
    wheel="$name-$VERSION-py3-none-any.whl"
    expected="$(awk -v f="$wheel" '$2 == f || $2 == "*" f { print $1 }' "$WHEEL_DIR/SHA256SUMS")"
    [[ -n "$expected" ]] || fail "SHA256SUMS of $VERSION does not list $wheel"
    curl -fsSL --retry 3 -o "$WHEEL_DIR/$wheel" "$url/$wheel" || fail "could not download $url/$wheel"
    actual="$("${SHA256[@]}" "$WHEEL_DIR/$wheel" | awk '{ print $1 }')"
    if [[ "$actual" != "$expected" ]]; then
      rm -f "$WHEEL_DIR/$wheel"
      fail "checksum mismatch for $wheel (expected $expected, got $actual); the download was removed"
    fi
    echo "verified $wheel"
  done
fi
for name in "${PACKAGES[@]}"; do
  compgen -G "$WHEEL_DIR/${name}-*.whl" >/dev/null || fail "no $name wheel in $WHEEL_DIR"
done

# ------------------------------------------------------------------ install
step "Installing into $PREFIX/venv (Python: $PYTHON)"
[[ -x "$PREFIX/venv/bin/python" ]] || "$PYTHON" -m venv "$PREFIX/venv" || fail "could not create a virtual environment (on Debian/Ubuntu: sudo apt-get install python3-venv)"
suite="caliber-suite"
[[ "$WITH_LLM" == 1 ]] && suite="caliber-suite[llm,anthropic]"
"$PREFIX/venv/bin/python" -m pip install -q --upgrade pip >/dev/null 2>&1 || true
"$PREFIX/venv/bin/python" -m pip install -q --upgrade --find-links "$WHEEL_DIR" \
  "caliber-sdk==$VERSION" "caliber-cli==$VERSION" "$suite==$VERSION" \
  || fail "pip could not install CALIBER $VERSION and its dependencies (is PyPI reachable?)"

# ------------------------------------------------------------------ configuration
free_port() { # free_port PREFERRED: PREFERRED when free, else one the OS picks
  "$PREFIX/venv/bin/python" - "$1" <<'PY'
import socket, sys
for port in (int(sys.argv[1]), 0):
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            continue
        print(s.getsockname()[1])
        break
PY
}

mkdir -p "$PREFIX/data" "$PREFIX/logs" "$PREFIX/run" "$PREFIX/bin"
ENV_FILE="$PREFIX/caliber.env"
PASSWORD_FILE="$PREFIX/admin-password"
if [[ ! -f "$ENV_FILE" ]]; then
  step "Writing $ENV_FILE"
  PORT="${PORT:-$(free_port 5001)}"
  MLFLOW_PORT="${MLFLOW_PORT:-$(free_port 5000)}"
  [[ "$PORT" != "$MLFLOW_PORT" ]] || fail "CALIBER and MLflow cannot share port $PORT"
  if [[ "$WITH_LLM" == 1 ]]; then providers="auto"; else providers="fake"; fi
  cat >"$ENV_FILE" <<EOF
# CALIBER configuration, read by the 'caliber' command on every start.
# Plain KEY=value lines. Restart with 'caliber down && caliber up' after editing.
CALIBER_PORT=$PORT
MLFLOW_PORT=$MLFLOW_PORT
MLFLOW_TRACKING_URI=http://127.0.0.1:$MLFLOW_PORT
CALIBER_DATABASE_URL=sqlite:///$PREFIX/data/caliber.db

# The admin account is created on the first start with the password in $PASSWORD_FILE.
# Change it under Administration; this file is only read while no account exists.
CALIBER_ADMIN_USERS=admin
CALIBER_OPERATOR_USERS=admin
CALIBER_APPROVER_USERS=admin
CALIBER_AUTH_BOOTSTRAP_ADMIN_USER=admin
CALIBER_AUTH_BOOTSTRAP_ADMIN_PASSWORD_ENV=file://$PASSWORD_FILE
# Plain HTTP on 127.0.0.1 only. Put TLS in front before exposing CALIBER on a network.
CALIBER_AUTH_SESSION_COOKIE_SECURE=false

CALIBER_PROMOTER_PROVIDER=mlflow
CALIBER_ARTIFACT_STORE_PROVIDER=mlflow
CALIBER_LOG_SINK=stderr

# Models. "fake" is the honest offline default: calibration runs, scores are simulated and
# every screen says so. For real models, reinstall with --with-llm, add your key here and set
# CALIBER_LLM_PROVIDER=auto and CALIBER_EVAL_PROVIDER=auto.
CALIBER_LLM_PROVIDER=$providers
CALIBER_EVAL_PROVIDER=$providers
# OPENAI_API_KEY=
# ANTHROPIC_API_KEY=
EOF
else
  step "Keeping the existing configuration in $ENV_FILE"
  [[ -z "$PORT$MLFLOW_PORT" ]] || echo "note: --port/--mlflow-port apply to a first install only; edit $ENV_FILE to change them"
fi
if [[ ! -s "$PASSWORD_FILE" ]]; then
  ( umask 077; "$PREFIX/venv/bin/python" -c 'import secrets, sys; sys.stdout.write(secrets.token_urlsafe(18))' >"$PASSWORD_FILE" )
fi
chmod 600 "$ENV_FILE" "$PASSWORD_FILE"

step "Migrating the database"
( set -a; . "$ENV_FILE"; set +a; "$PREFIX/venv/bin/caliber-migrate" ) || fail "caliber-migrate failed"

# ------------------------------------------------------------------ the caliber command
step "Installing the 'caliber' command"
{
  printf '#!/usr/bin/env bash\n'
  printf '# Written by install.sh for CALIBER %s. Re-run the installer to update it.\n' "$VERSION"
  printf 'CALIBER_HOME=%q\n' "$PREFIX"
  cat <<'LAUNCHER'
set -euo pipefail
ENV_FILE="$CALIBER_HOME/caliber.env"
RUN="$CALIBER_HOME/run"
LOGS="$CALIBER_HOME/logs"
VENV="$CALIBER_HOME/venv/bin"
[[ -f "$ENV_FILE" ]] || { echo "caliber: $ENV_FILE is missing; run the installer again" >&2; exit 1; }
set -a; . "$ENV_FILE"; set +a
API="http://127.0.0.1:$CALIBER_PORT/ajax-api/2.0/mlflow/caliber"
UI="http://127.0.0.1:$CALIBER_PORT/caliber/"

alive() { [[ -f "$RUN/$1.pid" ]] && kill -0 "$(cat "$RUN/$1.pid")" 2>/dev/null; }
kill_tree() {
  local child
  for child in $(pgrep -P "$1" 2>/dev/null || true); do kill_tree "$child"; done
  kill "$1" 2>/dev/null || true
}
port_busy() { "$VENV/python" -c 'import socket,sys; s=socket.socket(); r=s.connect_ex(("127.0.0.1",int(sys.argv[1]))); s.close(); raise SystemExit(0 if r==0 else 1)' "$1"; }
wait_for() { # wait_for URL SECONDS NAME
  local i
  for ((i = 0; i < $2; i++)); do
    curl -sf "$1" >/dev/null 2>&1 && return 0
    alive "$3" || { tail -n 30 "$LOGS/$3.log" >&2; echo "caliber: $3 exited during startup (log: $LOGS/$3.log)" >&2; return 1; }
    sleep 1
  done
  tail -n 30 "$LOGS/$3.log" >&2
  echo "caliber: $3 did not answer at $1 within $2s (log: $LOGS/$3.log)" >&2
  return 1
}
start() { # start NAME COMMAND...
  local name="$1"; shift
  nohup "$@" >>"$LOGS/$name.log" 2>&1 &
  echo "$!" >"$RUN/$name.pid"
}
stop() {
  if alive "$1"; then kill_tree "$(cat "$RUN/$1.pid")"; echo "stopped $1"; fi
  rm -f "$RUN/$1.pid"
}

case "${1:-help}" in
  up)
    mkdir -p "$RUN" "$LOGS"
    if alive caliber && alive mlflow; then echo "CALIBER is already running at $UI"; exit 0; fi
    stop caliber >/dev/null; stop mlflow >/dev/null
    for p in "$MLFLOW_PORT" "$CALIBER_PORT"; do
      if port_busy "$p"; then echo "caliber: port $p is in use by another program; change it in $ENV_FILE" >&2; exit 1; fi
    done
    "$VENV/caliber-migrate" >>"$LOGS/caliber.log" 2>&1 || { echo "caliber: migration failed (log: $LOGS/caliber.log)" >&2; exit 1; }
    start mlflow "$VENV/mlflow" server \
      --backend-store-uri "sqlite:///$CALIBER_HOME/data/mlflow.db" \
      --default-artifact-root "$CALIBER_HOME/data/mlflow-artifacts" \
      --host 127.0.0.1 --port "$MLFLOW_PORT"
    wait_for "http://127.0.0.1:$MLFLOW_PORT/health" 120 mlflow || { stop mlflow; exit 1; }
    start caliber "$VENV/uvicorn" caliber.server:create_app --factory --host 127.0.0.1 --port "$CALIBER_PORT"
    wait_for "$API/health" 180 caliber || { stop caliber; stop mlflow; exit 1; }
    echo
    echo "CALIBER is running at $UI"
    echo "  sign in as admin; password: caliber password"
    echo "  stop with: caliber down"
    ;;
  down) stop caliber; stop mlflow ;;
  status)
    rc=0
    for name in mlflow caliber; do
      if alive "$name"; then echo "$name: running (pid $(cat "$RUN/$name.pid"))"; else echo "$name: stopped"; rc=1; fi
    done
    [[ "$rc" == 0 ]] && echo "UI: $UI"
    exit "$rc"
    ;;
  logs) tail -n 100 -f "$LOGS/${2:-caliber}.log" ;;
  password) cat "$CALIBER_HOME/admin-password"; echo ;;
  open)
    if command -v open >/dev/null 2>&1; then open "$UI"
    elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$UI"
    else echo "$UI"; fi
    ;;
  help|-h|--help)
    cat <<EOF
caliber up         start MLflow and CALIBER in the background
caliber down       stop them (data is kept in $CALIBER_HOME/data)
caliber status     show whether they are running (exit 1 if not)
caliber logs [caliber|mlflow]   follow a log
caliber password   print the generated admin password
caliber open       open the UI in a browser
Configuration: $ENV_FILE
EOF
    ;;
  *) echo "caliber: unknown command: $1 (see 'caliber help')" >&2; exit 2 ;;
esac
LAUNCHER
} >"$PREFIX/bin/caliber"
chmod 755 "$PREFIX/bin/caliber"
mkdir -p "$BIN_DIR"
if [[ -e "$BIN_DIR/caliber" && ! -L "$BIN_DIR/caliber" ]]; then
  # Never replace someone else's program of the same name.
  echo "note: $BIN_DIR/caliber already exists and is not this installer's link; left it alone. Run $PREFIX/bin/caliber instead."
else
  ln -sf "$PREFIX/bin/caliber" "$BIN_DIR/caliber"
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "note: $BIN_DIR is not on your PATH. Add it (for example: echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.zshrc), or run $PREFIX/bin/caliber" ;;
  esac
fi

if [[ "$START" == 1 ]]; then
  step "Starting CALIBER"
  "$PREFIX/bin/caliber" up
else
  printf '\nInstalled CALIBER %s in %s. Start it with: caliber up\n' "$VERSION" "$PREFIX"
fi
