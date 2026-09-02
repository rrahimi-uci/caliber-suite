#!/usr/bin/env bash

# Deterministic CALIBER server bootstrap for Playwright E2E.
#
# - Uses a shared per-port state directory under ./.tmp
# - Lets concurrent Playwright invocations reuse one bootstrap instead of
#   racing migrations, temp-file cleanup, or port binding
# - Cleans up only after the last active client exits

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_PATH="$REPO_ROOT/scripts/run-playwright-server.sh"
cd "$REPO_ROOT"

VENV_DIR="${VENV_DIR:-.venv}"
if [[ "${1:-}" != "--force-cleanup" && ! -x "$VENV_DIR/bin/python" ]]; then
  echo "error: virtualenv not found at $VENV_DIR (run make install first)" >&2
  exit 1
fi

PROJECT_ENV_FILE="${CALIBER_E2E_ENV_FILE:-$REPO_ROOT/../.env}"

load_e2e_env_defaults() {
  local env_file="$1"
  [[ -f "$env_file" ]] || return 0

  local entry
  while IFS= read -r -d '' entry; do
    local key="${entry%%=*}"
    local value="${entry#*=}"
    case "$key" in
      CALIBER_DATABASE_URL | CALIBER_KNOWLEDGE_AGE_ENABLED | CALIBER_KNOWLEDGE_AGE_GRAPH_NAME | POSTGRES_URL)
        if [[ -z "${!key+x}" ]]; then
          export "$key=$value"
        fi
        ;;
    esac
  done < <(env -i bash -a -c "source \"$env_file\" >/dev/null 2>&1; env -0")
}

load_e2e_env_defaults "$PROJECT_ENV_FILE"
if [[ -z "${CALIBER_DATABASE_URL:-}" && -n "${POSTGRES_URL:-}" ]]; then
  export CALIBER_DATABASE_URL="$POSTGRES_URL"
fi

export MLFLOW_HOST="${MLFLOW_HOST:-127.0.0.1}"
export MLFLOW_PORT="${MLFLOW_PORT:-5150}"

TMP_ROOT="${CALIBER_E2E_TMP_ROOT:-$REPO_ROOT/.tmp}"
STATE_DIR="${CALIBER_E2E_STATE_DIR:-$TMP_ROOT/playwright-server-$MLFLOW_PORT}"
LOCK_DIR="$STATE_DIR.lock"
CLIENTS_DIR="$STATE_DIR/clients"
BOOTSTRAP_OWNER_FILE="$STATE_DIR/bootstrap-owner.pid"
WATCHER_PID_FILE="$STATE_DIR/cleanup-watcher.pid"
PID_FILE="$STATE_DIR/server.pid"
LOG_FILE="$STATE_DIR/server.log"
DB_FILE="${CALIBER_E2E_DB_FILE:-$STATE_DIR/playwright-e2e.db}"
PLAYWRIGHT_WORKSPACES="$STATE_DIR/workspaces"
PLAYWRIGHT_ARTIFACTS="$STATE_DIR/artifacts"
DEFAULT_HF_HOME="${CALIBER_E2E_HF_HOME:-$TMP_ROOT/huggingface}"
HEALTH_URL="http://${MLFLOW_HOST}:${MLFLOW_PORT}/ajax-api/2.0/mlflow/caliber/health"
BOOTSTRAP_TIMEOUT_SECONDS="${CALIBER_E2E_BOOTSTRAP_TIMEOUT_SECONDS:-330}"
CLIENT_MARKER="$CLIENTS_DIR/$$"
# Prewarm the local embedding backend by default so the first knowledge-base
# build in the Playwright suite does not spend its test budget on a cold model
# download/load. Developers can still opt out with CALIBER_SKIP_KNOWLEDGE_WARMUP=1
# when they only need the non-knowledge browser flows.
export CALIBER_SKIP_KNOWLEDGE_WARMUP="${CALIBER_SKIP_KNOWLEDGE_WARMUP:-0}"
# Playwright's disposable stack is the one place we intentionally allow the
# local Hugging Face embedding path even when the optional torch runtime is
# under an advisory. Production and ordinary local runs still default to the
# safer blocked posture because this override only lives in the e2e harness.
export CALIBER_ALLOW_FLAGGED_LOCAL_EMBEDDINGS="${CALIBER_ALLOW_FLAGGED_LOCAL_EMBEDDINGS:-true}"

mkdir -p "$TMP_ROOT"
export CALIBER_E2E_TMP_ROOT="$TMP_ROOT"
export CALIBER_E2E_STATE_DIR="$STATE_DIR"
export VENV_DIR="$VENV_DIR"

acquire_lock() {
  while ! mkdir "$LOCK_DIR" 2>/dev/null; do
    if [[ ! -d "$LOCK_DIR" ]]; then
      continue
    fi
    if [[ -f "$LOCK_DIR/pid" ]]; then
      local holder
      holder="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
      if [[ -n "$holder" ]] && kill -0 "$holder" 2>/dev/null; then
        sleep 0.1
        continue
      fi
      rm -rf "$LOCK_DIR"
      continue
    fi
    sleep 0.1
    if [[ -d "$LOCK_DIR" && ! -f "$LOCK_DIR/pid" ]]; then
      rm -rf "$LOCK_DIR"
      continue
    fi
    sleep 0.1
  done
  printf '%s\n' "$$" > "$LOCK_DIR/pid"
}

release_lock() {
  rm -rf "$LOCK_DIR"
}

normalize_postgres_driver_url() {
  local value="$1"
  if [[ "$value" == postgresql+* ]]; then
    printf '%s\n' "$value"
    return
  fi
  if [[ "$value" == postgresql://* ]]; then
    printf 'postgresql+psycopg://%s\n' "${value#postgresql://}"
    return
  fi
  if [[ "$value" == postgres://* ]]; then
    printf 'postgresql+psycopg://%s\n' "${value#postgres://}"
    return
  fi
  printf '%s\n' "$value"
}

server_pid() {
  if [[ ! -f "$PID_FILE" ]]; then
    return 1
  fi
  cat "$PID_FILE" 2>/dev/null
}

bootstrap_owner_pid() {
  if [[ ! -f "$BOOTSTRAP_OWNER_FILE" ]]; then
    return 1
  fi
  cat "$BOOTSTRAP_OWNER_FILE" 2>/dev/null
}

cleanup_watcher_pid() {
  if [[ ! -f "$WATCHER_PID_FILE" ]]; then
    return 1
  fi
  cat "$WATCHER_PID_FILE" 2>/dev/null
}

pid_is_alive() {
  local pid="$1"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

server_pid_alive() {
  local pid
  pid="$(server_pid || true)"
  pid_is_alive "$pid"
}

bootstrap_owner_alive() {
  local pid
  pid="$(bootstrap_owner_pid || true)"
  pid_is_alive "$pid"
}

cleanup_watcher_alive() {
  local pid
  pid="$(cleanup_watcher_pid || true)"
  pid_is_alive "$pid"
}

server_healthy() {
  curl --silent --fail --max-time 2 "$HEALTH_URL" >/dev/null 2>&1
}

prune_dead_clients() {
  mkdir -p "$CLIENTS_DIR"
  local marker
  for marker in "$CLIENTS_DIR"/*; do
    [[ -e "$marker" ]] || continue
    local pid
    pid="$(basename "$marker")"
    if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$marker"
    fi
  done
}

prune_dead_server_pid() {
  local pid
  pid="$(server_pid || true)"
  if [[ -n "$pid" ]] && ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$PID_FILE"
  fi
}

prune_dead_bootstrap_owner() {
  local pid
  pid="$(bootstrap_owner_pid || true)"
  if [[ -n "$pid" ]] && ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$BOOTSTRAP_OWNER_FILE"
  fi
}

prune_dead_cleanup_watcher() {
  local pid
  pid="$(cleanup_watcher_pid || true)"
  if [[ -n "$pid" ]] && ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$WATCHER_PID_FILE"
  fi
}

active_client_count() {
  if [[ ! -d "$CLIENTS_DIR" ]]; then
    echo 0
    return
  fi
  find "$CLIENTS_DIR" -mindepth 1 -maxdepth 1 -type f | wc -l | tr -d ' '
}

prepare_fresh_state() {
  rm -f "$DB_FILE" "$PID_FILE" "$LOG_FILE"
  rm -rf "$PLAYWRIGHT_WORKSPACES" "$PLAYWRIGHT_ARTIFACTS"
  mkdir -p "$PLAYWRIGHT_WORKSPACES" "$PLAYWRIGHT_ARTIFACTS" "$DEFAULT_HF_HOME"
}

stop_pid() {
  local pid="$1"
  if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
    return
  fi
  # ``spawn_detached_command`` creates a new session whose leader may launch a
  # child server (MLflow -> uvicorn). Stop the whole process group so the child
  # cannot retain the test port after Playwright releases its client.
  kill -TERM -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
  local _
  for _ in $(seq 1 20); do
    if ! kill -0 "-$pid" 2>/dev/null; then
      return
    fi
    sleep 0.25
  done
  kill -KILL -- "-$pid" 2>/dev/null || kill -9 "$pid" 2>/dev/null || true
}

cleanup_shared_state() {
  local pid
  pid="$(server_pid || true)"
  stop_pid "$pid"
  rm -rf "$STATE_DIR"
  rmdir "$TMP_ROOT" 2>/dev/null || true
}

spawn_detached_command() {
  local log_path="$1"
  shift

  "$VENV_DIR/bin/python" - "$log_path" "$@" <<'PY'
import os
import sys

log_path = sys.argv[1]
command = sys.argv[2:]
if not command:
    sys.exit(1)

ready_r, ready_w = os.pipe()
pid = os.fork()
if pid > 0:
    os.close(ready_w)
    with os.fdopen(ready_r, "r", encoding="utf-8") as reader:
        daemon_pid = reader.read().strip()
    _, status = os.waitpid(pid, 0)
    if status != 0 or not daemon_pid:
        sys.exit(1)
    print(daemon_pid)
    sys.exit(0)

os.close(ready_r)
os.setsid()
grandchild = os.fork()
if grandchild > 0:
    os.write(ready_w, f"{grandchild}\n".encode("utf-8"))
    os.close(ready_w)
    os._exit(0)

os.close(ready_w)
devnull_fd = os.open(os.devnull, os.O_RDONLY)
log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
os.dup2(devnull_fd, 0)
os.dup2(log_fd, 1)
os.dup2(log_fd, 2)
for fd in (devnull_fd, log_fd):
    if fd > 2:
        os.close(fd)

os.execvp(command[0], command)
PY
}

register_client() {
  acquire_lock
  mkdir -p "$STATE_DIR"
  prune_dead_clients
  touch "$CLIENT_MARKER"
  release_lock
}

release_client() {
  acquire_lock
  rm -f "$CLIENT_MARKER"
  prune_dead_clients
  prune_dead_server_pid
  prune_dead_bootstrap_owner
  prune_dead_cleanup_watcher
  local remaining
  remaining="$(active_client_count)"
  if [[ "$remaining" == "0" ]]; then
    cleanup_shared_state
  fi
  release_lock
}

start_cleanup_watcher() {
  spawn_detached_command "/dev/null" "$SCRIPT_PATH" --cleanup-watcher >/dev/null
}

claim_bootstrap_if_needed() {
  acquire_lock
  mkdir -p "$STATE_DIR"
  prune_dead_clients
  prune_dead_server_pid
  prune_dead_bootstrap_owner

  if server_healthy; then
    release_lock
    return 1
  fi

  if server_pid_alive || bootstrap_owner_alive; then
    release_lock
    return 1
  fi

  printf '%s\n' "$$" > "$BOOTSTRAP_OWNER_FILE"
  prepare_fresh_state
  release_lock
  return 0
}

clear_bootstrap_owner_if_mine() {
  acquire_lock
  local owner
  owner="$(bootstrap_owner_pid || true)"
  if [[ "$owner" == "$$" ]]; then
    rm -f "$BOOTSTRAP_OWNER_FILE"
  fi
  release_lock
}

run_owner_bootstrap() {
  if "$VENV_DIR/bin/python" -c 'import importlib.util, sys; sys.exit(0 if importlib.util.find_spec("caliber.demo") else 1)'; then
    echo ">> seeding demo scenario for Playwright"
    "$VENV_DIR/bin/python" -m caliber.demo --scenario "${CALIBER_E2E_SCENARIO:-fleet}" --reset --quiet
  else
    echo ">> legacy caliber.demo seed not present; continuing with empty test fixture"
  fi

  if [[ "${CALIBER_SKIP_KNOWLEDGE_WARMUP:-0}" != "1" ]]; then
    echo ">> prewarming MiniLM knowledge embedding model for Playwright"
    "$VENV_DIR/bin/python" - <<'PY'
from caliber.knowledge.embeddings import build_embedding_backend

MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"

try:
    backend = build_embedding_backend(MODEL_ID)
    backend.embed_query("playwright knowledge base warmup")
    print(f">> knowledge embedding model ready: {MODEL_ID}")
except Exception as exc:
    print(f">> knowledge embedding warmup skipped: {type(exc).__name__}: {exc}")
PY
  fi

  local detached_pid
  detached_pid="$(spawn_detached_command "$LOG_FILE" ./scripts/run-dev.sh)"
  if [[ -z "$detached_pid" ]]; then
    return 1
  fi

  acquire_lock
  printf '%s\n' "$detached_pid" > "$PID_FILE"
  local owner
  owner="$(bootstrap_owner_pid || true)"
  if [[ "$owner" == "$$" ]]; then
    rm -f "$BOOTSTRAP_OWNER_FILE"
  fi
  release_lock
}

run_cleanup_watcher() {
  acquire_lock
  mkdir -p "$STATE_DIR"
  prune_dead_cleanup_watcher
  local watcher
  watcher="$(cleanup_watcher_pid || true)"
  if [[ -n "$watcher" && "$watcher" != "$$" ]] && kill -0 "$watcher" 2>/dev/null; then
    release_lock
    exit 0
  fi
  printf '%s\n' "$$" > "$WATCHER_PID_FILE"
  release_lock

  while true; do
    sleep 2
    acquire_lock
    prune_dead_clients
    prune_dead_server_pid
    prune_dead_bootstrap_owner
    printf '%s\n' "$$" > "$WATCHER_PID_FILE"
    local remaining
    remaining="$(active_client_count)"
    if [[ "$remaining" == "0" ]]; then
      cleanup_shared_state
      release_lock
      exit 0
    fi
    release_lock
  done
}

ensure_server_running() {
  local waited=0

  while true; do
    if server_healthy; then
      return 0
    fi

    if claim_bootstrap_if_needed; then
      if ! run_owner_bootstrap; then
        clear_bootstrap_owner_if_mine
      fi
    fi

    if server_healthy; then
      return 0
    fi

    if (( waited >= BOOTSTRAP_TIMEOUT_SECONDS )); then
      echo "error: timed out waiting for Playwright server at $HEALTH_URL" >&2
      if [[ -f "$LOG_FILE" ]]; then
        echo "recent server log:" >&2
        tail -n 80 "$LOG_FILE" >&2 || true
      fi
      return 1
    fi

    sleep 1
    waited=$((waited + 1))
  done
}

if [[ "${1:-}" == "--cleanup-watcher" ]]; then
  run_cleanup_watcher
fi

if [[ "${1:-}" == "--force-cleanup" ]]; then
  acquire_lock
  cleanup_shared_state
  release_lock
  exit 0
fi

register_client
start_cleanup_watcher

cleanup() {
  release_client
}
trap cleanup EXIT INT TERM

export CALIBER_DATABASE_URL="$(normalize_postgres_driver_url "${CALIBER_DATABASE_URL:-sqlite:///$DB_FILE}")"
export MLFLOW_BACKEND_STORE_URI="$(normalize_postgres_driver_url "${MLFLOW_BACKEND_STORE_URI:-$CALIBER_DATABASE_URL}")"
export MINIO_ROOT_USER="${MINIO_ROOT_USER:-minioadmin}"
export MINIO_ROOT_PASSWORD="${MINIO_ROOT_PASSWORD:-minioadmin}"
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-$MINIO_ROOT_USER}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-$MINIO_ROOT_PASSWORD}"
export MLFLOW_S3_ENDPOINT_URL="${MLFLOW_S3_ENDPOINT_URL:-http://127.0.0.1:9000}"
export MLFLOW_ARTIFACT_ROOT="${MLFLOW_ARTIFACT_ROOT:-s3://mlflow/mlruns}"

export CALIBER_AUTH_MODE="${CALIBER_AUTH_MODE:-session}"
export CALIBER_AUTH_SESSION_COOKIE_SECURE="${CALIBER_AUTH_SESSION_COOKIE_SECURE:-false}"
export CALIBER_APPROVAL_ALLOW_SELF_APPROVAL="${CALIBER_APPROVAL_ALLOW_SELF_APPROVAL:-true}"
# A single process keeps the disposable SQLite stack within Playwright's
# startup budget and avoids loading one embedding runtime per MLflow worker.
export MLFLOW_WORKERS="${MLFLOW_WORKERS:-1}"
case "$MLFLOW_HOST" in
  127.0.0.1 | localhost | ::1)
    e2e_insecure_bootstrap_default=true
    ;;
  *)
    e2e_insecure_bootstrap_default=false
    ;;
esac
export CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT="${CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT:-$e2e_insecure_bootstrap_default}"
export CALIBER_DEV_USER="${CALIBER_DEV_USER:-admin}"
export CALIBER_ADMIN_USERS="${CALIBER_ADMIN_USERS:-${CALIBER_DEV_USER}}"
export CALIBER_APPROVER_USERS="${CALIBER_APPROVER_USERS:-${CALIBER_DEV_USER}}"
export CALIBER_OPERATOR_USERS="${CALIBER_OPERATOR_USERS:-${CALIBER_DEV_USER}}"
export CALIBER_WORKFLOW_RUN_QUEUE_ENABLED="${CALIBER_WORKFLOW_RUN_QUEUE_ENABLED:-true}"
export CALIBER_WORKFLOW_RUN_RUNTIME_APPROVALS_ENABLED="${CALIBER_WORKFLOW_RUN_RUNTIME_APPROVALS_ENABLED:-$CALIBER_WORKFLOW_RUN_QUEUE_ENABLED}"
export CALIBER_WORKFLOW_RUN_CHECKPOINTING_ENABLED="${CALIBER_WORKFLOW_RUN_CHECKPOINTING_ENABLED:-$CALIBER_WORKFLOW_RUN_QUEUE_ENABLED}"
export CALIBER_WORKFLOW_RUN_EVENT_BACKEND="${CALIBER_WORKFLOW_RUN_EVENT_BACKEND:-database}"
export CALIBER_KNOWLEDGE_GRAPH_EXTRACTOR_BACKEND="${CALIBER_KNOWLEDGE_GRAPH_EXTRACTOR_BACKEND:-heuristic}"
if [[ -z "${CALIBER_KNOWLEDGE_AGE_ENABLED:-}" ]]; then
  if [[ "$CALIBER_DATABASE_URL" == postgresql* ]]; then
    export CALIBER_KNOWLEDGE_AGE_ENABLED=true
  else
    export CALIBER_KNOWLEDGE_AGE_ENABLED=false
  fi
fi
export HF_HOME="${HF_HOME:-$DEFAULT_HF_HOME}"
export SENTENCE_TRANSFORMERS_HOME="${SENTENCE_TRANSFORMERS_HOME:-$HF_HOME}"

# Keep local as default, but expose MinIO/S3 as configured in the UI.
export CALIBER_WORKFLOW_STORAGE_BACKEND="${CALIBER_WORKFLOW_STORAGE_BACKEND:-local}"
export CALIBER_WORKFLOW_STORAGE_BASE_URI="${CALIBER_WORKFLOW_STORAGE_BASE_URI:-file://$PLAYWRIGHT_WORKSPACES}"
export CALIBER_WORKFLOW_STORAGE_BUCKET="${CALIBER_WORKFLOW_STORAGE_BUCKET:-caliber-workspaces}"
export CALIBER_WORKFLOW_STORAGE_INTERNAL_ENDPOINT_URL="${CALIBER_WORKFLOW_STORAGE_INTERNAL_ENDPOINT_URL:-http://127.0.0.1:9000}"
export CALIBER_WORKFLOW_STORAGE_PUBLIC_ENDPOINT_URL="${CALIBER_WORKFLOW_STORAGE_PUBLIC_ENDPOINT_URL:-http://127.0.0.1:9000}"
export CALIBER_WORKFLOW_STORAGE_REGION="${CALIBER_WORKFLOW_STORAGE_REGION:-us-east-1}"
export CALIBER_WORKFLOW_STORAGE_FORCE_PATH_STYLE="${CALIBER_WORKFLOW_STORAGE_FORCE_PATH_STYLE:-true}"
export CALIBER_WORKFLOW_STORAGE_ACCESS_KEY_SOURCE="${CALIBER_WORKFLOW_STORAGE_ACCESS_KEY_SOURCE:-MINIO_ROOT_USER}"
export CALIBER_WORKFLOW_STORAGE_SECRET_KEY_SOURCE="${CALIBER_WORKFLOW_STORAGE_SECRET_KEY_SOURCE:-MINIO_ROOT_PASSWORD}"
ensure_server_running

# Stay alive so Playwright owns a process handle for this shared bootstrap.
while true; do
  sleep 3600
done
