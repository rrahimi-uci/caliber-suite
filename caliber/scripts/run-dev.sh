#!/usr/bin/env bash
# Launch MLflow with the CALIBER plugin enabled for local development.
#
# Loads ./.env (if present) without overwriting already-exported variables,
# applies sensible defaults for the caliber metadata DB + MLflow backend
# store, runs alembic to bring schemas current, then starts the server.
#
# Override anything by exporting before invoking, e.g.:
#   MLFLOW_PORT=5050 ./scripts/run-dev.sh
#   CALIBER_DATABASE_URL=postgresql://… ./scripts/run-dev.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VENV_DIR="${VENV_DIR:-.venv}"
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    echo "error: virtualenv not found at $VENV_DIR — run 'make install' first" >&2
    exit 1
fi

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

# Source .env without clobbering anything the caller already exported.
# The `set -a` block exports every variable assigned inside, and the
# conditional skips lines whose key is already set.
if [[ -f .env ]]; then
    set -a
    while IFS='=' read -r key value; do
        [[ -z "$key" || "$key" =~ ^# ]] && continue
        if [[ -z "${!key:-}" ]]; then
            # shellcheck disable=SC2086
            export $key="${value}"
        fi
    done < .env
    set +a
fi

# Local-dev defaults. Caliber and MLflow share the same SQLite file so a
# single transactional view spans assessments, traces, and caliber's
# audit log. MLflow artifacts default to the local MinIO bucket so new
# runs do not spill back into filesystem-backed `mlruns` / `mlartifacts`
# trees unless the operator explicitly overrides the artifact root.
export CALIBER_DATABASE_URL="$(normalize_postgres_driver_url "${CALIBER_DATABASE_URL:-sqlite:///./caliber.db}")"
export MLFLOW_BACKEND_STORE_URI="$(normalize_postgres_driver_url "${MLFLOW_BACKEND_STORE_URI:-$CALIBER_DATABASE_URL}")"
export MINIO_ROOT_USER="${MINIO_ROOT_USER:-minioadmin}"
export MINIO_ROOT_PASSWORD="${MINIO_ROOT_PASSWORD:-minioadmin}"
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-$MINIO_ROOT_USER}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-$MINIO_ROOT_PASSWORD}"
export MLFLOW_S3_ENDPOINT_URL="${MLFLOW_S3_ENDPOINT_URL:-http://127.0.0.1:9000}"
export MLFLOW_ARTIFACT_ROOT="${MLFLOW_ARTIFACT_ROOT:-s3://mlflow/mlruns}"
export MLFLOW_HOST="${MLFLOW_HOST:-127.0.0.1}"
export MLFLOW_PORT="${MLFLOW_PORT:-5000}"
export CALIBER_LOG_LEVEL="${CALIBER_LOG_LEVEL:-INFO}"
export CALIBER_AUTH_MODE="${CALIBER_AUTH_MODE:-session}"
export CALIBER_AUTH_BOOTSTRAP_ADMIN_USER="${CALIBER_AUTH_BOOTSTRAP_ADMIN_USER:-admin}"
case "$MLFLOW_HOST" in
  127.0.0.1 | localhost | ::1)
    local_insecure_bootstrap_default=true
    local_session_cookie_secure=false
    ;;
  *)
    local_insecure_bootstrap_default=false
    # A network-reachable bind must not default to a cookie the browser may send
    # over plaintext. Operators terminating TLS upstream can still use this host
    # bind; the Secure attribute remains the correct browser boundary.
    local_session_cookie_secure=true
    ;;
esac
export CALIBER_AUTH_SESSION_COOKIE_SECURE="${CALIBER_AUTH_SESSION_COOKIE_SECURE:-$local_session_cookie_secure}"
export CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT="${CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT:-$local_insecure_bootstrap_default}"
export CALIBER_DEV_USER="${CALIBER_DEV_USER:-admin}"
export CALIBER_ADMIN_USERS="${CALIBER_ADMIN_USERS:-${CALIBER_DEV_USER}}"
export CALIBER_APPROVER_USERS="${CALIBER_APPROVER_USERS:-${CALIBER_DEV_USER}}"
export CALIBER_OPERATOR_USERS="${CALIBER_OPERATOR_USERS:-${CALIBER_DEV_USER}}"
export CALIBER_WORKFLOW_RUN_QUEUE_ENABLED="${CALIBER_WORKFLOW_RUN_QUEUE_ENABLED:-true}"
export CALIBER_WORKFLOW_RUN_RUNTIME_APPROVALS_ENABLED="${CALIBER_WORKFLOW_RUN_RUNTIME_APPROVALS_ENABLED:-$CALIBER_WORKFLOW_RUN_QUEUE_ENABLED}"
export CALIBER_WORKFLOW_RUN_CHECKPOINTING_ENABLED="${CALIBER_WORKFLOW_RUN_CHECKPOINTING_ENABLED:-$CALIBER_WORKFLOW_RUN_QUEUE_ENABLED}"
export CALIBER_WORKFLOW_RUN_EVENT_BACKEND="${CALIBER_WORKFLOW_RUN_EVENT_BACKEND:-database}"
export CALIBER_KNOWLEDGE_GRAPH_EXTRACTOR_BACKEND="${CALIBER_KNOWLEDGE_GRAPH_EXTRACTOR_BACKEND:-spacy}"
export CALIBER_KNOWLEDGE_GRAPH_SPACY_MODEL="${CALIBER_KNOWLEDGE_GRAPH_SPACY_MODEL:-en_core_web_sm}"
export CALIBER_KNOWLEDGE_AGE_GRAPH_NAME="${CALIBER_KNOWLEDGE_AGE_GRAPH_NAME:-knowledge_graph}"

db_supports_age=0
if [[ "$CALIBER_DATABASE_URL" == postgresql* ]]; then
    db_supports_age=1
fi
if [[ -z "${CALIBER_KNOWLEDGE_AGE_ENABLED:-}" ]]; then
    if [[ "$db_supports_age" == "1" ]]; then
        export CALIBER_KNOWLEDGE_AGE_ENABLED=true
    else
        export CALIBER_KNOWLEDGE_AGE_ENABLED=false
    fi
fi

if [[ "$MLFLOW_ARTIFACT_ROOT" != s3://* ]]; then
    mkdir -p "${MLFLOW_ARTIFACT_ROOT#file://}"
fi

PY="$VENV_DIR/bin/python"
MLFLOW="$VENV_DIR/bin/mlflow"
ALEMBIC="$VENV_DIR/bin/alembic"

echo ">> caliber dev server"
echo "   database         : configured (URL hidden)"
echo "   artifact root    : configured (location hidden)"
if [[ "$MLFLOW_ARTIFACT_ROOT" == s3://* ]]; then
    echo "   s3 endpoint      : configured (URL hidden)"
fi
echo "   listen           : http://$MLFLOW_HOST:$MLFLOW_PORT"
echo "   caliber UI       : http://$MLFLOW_HOST:$MLFLOW_PORT/caliber/"
echo "   caliber API      : http://$MLFLOW_HOST:$MLFLOW_PORT/ajax-api/2.0/mlflow/caliber/"
echo "   dev identity     : $CALIBER_DEV_USER"
if [[ "${CALIBER_AUTH_BOOTSTRAP_ADMIN_USER}" == "admin" \
  && "${CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT}" == "true" \
  && -z "${CALIBER_AUTH_BOOTSTRAP_ADMIN_PASSWORD_ENV:-}" ]]; then
  echo "   initial login    : admin / admin (empty account table only; change immediately)"
else
  echo "   initial login    : configured strong bootstrap account (empty account table only)"
fi
echo "   workflow queue   : $CALIBER_WORKFLOW_RUN_QUEUE_ENABLED"
echo "   approvals        : $CALIBER_WORKFLOW_RUN_RUNTIME_APPROVALS_ENABLED"
echo "   checkpointing    : $CALIBER_WORKFLOW_RUN_CHECKPOINTING_ENABLED"
echo "   event backend    : $CALIBER_WORKFLOW_RUN_EVENT_BACKEND"
echo "   graph extractor  : $CALIBER_KNOWLEDGE_GRAPH_EXTRACTOR_BACKEND"
if [[ "$CALIBER_KNOWLEDGE_GRAPH_EXTRACTOR_BACKEND" == "spacy" ]]; then
    echo "   spacy model      : $CALIBER_KNOWLEDGE_GRAPH_SPACY_MODEL"
fi
if [[ "${CALIBER_KNOWLEDGE_AGE_ENABLED}" == "true" && "$db_supports_age" == "1" ]]; then
    echo "   apache age       : enabled"
    echo "   age graph        : $CALIBER_KNOWLEDGE_AGE_GRAPH_NAME"
elif [[ "${CALIBER_KNOWLEDGE_AGE_ENABLED}" == "true" ]]; then
    echo "   apache age       : requested, but the current database is not PostgreSQL"
else
    echo "   apache age       : disabled"
fi
echo

# Fail fast on config errors before alembic touches the DB.
"$PY" -c "from caliber.config import CaliberConfig; CaliberConfig.load()"

echo ">> checking runtime dependency advisories"
CALIBER_RUNTIME_ADVISORIES_WARN_ONLY=1 "$REPO_ROOT/scripts/check-runtime-advisories.sh"

echo ">> applying caliber migrations"
"$ALEMBIC" upgrade head

# Set SQLite to WAL mode while we still hold the DB exclusively.
# Once MLflow's tracking store opens its connection in the next step,
# any later attempt to change journal_mode silently no-ops because the
# pragma cannot run while other connections hold the DB. WAL is a
# persistent per-database setting, so doing it once here is enough.
if [[ "$CALIBER_DATABASE_URL" == sqlite* ]]; then
    DB_PATH="${CALIBER_DATABASE_URL#sqlite:///}"
    DB_PATH="${DB_PATH#/}"
    if command -v sqlite3 >/dev/null 2>&1; then
        echo ">> enabling SQLite WAL mode on /$DB_PATH"
        # `PRAGMA journal_mode=WAL;` returns the resulting mode on stdout.
        MODE="$(sqlite3 "/$DB_PATH" 'PRAGMA journal_mode=WAL;' 2>/dev/null || true)"
        echo "   journal_mode=$MODE"
    else
        echo "   (sqlite3 CLI missing; skipping WAL pragma — locking under load may degrade)"
    fi
fi

# Worker-process count for the MLflow server. Unset keeps MLflow's own default
# (4 gunicorn workers). Every worker process runs its OWN copy of all eight
# CALIBER background loops, which is safe for the queue consumers (they claim
# rows atomically) but collapses the worker-heartbeat table: worker_id is derived
# from id(self), a per-process memory address that collides across forks, so N
# processes register as one row and the rest log an IntegrityError warning.
# Set MLFLOW_WORKERS=1 for a single set of loops and an accurate Services page.
MLFLOW_WORKERS="${MLFLOW_WORKERS:-}"
if [[ -n "$MLFLOW_WORKERS" ]]; then
    WORKER_ARGS=(--workers "$MLFLOW_WORKERS")
    echo ">> starting mlflow server with --app-name caliber (--workers $MLFLOW_WORKERS)"
else
    WORKER_ARGS=()
    echo ">> starting mlflow server with --app-name caliber"
fi

# Auto-build UI if stale or missing (mirrored from caliber/start.sh).
UI_OUT="src/caliber/ui/index.html"
UI_TREE="caliber-ui"
if [ "${CALIBER_SKIP_UI_BUILD:-0}" = "1" ]; then
    echo ">> SPA build skipped (CALIBER_SKIP_UI_BUILD=1)"
elif ! command -v npm >/dev/null 2>&1 || ! command -v make >/dev/null 2>&1; then
    echo ">> npm/make not found — skipping SPA build (UI may be stale)"
else
    needs_build=0
    if [ ! -f "$UI_OUT" ]; then
        needs_build=1
    elif [ -n "$(find "$UI_TREE" -path '*/node_modules' -prune -o \
                -path '*/dist' -prune -o -type f -newer "$UI_OUT" -print -quit 2>/dev/null)" ]; then
        needs_build=1
    fi
    if [ "$needs_build" = "1" ]; then
        echo ">> building SPA bundle (missing or source newer than build)"
        make ui
    else
        echo ">> SPA bundle up to date"
    fi
fi

exec "$MLFLOW" server \
    --app-name caliber \
    --backend-store-uri "$MLFLOW_BACKEND_STORE_URI" \
    --default-artifact-root "$MLFLOW_ARTIFACT_ROOT" \
    --host "$MLFLOW_HOST" \
    --port "$MLFLOW_PORT" \
    "${WORKER_ARGS[@]}"
