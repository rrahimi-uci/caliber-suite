#!/usr/bin/env bash
#
# cookbook-env.sh — start a disposable CALIBER stack to run the cookbook labs against.
#
# The cookbook is executable documentation, so its steps have to be run against a real server,
# not a stub. This script brings up the stack docs/start/alpha-install.md describes, from this
# checkout's development venv (editable installs, so a code fix is live after `restart`):
#
#   caliber-migrate -> native MLflow server -> CALIBER (uvicorn) -> /readiness ready
#
# Each named environment has its own ports and state directory under .tmp/cookbook-env/NAME/,
# so several can run side by side and none touches a developer's own stack.
#
# Usage:
#   scripts/cookbook-env.sh up      NAME [--flags LIST] [--real-llm] [--no-workers] [--s3]
#   scripts/cookbook-env.sh restart NAME            (same flags as the last `up`)
#   scripts/cookbook-env.sh down    NAME
#   scripts/cookbook-env.sh env     NAME            (print `export` lines; eval them in a shell)
#   scripts/cookbook-env.sh status
#   scripts/cookbook-env.sh build-ui                (rebuild the SPA bundle the server serves)
#
#   --flags LIST   comma list of aria, integrations, cookbooks, release_candidates, or `all`
#                  (default: none, the shipped alpha default: every preview area off).
#   --real-llm     do not force the simulated providers. The LLM is OpenAI (CALIBER_LLM_PROVIDER=openai)
#                  and the evaluator is the real MLflow one (CALIBER_EVAL_PROVIDER=mlflow), unless you set
#                  them yourself. OPENAI_API_KEY / ANTHROPIC_API_KEY come from the environment, else from
#                  the repository-root .env (only those two keys are read from it, and they are never
#                  printed or written to the state directory). Calls cost real money: keep runs small.
#                  (default: simulated LLM and evaluator).
#   --no-workers   do not run background workers.
#   --s3           also start a private S3-compatible endpoint (moto_server, in memory) and point the
#                  Object Store console and the workflow storage backend at it, for the labs that read
#                  or write buckets. Its endpoint is CALIBER_S3_ENDPOINT in env.sh (admin creds
#                  minioadmin / minioadmin). Objects do not survive `restart`.
#
# After `up`, the stack serves the web app at $CALIBER_URL/caliber/ and the API at $CALIBER_API,
# and you sign in as admin / admin. `env` prints CALIBER_URL, CALIBER_API, MLFLOW_TRACKING_URI
# and the file the server logs to.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CALIBER_DIR="$REPO_ROOT/caliber"
VENV="${CALIBER_COOKBOOK_VENV:-$CALIBER_DIR/.venv}"
ROOT="$REPO_ROOT/.tmp/cookbook-env"

die() { printf 'cookbook-env: %s\n' "$*" >&2; exit 1; }
say() { printf '==> %s\n' "$*"; }

[[ -x "$VENV/bin/python" ]] || die "no virtualenv at $VENV (run 'make install' first)"

dotenv_value() { # dotenv_value KEY -> the value of KEY in the repository-root .env, or nothing
  local line
  [[ -f "$REPO_ROOT/.env" ]] || return 0
  line="$(grep -E "^$1=" "$REPO_ROOT/.env" | tail -1 || true)"
  line="${line#*=}"
  line="${line%\"}"; line="${line#\"}"; line="${line%\'}"; line="${line#\'}"
  printf '%s' "$line"
}

free_port() {
  "$VENV/bin/python" - <<'PY'
import socket
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    print(s.getsockname()[1])
PY
}

wait_for() { # wait_for URL SECONDS LABEL
  local url="$1" seconds="$2" label="$3" i
  for ((i = 0; i < seconds; i++)); do
    curl -sf "$url" >/dev/null 2>&1 && return 0
    sleep 1
  done
  die "$label did not become healthy at $url within ${seconds}s (see $STATE/*.log)"
}

alive() { [[ -f "$1" ]] && kill -0 "$(cat "$1")" 2>/dev/null; }

build_ui() {
  local ui="$CALIBER_DIR/src/caliber/ui"
  if [[ -f "$ui/index.html" ]] && [[ -z "$(find "$CALIBER_DIR/caliber-ui/src" "$CALIBER_DIR/caliber-ui/index.html" \
      -newer "$ui/index.html" -type f 2>/dev/null | head -1)" ]]; then
    return 0
  fi
  say "Building the web app bundle (the sources are newer than caliber/src/caliber/ui)"
  (cd "$CALIBER_DIR/caliber-ui" && { [[ -d node_modules ]] || npm ci >/dev/null; } && npx vite build >/dev/null)
  rm -rf "$ui"
  mkdir -p "$ui"
  cp -R "$CALIBER_DIR/caliber-ui/dist/." "$ui/"
}

flag_vars() { # flag_vars LIST -> prints KEY=VALUE lines
  local list="$1" id
  [[ "$list" == "all" ]] && list="aria,integrations,cookbooks,release_candidates"
  for id in ${list//,/ }; do
    case "$id" in
      aria|integrations|cookbooks|release_candidates)
        printf 'CALIBER_FEATURE_%s=true\n' "$(printf '%s' "$id" | tr '[:lower:]' '[:upper:]')" ;;
      none|"") ;;
      *) die "unknown flag: $id" ;;
    esac
  done
}

start_stack() { # start_stack NAME
  local name="$1" STATE="$ROOT/$1"
  mkdir -p "$STATE"
  # shellcheck disable=SC1091
  source "$STATE/config" 2>/dev/null || true
  local flags="${FLAGS:-none}" real="${REAL_LLM:-0}" workers="${WORKERS:-1}" s3="${S3:-0}"
  local s3_port="${S3_PORT:-0}"
  [[ "$s3" == 1 && "$s3_port" == 0 ]] && s3_port="$(free_port)"
  local mlflow_port="${MLFLOW_PORT:-$(free_port)}" caliber_port="${CALIBER_PORT:-$(free_port)}"
  printf 'FLAGS=%q\nREAL_LLM=%q\nWORKERS=%q\nS3=%q\nS3_PORT=%q\nMLFLOW_PORT=%q\nCALIBER_PORT=%q\n' \
    "$flags" "$real" "$workers" "$s3" "$s3_port" "$mlflow_port" "$caliber_port" >"$STATE/config"

  build_ui

  say "Migrating the CALIBER database ($STATE/caliber.db)"
  (
    export CALIBER_DATABASE_URL="sqlite:///$STATE/caliber.db"
    "$VENV/bin/python" -m caliber.db.migrate >"$STATE/migrate.log" 2>&1
  ) || die "caliber-migrate failed (see $STATE/migrate.log)"

  if ! alive "$STATE/mlflow.pid"; then
    say "Starting MLflow on port $mlflow_port"
    "$VENV/bin/mlflow" server \
      --backend-store-uri "sqlite:///$STATE/mlflow.db" \
      --default-artifact-root "$STATE/mlflow-artifacts" \
      --host 127.0.0.1 --port "$mlflow_port" >"$STATE/mlflow.log" 2>&1 &
    echo $! >"$STATE/mlflow.pid"
  fi
  wait_for "http://127.0.0.1:$mlflow_port/health" 120 "MLflow"

  if [[ "$s3" == 1 ]]; then
    if ! alive "$STATE/s3.pid"; then
      say "Starting an S3-compatible endpoint (moto_server) on port $s3_port"
      "$VENV/bin/moto_server" -H 127.0.0.1 -p "$s3_port" >"$STATE/s3.log" 2>&1 &
      echo $! >"$STATE/s3.pid"
    fi
    wait_for "http://127.0.0.1:$s3_port/" 60 "the S3 endpoint"
    # moto keeps everything in memory, so the workflow storage bucket is created on every start.
    "$VENV/bin/python" - "$s3_port" <<'PY'
import sys

import boto3

boto3.client(
    "s3",
    endpoint_url=f"http://127.0.0.1:{sys.argv[1]}",
    region_name="us-east-1",
    aws_access_key_id="minioadmin",
    aws_secret_access_key="minioadmin",
).create_bucket(Bucket="caliber-workspaces")
PY
  fi

  if ! alive "$STATE/caliber.pid"; then
    say "Starting CALIBER on port $caliber_port (flags: $flags)"
    (
      cd "$STATE"
      export CALIBER_DATABASE_URL="sqlite:///$STATE/caliber.db"
      export MLFLOW_TRACKING_URI="http://127.0.0.1:$mlflow_port"
      # admin only by default; a lab that needs more people with platform scopes (lab 18) exports
      # CALIBER_ADMIN_USERS / CALIBER_OPERATOR_USERS / CALIBER_APPROVER_USERS before `up`/`restart`.
      export CALIBER_ADMIN_USERS="${CALIBER_ADMIN_USERS:-admin}"
      export CALIBER_OPERATOR_USERS="${CALIBER_OPERATOR_USERS:-admin}"
      export CALIBER_APPROVER_USERS="${CALIBER_APPROVER_USERS:-admin}"
      export CALIBER_PROMOTER_PROVIDER=mlflow CALIBER_ARTIFACT_STORE_PROVIDER=mlflow
      export CALIBER_LOG_SINK=stderr
      export CALIBER_AUTH_SESSION_COOKIE_SECURE=false
      export CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT=true
      export CALIBER_WORKFLOW_STORAGE_BASE_URI="file://$STATE/workspaces"
      # on unless the caller sets it (lab 07 restarts a stack with it false to show the refusal)
      export CALIBER_WORKFLOW_RUN_QUEUE_ENABLED="${CALIBER_WORKFLOW_RUN_QUEUE_ENABLED:-true}"
      export CALIBER_WORKFLOW_RUN_CHECKPOINTING_ENABLED=true
      export CALIBER_WORKFLOW_RUN_RUNTIME_APPROVALS_ENABLED=true
      export CALIBER_SKIP_KNOWLEDGE_WARMUP=1
      if [[ "$s3" == 1 ]]; then
        export AWS_ACCESS_KEY_ID=minioadmin AWS_SECRET_ACCESS_KEY=minioadmin
        export CALIBER_WORKFLOW_STORAGE_BACKEND=s3 CALIBER_WORKFLOW_STORAGE_BUCKET=caliber-workspaces
        export CALIBER_WORKFLOW_STORAGE_INTERNAL_ENDPOINT_URL="http://127.0.0.1:$s3_port"
        export CALIBER_WORKFLOW_STORAGE_FORCE_PATH_STYLE=true
        export CALIBER_WORKFLOW_STORAGE_ACCESS_KEY_SOURCE=AWS_ACCESS_KEY_ID
        export CALIBER_WORKFLOW_STORAGE_SECRET_KEY_SOURCE=AWS_SECRET_ACCESS_KEY
        export CALIBER_OBJECT_STORE_ENDPOINT_URL="http://127.0.0.1:$s3_port"
        export CALIBER_OBJECT_STORE_FORCE_PATH_STYLE=true
        export CALIBER_OBJECT_STORE_ACCESS_KEY_SOURCE=AWS_ACCESS_KEY_ID
        export CALIBER_OBJECT_STORE_SECRET_KEY_SOURCE=AWS_SECRET_ACCESS_KEY
      fi
      if [[ "$workers" == 1 ]]; then
        export CALIBER_BACKGROUND_TASKS_ENABLED=true
      else
        export CALIBER_BACKGROUND_TASKS_ENABLED=false
      fi
      if [[ "$real" != 1 ]]; then
        export CALIBER_LLM_PROVIDER=fake CALIBER_EVAL_PROVIDER=fake CALIBER_ASSISTANT_ENGINE=fake
      else
        # A real provider: keys from the caller's environment or, failing that, the root .env.
        # Only the two key variables are read from the file (it also holds the owner's own stack
        # settings, which must not leak into a disposable stack).
        for key in OPENAI_API_KEY ANTHROPIC_API_KEY; do
          if [[ -z "${!key:-}" ]]; then
            value="$(dotenv_value "$key")"
            [[ -n "$value" ]] && export "$key=$value"
          fi
        done
        export CALIBER_LLM_PROVIDER="${CALIBER_LLM_PROVIDER:-openai}"
        export CALIBER_EVAL_PROVIDER="${CALIBER_EVAL_PROVIDER:-mlflow}"
        export CALIBER_ASSISTANT_ENGINE="${CALIBER_ASSISTANT_ENGINE:-openai}"
      fi
      while IFS= read -r line; do [[ -n "$line" ]] && export "$line"; done < <(flag_vars "$flags")
      exec "$VENV/bin/uvicorn" caliber.server:create_app --factory \
        --host 127.0.0.1 --port "$caliber_port" >"$STATE/caliber.log" 2>&1
    ) &
    echo $! >"$STATE/caliber.pid"
  fi
  wait_for "http://127.0.0.1:$caliber_port/ajax-api/2.0/mlflow/caliber/health" 180 "CALIBER"

  cat >"$STATE/env.sh" <<EOF
export CALIBER_ENV_NAME=$name
export CALIBER_URL=http://127.0.0.1:$caliber_port
export CALIBER_BASE_URL=http://127.0.0.1:$caliber_port
export CALIBER_API=http://127.0.0.1:$caliber_port/ajax-api/2.0/mlflow/caliber
export MLFLOW_TRACKING_URI=http://127.0.0.1:$mlflow_port
export CALIBER_USER=admin
export CALIBER_PASSWORD=admin
export CALIBER_SERVER_LOG=$STATE/caliber.log
export CALIBER_STATE_DIR=$STATE
EOF
  [[ "$s3" == 1 ]] && printf 'export CALIBER_S3_ENDPOINT=http://127.0.0.1:%s\n' "$s3_port" >>"$STATE/env.sh"
  say "Ready: $name"
  cat "$STATE/env.sh"
}

stop_stack() { # stop_stack NAME
  local STATE="$ROOT/$1" pidfile pid
  for pidfile in "$STATE/caliber.pid" "$STATE/mlflow.pid" "$STATE/s3.pid"; do
    if alive "$pidfile"; then
      pid="$(cat "$pidfile")"
      kill "$pid" 2>/dev/null || true
      for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$pidfile"
  done
}

cmd="${1:-}"; shift || true
case "$cmd" in
  up)
    name="${1:?usage: cookbook-env.sh up NAME [--flags LIST]}"; shift
    STATE="$ROOT/$name"; mkdir -p "$STATE"
    FLAGS=none; REAL_LLM=0; WORKERS=1; S3=0; S3_PORT=0
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --flags) FLAGS="${2:?--flags needs a list}"; shift 2 ;;
        --real-llm) REAL_LLM=1; shift ;;
        --no-workers) WORKERS=0; shift ;;
        --s3) S3=1; shift ;;
        *) die "unknown argument: $1" ;;
      esac
    done
    if alive "$STATE/caliber.pid"; then die "$name is already running (use restart or down)"; fi
    printf 'FLAGS=%q\nREAL_LLM=%q\nWORKERS=%q\nS3=%q\nS3_PORT=%q\n' "$FLAGS" "$REAL_LLM" "$WORKERS" "$S3" "$S3_PORT" >"$STATE/config"
    start_stack "$name"
    ;;
  restart)
    name="${1:?usage: cookbook-env.sh restart NAME}"
    stop_stack "$name"
    start_stack "$name"
    ;;
  down)
    name="${1:?usage: cookbook-env.sh down NAME}"
    stop_stack "$name"
    say "Stopped $name (state kept in $ROOT/$name; delete the directory to reset)"
    ;;
  env)
    name="${1:?usage: cookbook-env.sh env NAME}"
    cat "$ROOT/$name/env.sh"
    ;;
  status)
    for d in "$ROOT"/*/; do
      [[ -d "$d" ]] || continue
      n="$(basename "$d")"
      if alive "$d/caliber.pid"; then s=running; else s=stopped; fi
      printf '%-24s %s\n' "$n" "$s"
    done
    ;;
  build-ui) build_ui ;;
  -h|--help|"") sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) die "unknown command: $cmd" ;;
esac
