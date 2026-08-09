#!/usr/bin/env bash
# Bring up the whole CALIBER suite in containers: MinIO, Postgres, MLflow, the
# MLflow AI Gateway, and CALIBER. MLflow and CALIBER run as separate services
# (see deploy/README.md). Replaces the old native launcher.
set -euo pipefail

cd "$(dirname "$0")"


if ! command -v docker >/dev/null 2>&1; then
  echo "docker not found — install Docker Desktop, or run a single service natively (see the caliber/ README)." >&2
  exit 1
fi

ENV_ARGS=()
[[ -f deploy/.env ]] && ENV_ARGS+=(--env-file deploy/.env)
[[ -f .env ]] && ENV_ARGS+=(--env-file .env)

resolve_env_value() {
  local key="$1"
  local value="${!key:-}"
  local file=""
  local line=""
  if [[ -n "$value" ]]; then
    printf '%s' "$value"
    return 0
  fi
  for file in deploy/.env .env; do
    [[ -f "$file" ]] || continue
    line="$(sed -n "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*//p" "$file" | tail -n1)"
    if [[ -n "$line" ]]; then
      value="$line"
    fi
  done
  value="$(printf '%s' "$value" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")"
  printf '%s' "$value"
}

EVENT_BACKEND="$(resolve_env_value CALIBER_WORKFLOW_RUN_EVENT_BACKEND)"
if [[ -z "$EVENT_BACKEND" ]]; then
  EVENT_BACKEND="nats"
fi

BUS_SERVICE=""
BUS_PROFILE_ARGS=()
case "$EVENT_BACKEND" in
  redis)
    echo "CALIBER_WORKFLOW_RUN_EVENT_BACKEND=redis is no longer supported by deploy/. Use nats for the container stack, or run a custom external Redis-backed setup outside deploy/." >&2
    exit 1
    ;;
  nats)
    BUS_SERVICE="nats"
    BUS_PROFILE_ARGS=(--profile nats)
    ;;
esac

PROFILE_ARGS=("${BUS_PROFILE_ARGS[@]}" --profile app)
COMPOSE_BASE=(docker compose "${ENV_ARGS[@]}" -f deploy/compose.yaml)
COMPOSE=("${COMPOSE_BASE[@]}" "${PROFILE_ARGS[@]}")

echo "starting caliber-suite (containers)..."
# Ensure the selected shared event bus is up before the app tier starts.
if [[ -n "$BUS_SERVICE" ]]; then
  "${COMPOSE_BASE[@]}" "${BUS_PROFILE_ARGS[@]}" up -d "$BUS_SERVICE"
fi
# Default behavior favors a clean app start so UI/API changes aren't masked by
# stale app containers or reused old images.
#   CLEAN_START=1 (default): remove app containers before startup
#   BUILD=1 (default): rebuild app images on start
# Opt out for faster local iteration:
#   CLEAN_START=0 BUILD=0 ./start.sh
CLEAN_START="${CLEAN_START:-1}"
BUILD="${BUILD:-1}"

if [[ "$CLEAN_START" != "0" ]]; then
  "${COMPOSE[@]}" rm -fsv caliber mlflow mlflow-gateway >/dev/null 2>&1 || true
fi

port_is_listening() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1
    return $?
  fi
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | awk -v suffix=":${port}" '$4 ~ suffix "$" { found=1 } END { exit found ? 0 : 1 }'
    return $?
  fi
  # Docker Desktop on macOS provides lsof. If neither probe exists, leave
  # conflict detection to Compose instead of making the launcher unusable.
  return 1
}

managed_mlflow_owns_port() {
  local port="$1"
  local mapping=""
  [[ "$CLEAN_START" == "0" ]] || return 1
  mapping="$(docker port caliber-mlflow 5000/tcp 2>/dev/null || true)"
  [[ "$mapping" == *":${port}" ]]
}

CALIBER_PORT="$(resolve_env_value CALIBER_PORT)"
CALIBER_PORT="${CALIBER_PORT:-5001}"
GATEWAY_PORT="$(resolve_env_value MLFLOW_GATEWAY_PORT)"
GATEWAY_PORT="${GATEWAY_PORT:-5002}"
CONFIGURED_MLFLOW_PORT="$(resolve_env_value MLFLOW_PORT)"

if [[ -n "$CONFIGURED_MLFLOW_PORT" ]]; then
  if [[ "$CONFIGURED_MLFLOW_PORT" == "$CALIBER_PORT" ]]; then
    echo "MLFLOW_PORT=${CONFIGURED_MLFLOW_PORT} conflicts with CALIBER_PORT=${CALIBER_PORT}." >&2
    exit 1
  fi
  if [[ "$CONFIGURED_MLFLOW_PORT" == "$GATEWAY_PORT" ]]; then
    echo "MLFLOW_PORT=${CONFIGURED_MLFLOW_PORT} conflicts with MLFLOW_GATEWAY_PORT=${GATEWAY_PORT}." >&2
    exit 1
  fi
  if port_is_listening "$CONFIGURED_MLFLOW_PORT" && ! managed_mlflow_owns_port "$CONFIGURED_MLFLOW_PORT"; then
    echo "MLFLOW_PORT=${CONFIGURED_MLFLOW_PORT} is already in use. Set MLFLOW_PORT to a free host port or stop the process using it." >&2
    exit 1
  fi
  MLFLOW_HOST_PORT="$CONFIGURED_MLFLOW_PORT"
else
  MLFLOW_HOST_PORT=5000
  while [[ "$MLFLOW_HOST_PORT" == "$CALIBER_PORT" || "$MLFLOW_HOST_PORT" == "$GATEWAY_PORT" ]] || { port_is_listening "$MLFLOW_HOST_PORT" && ! managed_mlflow_owns_port "$MLFLOW_HOST_PORT"; }; do
    MLFLOW_HOST_PORT=$((MLFLOW_HOST_PORT + 1))
  done
  if [[ "$MLFLOW_HOST_PORT" != "5000" ]]; then
    echo "MLflow host port 5000 is busy; using ${MLFLOW_HOST_PORT} for this container launch. Set MLFLOW_PORT=${MLFLOW_HOST_PORT} to make it explicit."
  fi
fi

# Compose uses this value only for the host-side mapping (the container keeps
# serving MLflow on :5000, and CALIBER reaches it over the Compose network).
export MLFLOW_PORT="$MLFLOW_HOST_PORT"

# Ensure the Allure report dir exists (user-owned) before the caliber service's
# read-only bind mount, so `make allure-report` on the host can write into it
# (Docker would otherwise create the missing source dir as root on Linux).
mkdir -p caliber/caliber-ui/allure-report

# Branch instead of expanding a possibly-empty array — "${arr[@]}" under
# `set -u` is an "unbound variable" error on macOS's bash 3.2.
if [[ "$BUILD" != "0" ]]; then
  "${COMPOSE[@]}" up -d --build --force-recreate
else
  "${COMPOSE[@]}" up -d --force-recreate
fi

cat <<EOF

ready.
  CALIBER UI     : http://127.0.0.1:5001/caliber/
  MLflow UI      : http://127.0.0.1:${MLFLOW_HOST_PORT}
  LLM gateway    : http://127.0.0.1:5002/api/2.0/endpoints/   (MLflow AI Gateway)
  MinIO console  : http://127.0.0.1:9001   (login: minioadmin / minioadmin)
  Adminer UI     : http://127.0.0.1:8081/?pgsql=postgres&username=caliber&db=caliber   (password: caliber)
  Graph console  : http://127.0.0.1:8082   (AGE Viewer — auto-connects to graph knowledge_graph, no login)
  Event backend  : ${EVENT_BACKEND}

  logs   : docker compose ${ENV_ARGS[*]} -f deploy/compose.yaml ${PROFILE_ARGS[*]} logs -f
  status : docker compose ${ENV_ARGS[*]} -f deploy/compose.yaml ${PROFILE_ARGS[*]} ps

stop with: ./stop.sh
EOF
