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
  MLflow UI      : http://127.0.0.1:5000
  LLM gateway    : http://127.0.0.1:5002/api/2.0/endpoints/   (MLflow AI Gateway)
  MinIO console  : http://127.0.0.1:9001   (login: minioadmin / minioadmin)
  Adminer UI     : http://127.0.0.1:8081/?pgsql=postgres&username=caliber&db=caliber   (password: caliber)
  Graph console  : http://127.0.0.1:8082   (AGE Viewer — auto-connects to graph knowledge_graph, no login)
  Event backend  : ${EVENT_BACKEND}

  logs   : docker compose ${ENV_ARGS[*]} -f deploy/compose.yaml ${PROFILE_ARGS[*]} logs -f
  status : docker compose ${ENV_ARGS[*]} -f deploy/compose.yaml ${PROFILE_ARGS[*]} ps

stop with: ./stop.sh
EOF
