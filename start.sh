#!/usr/bin/env bash
# Bring up the whole CALIBER suite in containers: MinIO, Postgres, MLflow, the
# MLflow AI Gateway, and CALIBER. MLflow and CALIBER run as separate services
# (see deploy/README.md). Replaces the old native launcher.
#
# Usage:  ./start.sh [-h|--help]        (or: make start)
#
# Needs Docker with the Compose v2 plugin (2.20 or newer) and a running daemon.
# Optional config: copy .env.example to .env (app settings and secrets) and
# deploy/.env.example to deploy/.env (infra ports). Without them the documented
# defaults apply (simulated LLM providers, admin / admin on localhost).
#
# Environment:
#   CLEAN_START=1       (default) remove the app containers before starting, so UI/API changes
#                       are not masked by a stale container. Data volumes are kept.
#   BUILD=1             (default) rebuild the app images. CLEAN_START=0 BUILD=0 ./start.sh is the
#                       fast path for local iteration.
#   MLFLOW_PORT         host port for MLflow. Unset: 5000, or the next free port if 5000 is busy.
#   CALIBER_PORT        host port for CALIBER (default 5001).
#   MLFLOW_GATEWAY_PORT host port for the MLflow AI Gateway (default 5002).
#   START_TIMEOUT       seconds to wait for CALIBER to answer after the containers start (default 600).
#   CALIBER_WORKFLOW_RUN_EVENT_BACKEND   nats (default) or database / in_process; redis is not
#                       supported by deploy/. The values can also be set in .env or deploy/.env.
# Stop with ./stop.sh (keeps the data volumes).
set -euo pipefail

cd "$(dirname "$0")"

for arg in "$@"; do
  case "$arg" in
    -h|--help)
      awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
      exit 0
      ;;
    *)
      echo "start.sh: unknown argument: $arg (see ./start.sh --help)" >&2
      exit 2
      ;;
  esac
done

fail() { echo "start.sh: $*" >&2; exit 1; }

# ---- prerequisites: fail early, say what to install ---------------------------------------
command -v docker >/dev/null 2>&1 \
  || fail "docker was not found. Install Docker Desktop (macOS, Windows) or Docker Engine with the Compose plugin (Linux): https://docs.docker.com/get-docker/ . To run a single service natively instead, see caliber/README.md."
compose_version="$(docker compose version --short 2>/dev/null || true)"
[[ -n "$compose_version" ]] \
  || fail "'docker compose' (Compose v2) was not found. Install the Compose plugin: https://docs.docker.com/compose/install/ (the legacy 'docker-compose' is not supported)."
compose_version="${compose_version#v}"
compose_major="${compose_version%%.*}"
compose_minor="${compose_version#*.}"; compose_minor="${compose_minor%%.*}"
# deploy/compose.yaml uses `include:`, which arrived in Compose 2.20.
if [[ "$compose_major" =~ ^[0-9]+$ && "$compose_minor" =~ ^[0-9]+$ ]] \
   && { [[ "$compose_major" -lt 2 ]] || { [[ "$compose_major" -eq 2 ]] && [[ "$compose_minor" -lt 20 ]]; }; }; then
  fail "Docker Compose $compose_version is too old; deploy/compose.yaml needs 2.20 or newer. Update Docker Desktop or the Compose plugin."
fi
docker info >/dev/null 2>&1 \
  || fail "the Docker daemon is not reachable. Start Docker Desktop (or 'sudo systemctl start docker') and run ./start.sh again."

ENV_ARGS=()
[[ -f deploy/.env ]] && ENV_ARGS+=(--env-file deploy/.env)
[[ -f .env ]] && ENV_ARGS+=(--env-file .env)
if [[ ! -f .env ]]; then
  echo "note: no .env found; using the built-in defaults (simulated LLM providers). Copy .env.example to .env to configure real providers."
fi

# "${ENV_ARGS[@]}" on an EMPTY array is an "unbound variable" error under `set -u` on macOS's
# bash 3.2, so every expansion below goes through ${arr[@]+"${arr[@]}"}.
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
    fail "CALIBER_WORKFLOW_RUN_EVENT_BACKEND=redis is no longer supported by deploy/. Use nats for the container stack, or run a custom external Redis-backed setup outside deploy/."
    ;;
  nats)
    BUS_SERVICE="nats"
    BUS_PROFILE_ARGS=(--profile nats)
    ;;
esac

PROFILE_ARGS=(${BUS_PROFILE_ARGS[@]+"${BUS_PROFILE_ARGS[@]}"} --profile app)
COMPOSE_BASE=(docker compose ${ENV_ARGS[@]+"${ENV_ARGS[@]}"} -f deploy/compose.yaml)
COMPOSE=("${COMPOSE_BASE[@]}" "${PROFILE_ARGS[@]}")
ENV_DISPLAY=""
if [[ ${#ENV_ARGS[@]} -gt 0 ]]; then ENV_DISPLAY="${ENV_ARGS[*]} "; fi

echo "starting caliber-suite (containers)..."
# Ensure the selected shared event bus is up before the app tier starts.
if [[ -n "$BUS_SERVICE" ]]; then
  "${COMPOSE_BASE[@]}" ${BUS_PROFILE_ARGS[@]+"${BUS_PROFILE_ARGS[@]}"} up -d "$BUS_SERVICE"
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
  # Neither lsof nor ss: try a plain TCP connect to loopback (bash's /dev/tcp).
  (exec 3<>"/dev/tcp/127.0.0.1/${port}") 2>/dev/null
}

# True when the named container of this stack already publishes HOST_PORT for CONTAINER_PORT.
container_owns_port() { # container_owns_port CONTAINER CONTAINER_PORT HOST_PORT
  local mapping
  mapping="$(docker port "$1" "$2/tcp" 2>/dev/null || true)"
  [[ "$mapping" == *":$3" ]]
}

managed_mlflow_owns_port() {
  [[ "$CLEAN_START" == "0" ]] || return 1
  container_owns_port caliber-mlflow 5000 "$1"
}

CALIBER_PORT="$(resolve_env_value CALIBER_PORT)"
CALIBER_PORT="${CALIBER_PORT:-5001}"
GATEWAY_PORT="$(resolve_env_value MLFLOW_GATEWAY_PORT)"
GATEWAY_PORT="${GATEWAY_PORT:-5002}"
CONFIGURED_MLFLOW_PORT="$(resolve_env_value MLFLOW_PORT)"

if [[ -n "$CONFIGURED_MLFLOW_PORT" ]]; then
  if [[ "$CONFIGURED_MLFLOW_PORT" == "$CALIBER_PORT" ]]; then
    fail "MLFLOW_PORT=${CONFIGURED_MLFLOW_PORT} conflicts with CALIBER_PORT=${CALIBER_PORT}."
  fi
  if [[ "$CONFIGURED_MLFLOW_PORT" == "$GATEWAY_PORT" ]]; then
    fail "MLFLOW_PORT=${CONFIGURED_MLFLOW_PORT} conflicts with MLFLOW_GATEWAY_PORT=${GATEWAY_PORT}."
  fi
  if port_is_listening "$CONFIGURED_MLFLOW_PORT" && ! managed_mlflow_owns_port "$CONFIGURED_MLFLOW_PORT"; then
    fail "MLFLOW_PORT=${CONFIGURED_MLFLOW_PORT} is already in use. Set MLFLOW_PORT to a free host port or stop the process using it."
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

MINIO_API_PORT_RESOLVED="$(resolve_env_value MINIO_API_PORT)"; MINIO_API_PORT_RESOLVED="${MINIO_API_PORT_RESOLVED:-9000}"
MINIO_CONSOLE_PORT_RESOLVED="$(resolve_env_value MINIO_CONSOLE_PORT)"; MINIO_CONSOLE_PORT_RESOLVED="${MINIO_CONSOLE_PORT_RESOLVED:-9001}"
POSTGRES_PORT_RESOLVED="$(resolve_env_value POSTGRES_PORT)"; POSTGRES_PORT_RESOLVED="${POSTGRES_PORT_RESOLVED:-5432}"
ADMINER_PORT_RESOLVED="$(resolve_env_value ADMINER_PORT)"; ADMINER_PORT_RESOLVED="${ADMINER_PORT_RESOLVED:-8081}"
AGE_VIEWER_PORT_RESOLVED="$(resolve_env_value AGE_VIEWER_PORT)"; AGE_VIEWER_PORT_RESOLVED="${AGE_VIEWER_PORT_RESOLVED:-8082}"

# The other published ports are fixed by the environment. A port held by something that is not this
# stack (a local PostgreSQL on 5432, another service on 9000, ...) would otherwise fail deep inside
# `docker compose up`; name every such conflict, and the variable that moves it, before starting.
CONFLICTS=""
check_port() { # check_port LABEL ENV_VAR HOST_PORT CONTAINER CONTAINER_PORT
  if port_is_listening "$3" && ! container_owns_port "$4" "$5" "$3"; then
    CONFLICTS="${CONFLICTS}  - host port $3 ($1) is in use: free it, or set $2 to another port in .env / deploy/.env
"
  fi
}
check_port "MinIO S3 API" MINIO_API_PORT "$MINIO_API_PORT_RESOLVED" caliber-minio 9000
check_port "MinIO console" MINIO_CONSOLE_PORT "$MINIO_CONSOLE_PORT_RESOLVED" caliber-minio 9001
check_port "PostgreSQL" POSTGRES_PORT "$POSTGRES_PORT_RESOLVED" caliber-mcp-postgres 5432
check_port "Adminer" ADMINER_PORT "$ADMINER_PORT_RESOLVED" caliber-adminer 8080
check_port "AGE Viewer" AGE_VIEWER_PORT "$AGE_VIEWER_PORT_RESOLVED" caliber-age-viewer 3000
check_port "CALIBER" CALIBER_PORT "$CALIBER_PORT" caliber 5001
check_port "MLflow AI Gateway" MLFLOW_GATEWAY_PORT "$GATEWAY_PORT" caliber-mlflow-gateway 5002
if [[ -n "$BUS_SERVICE" ]]; then
  NATS_CLIENT_PORT_RESOLVED="$(resolve_env_value NATS_CLIENT_PORT)"; NATS_CLIENT_PORT_RESOLVED="${NATS_CLIENT_PORT_RESOLVED:-4222}"
  NATS_MONITOR_PORT_RESOLVED="$(resolve_env_value NATS_MONITOR_PORT)"; NATS_MONITOR_PORT_RESOLVED="${NATS_MONITOR_PORT_RESOLVED:-8222}"
  check_port "NATS client" NATS_CLIENT_PORT "$NATS_CLIENT_PORT_RESOLVED" caliber-nats 4222
  check_port "NATS monitor" NATS_MONITOR_PORT "$NATS_MONITOR_PORT_RESOLVED" caliber-nats 8222
fi
if [[ -n "$CONFLICTS" ]]; then
  printf 'start.sh: cannot start, these ports are already in use by something else:\n%s' "$CONFLICTS" >&2
  exit 1
fi

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

# `up -d` returns as soon as the containers are created. CALIBER then migrates its database, so
# wait until it answers instead of announcing "ready" while the first request would still be refused.
START_TIMEOUT="${START_TIMEOUT:-600}"
HEALTH_URL="http://127.0.0.1:${CALIBER_PORT}/ajax-api/2.0/mlflow/caliber/health"
if command -v curl >/dev/null 2>&1; then
  echo "waiting for CALIBER to answer at ${HEALTH_URL} (up to ${START_TIMEOUT}s)..."
  deadline=$((SECONDS + START_TIMEOUT))
  until curl -sf "$HEALTH_URL" >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
      echo "start.sh: CALIBER did not become healthy within ${START_TIMEOUT}s. Inspect the logs:" >&2
      echo "  docker compose ${ENV_DISPLAY}-f deploy/compose.yaml ${PROFILE_ARGS[*]} logs --tail 100 caliber" >&2
      exit 1
    fi
    sleep 3
  done
else
  echo "note: curl was not found, so start.sh cannot wait for CALIBER; give it a minute before opening the UI."
fi

ADMIN_USER="$(resolve_env_value CALIBER_AUTH_BOOTSTRAP_ADMIN_USER)"; ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASSWORD="$(resolve_env_value CALIBER_BOOTSTRAP_PASSWORD)"
if [[ -z "$ADMIN_PASSWORD" || "$ADMIN_PASSWORD" == "admin" ]]; then
  LOGIN_LINE="${ADMIN_USER} / admin   (first boot only; change it in Administration)"
else
  LOGIN_LINE="${ADMIN_USER} / the CALIBER_BOOTSTRAP_PASSWORD you configured   (first boot only)"
fi

cat <<EOF

ready.
  CALIBER UI     : http://127.0.0.1:${CALIBER_PORT}/caliber/
  MLflow UI      : http://127.0.0.1:${MLFLOW_HOST_PORT}
  LLM gateway    : http://127.0.0.1:${GATEWAY_PORT}/api/2.0/endpoints/   (MLflow AI Gateway)
  MinIO console  : http://127.0.0.1:${MINIO_CONSOLE_PORT_RESOLVED}   (login: minioadmin / minioadmin)
  Adminer UI     : http://127.0.0.1:${ADMINER_PORT_RESOLVED}/?pgsql=postgres&username=caliber&db=caliber   (password: caliber)
  Graph console  : http://127.0.0.1:${AGE_VIEWER_PORT_RESOLVED}   (AGE Viewer — auto-connects to graph knowledge_graph, no login)
  Event backend  : ${EVENT_BACKEND}
  CALIBER login  : ${LOGIN_LINE}

  logs   : docker compose ${ENV_DISPLAY}-f deploy/compose.yaml ${PROFILE_ARGS[*]} logs -f
  status : docker compose ${ENV_DISPLAY}-f deploy/compose.yaml ${PROFILE_ARGS[*]} ps

stop with: ./stop.sh
EOF
