#!/usr/bin/env bash
#
# Reversible PostgreSQL 16 -> 17 major-version migration for the CALIBER suite.
#
# Postgres major upgrades change the on-disk format, so the data volume cannot
# simply be re-tagged: it must be logically dumped and restored into a freshly
# initialized pg17 cluster. This script does that SAFELY:
#
#   1. Logical backups (roles + each DB) -> <suite root>/pg16-backup-<ts>/ on the host.
#   2. A block-level CLONE of the pg16 data volume -> <volume>_pg16_bak.
#      (instant revert path — the original bytes are kept untouched).
#   3. Brings the stack down and re-initializes a clean pg17 cluster.
#   4. Restores roles, the MLflow DB, and CALIBER's relational + pgvector data.
#   5. Verifies extensions + table counts + service health.
#
# THIS IS A DESTRUCTIVE, ONE-WAY-UNLESS-REVERTED OPERATION: step 3 stops the whole stack and deletes
# the pg16 data volume (after steps 1 and 2 have saved it). It therefore refuses to run until you
# confirm: answer the prompt by typing "migrate", or pass --yes when running non-interactively.
# Nothing is touched before that confirmation.
#
# NOTE on schemas: this stack runs Postgres with `search_path=ag_catalog,public`
# (so AGE's cypher() resolves unqualified). A consequence is that CALIBER's own
# Alembic-created `caliber_*` tables live in the `ag_catalog` schema, NOT public.
# Therefore the CALIBER dump must NOT exclude ag_catalog (that would drop every
# CALIBER table). We take a FULL-fidelity pg_dump of the caliber DB so the
# caliber_* tables AND any AGE graph data migrate together exactly as-is.
#
# Usage (from anywhere):
#   bash caliber/deploy/mcp/migrate-pg16-to-pg17.sh [--yes] [-h|--help]
#
# Needs: Docker with the Compose v2 plugin and a running daemon, and the stack's PostgreSQL
# container (caliber-mcp-postgres) running on PostgreSQL 16 (start it with ./start.sh first).
# Exit status: 0 migrated (or already on 17), 1 a failure or a missing prerequisite, 2 a usage error.
#
# Revert (if anything looks wrong — old data is fully intact). The script prints these commands
# with the real volume name when it finishes or fails; in the default project they are:
#   docker compose -f deploy/compose.yaml --profile app --profile nats down
#   docker volume rm caliber-infra_caliber_pg_data
#   docker volume create caliber-infra_caliber_pg_data
#   docker run --rm -v caliber-infra_caliber_pg_data_pg16_bak:/from -v caliber-infra_caliber_pg_data:/to \
#       alpine sh -c 'cp -a /from/. /to/'
#   git checkout -- caliber/deploy/mcp/Dockerfile.postgres caliber/deploy/mcp/docker-compose.yml
#   docker compose -f deploy/compose.yaml --profile app --profile nats up -d --build
#
set -euo pipefail

ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --yes|-y) ASSUME_YES=1 ;;
    -h|--help)
      awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *)
      echo "migrate-pg16-to-pg17: unknown argument: $arg (see --help)" >&2
      exit 2
      ;;
  esac
done

# --- Config -----------------------------------------------------------------
PG_CONTAINER="caliber-mcp-postgres"
PG_USER="caliber"
MLFLOW_DB="mlflow"
CALIBER_DB="caliber"

# Resolve repo paths relative to this script so it runs from anywhere.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUITE_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
COMPOSE_FILE="${SUITE_ROOT}/deploy/compose.yaml"
TS="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="${SUITE_ROOT}/pg16-backup-${TS}"

# Same env files as ./start.sh, so Compose resolves the same project, ports and volumes.
ENV_ARGS=()
[[ -f "${SUITE_ROOT}/deploy/.env" ]] && ENV_ARGS+=(--env-file "${SUITE_ROOT}/deploy/.env")
[[ -f "${SUITE_ROOT}/.env" ]] && ENV_ARGS+=(--env-file "${SUITE_ROOT}/.env")
# (An empty array expanded with "${arr[@]}" is an unbound-variable error under `set -u` on macOS's bash 3.2.)
compose() { docker compose ${ENV_ARGS[@]+"${ENV_ARGS[@]}"} -f "${COMPOSE_FILE}" "$@"; }
log()     { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die()     { echo "ERROR: $*" >&2; exit 1; }

# --- 0. Pre-flight (read-only) -----------------------------------------------
command -v docker >/dev/null 2>&1 \
  || die "docker was not found. Install Docker Desktop (macOS, Windows) or Docker Engine with the Compose plugin (Linux): https://docs.docker.com/get-docker/"
docker compose version >/dev/null 2>&1 \
  || die "'docker compose' (Compose v2) was not found. Install the Compose plugin: https://docs.docker.com/compose/install/"
docker info >/dev/null 2>&1 \
  || die "the Docker daemon is not reachable. Start Docker Desktop (or 'sudo systemctl start docker') and run this again."

log "Pre-flight: confirming the pg16 cluster is up"
if ! docker exec "${PG_CONTAINER}" pg_isready -U "${PG_USER}" -d "${CALIBER_DB}" >/dev/null 2>&1; then
  echo "ERROR: ${PG_CONTAINER} is not running/ready. Start the stack first:" >&2
  echo "  ./start.sh        (from the suite root)" >&2
  exit 1
fi
RUNNING_MAJOR="$(docker exec "${PG_CONTAINER}" psql -U "${PG_USER}" -d "${CALIBER_DB}" -tAc 'SHOW server_version_num;' | tr -d '[:space:]' | cut -c1-2)"
echo "Running Postgres major: ${RUNNING_MAJOR:-unknown}"
if [ "${RUNNING_MAJOR}" = "17" ]; then
  echo "Already on pg17 — nothing to migrate. Exiting."
  exit 0
fi
[ "${RUNNING_MAJOR}" = "16" ] \
  || die "this script migrates PostgreSQL 16 to 17, but ${PG_CONTAINER} reports major '${RUNNING_MAJOR:-unknown}'. Nothing was changed."

# The actual Docker volume name is the compose-PROJECT-prefixed form
# (e.g. caliber-infra_caliber_pg_data), NOT the bare `caliber_pg_data` from the
# volumes: block. Resolve it from the running container's mount so we never
# operate on the wrong (or an auto-created empty) volume.
PG_VOLUME="$(docker inspect "${PG_CONTAINER}" \
  --format '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}}{{end}}{{end}}' \
  2>/dev/null || true)"
[ -n "${PG_VOLUME}" ] \
  || die "could not determine the data volume of ${PG_CONTAINER} (is it using a named volume?). Nothing was changed."
PG_BACKUP_VOLUME="${PG_VOLUME}_pg16_bak"
if docker volume inspect "${PG_BACKUP_VOLUME}" >/dev/null 2>&1; then
  die "the backup volume ${PG_BACKUP_VOLUME} already exists (a previous migration?). Remove or rename it, then run again. Nothing was changed."
fi

revert_instructions() {
  cat <<EOF
Revert (the pg16 bytes are preserved in ${PG_BACKUP_VOLUME}):
  docker compose ${ENV_ARGS[*]:-} -f ${COMPOSE_FILE} --profile app --profile nats down
  docker volume rm ${PG_VOLUME}
  docker volume create ${PG_VOLUME}
  docker run --rm -v ${PG_BACKUP_VOLUME}:/from -v ${PG_VOLUME}:/to alpine sh -c 'cp -a /from/. /to/'
  git checkout -- caliber/deploy/mcp/Dockerfile.postgres caliber/deploy/mcp/docker-compose.yml
  docker compose ${ENV_ARGS[*]:-} -f ${COMPOSE_FILE} --profile app --profile nats up -d --build
EOF
}

# --- Confirmation -------------------------------------------------------------
cat <<EOF

About to migrate PostgreSQL 16 -> 17 for the CALIBER suite:
  * back up roles and databases to   ${BACKUP_DIR}
  * clone the data volume            ${PG_VOLUME}  ->  ${PG_BACKUP_VOLUME}
  * STOP the whole compose stack and DELETE the volume ${PG_VOLUME}
  * re-create it on PostgreSQL 17, restore the data, and start the stack again
The old data stays recoverable from the backups and the cloned volume.
EOF
if [ "${ASSUME_YES}" -ne 1 ]; then
  if [ ! -t 0 ]; then
    die "this is destructive and stdin is not a terminal: re-run with --yes to confirm. Nothing was changed."
  fi
  printf 'Type "migrate" to continue: '
  read -r answer
  [ "${answer}" = "migrate" ] || die "not confirmed. Nothing was changed."
fi

# From here on, a failure leaves the stack partly migrated: say how to get back.
STAGE=confirmed
on_exit() {
  local rc=$?
  if [ "${rc}" -eq 0 ] || [ "${STAGE}" = "confirmed" ]; then
    return 0
  fi
  echo >&2
  case "${STAGE}" in
    backup|clone|build)
      echo "ERROR: the migration stopped at stage '${STAGE}' (exit ${rc}) BEFORE anything was stopped or deleted: the stack and the pg16 data are untouched." >&2
      echo "       Before a re-run, delete the partial backups in ${BACKUP_DIR} and, if it was created, the clone volume ${PG_BACKUP_VOLUME}." >&2
      ;;
    *)
      echo "ERROR: the migration stopped at stage '${STAGE}' (exit ${rc}). Your pg16 data is safe in ${BACKUP_DIR} and ${PG_BACKUP_VOLUME}." >&2
      revert_instructions >&2
      ;;
  esac
}
trap on_exit EXIT

# --- 1. Logical backups (host) ----------------------------------------------
STAGE=backup
log "Dumping roles + databases to ${BACKUP_DIR}"
# The dumps hold role password hashes and all application data: owner-only.
umask 077
mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"
# Roles (globals), including their password hashes.
docker exec "${PG_CONTAINER}" pg_dumpall -U "${PG_USER}" --roles-only \
  > "${BACKUP_DIR}/roles.sql"
# Full belt-and-suspenders logical dump of EVERYTHING (raw safety net).
docker exec "${PG_CONTAINER}" pg_dumpall -U "${PG_USER}" \
  > "${BACKUP_DIR}/dumpall-full.sql"
# MLflow DB — pure relational, restores perfectly (custom format for speed).
docker exec "${PG_CONTAINER}" pg_dump -U "${PG_USER}" -Fc "${MLFLOW_DB}" \
  > "${BACKUP_DIR}/mlflow.dump"
# CALIBER DB — FULL fidelity (relational + pgvector + ag_catalog caliber_*
# tables + any AGE graph data). Do NOT exclude ag_catalog: CALIBER's tables live
# there because of the search_path (see the schema note in the header).
docker exec "${PG_CONTAINER}" pg_dump -U "${PG_USER}" -Fc \
  "${CALIBER_DB}" > "${BACKUP_DIR}/caliber.dump"
for f in roles.sql dumpall-full.sql mlflow.dump caliber.dump; do
  [ -s "${BACKUP_DIR}/${f}" ] || die "backup file ${BACKUP_DIR}/${f} is empty; aborting before anything is deleted."
done
echo "Backups written:"
ls -lh "${BACKUP_DIR}"

# --- 2. Clone the pg16 data volume (instant revert path) --------------------
STAGE=clone
log "Cloning ${PG_VOLUME} -> ${PG_BACKUP_VOLUME} (untouched pg16 bytes)"
docker volume create "${PG_BACKUP_VOLUME}" >/dev/null
docker run --rm \
  -v "${PG_VOLUME}:/from:ro" \
  -v "${PG_BACKUP_VOLUME}:/to" \
  alpine sh -c 'cp -a /from/. /to/'

# --- 3. Build the pg17 image, stop the stack, reset the data volume ---------
STAGE=build
log "Building the pg17 Postgres image (pgvector + Apache AGE PG17)"
# Built BEFORE anything is stopped or deleted, so a build failure changes nothing.
compose build postgres

STAGE=stop
log "Stopping the stack (volumes preserved)"
compose --profile app --profile nats down

STAGE=reset-volume
log "Removing the pg16 data volume so pg17 initializes a clean cluster"
# Safe: the logical backups and the ${PG_BACKUP_VOLUME} clone above hold every byte.
docker volume rm "${PG_VOLUME}"

# --- 4. Bring up pg17 + restore ---------------------------------------------
STAGE=restore
log "Starting pg17 (initdb creates the ${CALIBER_DB} DB + vector/age extensions)"
compose up -d postgres
echo "Waiting for pg17 to accept connections..."
for _ in $(seq 1 60); do
  if docker exec "${PG_CONTAINER}" pg_isready -U "${PG_USER}" -d "${CALIBER_DB}" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
docker exec "${PG_CONTAINER}" pg_isready -U "${PG_USER}" -d "${CALIBER_DB}"

log "Restoring roles"
docker exec -i "${PG_CONTAINER}" psql -U "${PG_USER}" -d postgres \
  < "${BACKUP_DIR}/roles.sql" || true   # roles may already exist from initdb

log "Restoring MLflow DB"
# MLflow's own entrypoint normally creates the empty `mlflow` DB; create it here
# so the restore is self-contained even if MLflow hasn't started yet.
docker exec "${PG_CONTAINER}" psql -U "${PG_USER}" -d postgres \
  -c "CREATE DATABASE ${MLFLOW_DB};" 2>/dev/null || true
docker exec -i "${PG_CONTAINER}" pg_restore -U "${PG_USER}" -d "${MLFLOW_DB}" \
  --no-owner --clean --if-exists < "${BACKUP_DIR}/mlflow.dump"

log "Restoring CALIBER DB (full fidelity: caliber_* in ag_catalog + AGE graph)"
# Drop & recreate the caliber DB that initdb just made, so the full dump (which
# carries its own CREATE EXTENSION + schema) restores into a clean target. The
# dump recreates age/vector itself; AGE extension-owned objects may emit benign
# "already exists" notices — those are expected, not failures.
docker exec "${PG_CONTAINER}" psql -U "${PG_USER}" -d postgres -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='${CALIBER_DB}' AND pid<>pg_backend_pid();" >/dev/null 2>&1 || true
docker exec "${PG_CONTAINER}" psql -U "${PG_USER}" -d postgres -c "DROP DATABASE IF EXISTS ${CALIBER_DB};"
docker exec "${PG_CONTAINER}" psql -U "${PG_USER}" -d postgres -c "CREATE DATABASE ${CALIBER_DB};"
docker exec -i "${PG_CONTAINER}" pg_restore -U "${PG_USER}" -d "${CALIBER_DB}" \
  --no-owner < "${BACKUP_DIR}/caliber.dump" \
  || echo "NOTE: pg_restore reported non-fatal notices (expected for AGE objects)."
CALIBER_TABLES="$(docker exec "${PG_CONTAINER}" psql -U "${PG_USER}" -d "${CALIBER_DB}" -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_name LIKE 'caliber_%';")"
echo "Restored caliber_* tables: ${CALIBER_TABLES}"
[ "${CALIBER_TABLES:-0}" -gt 0 ] || { echo "ERROR: no caliber_* tables restored — aborting before app start." >&2; exit 1; }

# --- 5. Verify --------------------------------------------------------------
STAGE=verify
log "Verifying pg17 cluster"
docker exec "${PG_CONTAINER}" psql -U "${PG_USER}" -d "${CALIBER_DB}" \
  -c "SHOW server_version;" \
  -c "SELECT extname, extversion FROM pg_extension ORDER BY extname;"
echo "MLflow table count:"
docker exec "${PG_CONTAINER}" psql -U "${PG_USER}" -d "${MLFLOW_DB}" -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';"

STAGE=app-tier
log "Bringing the rest of the app tier back up"
compose --profile app --profile nats up -d

STAGE=confirmed
log "Migration complete."
cat <<EOF

Next:
  * Verify the app at http://localhost:5001 — skills/tools/workflows should all
    be present (caliber_* tables live in ag_catalog and migrate in full).
  * Backups kept at: ${BACKUP_DIR}   (owner-only; they contain password hashes: delete them when done)
  * pg16 volume preserved as: ${PG_BACKUP_VOLUME}
  * Once you've confirmed everything works, reclaim space with:
      docker volume rm ${PG_BACKUP_VOLUME}

If anything looks wrong, to go back:
EOF
revert_instructions
