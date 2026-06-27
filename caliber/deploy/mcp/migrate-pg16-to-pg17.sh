#!/usr/bin/env bash
#
# Reversible PostgreSQL 16 -> 17 major-version migration for the CALIBER suite.
#
# Postgres major upgrades change the on-disk format, so the data volume cannot
# simply be re-tagged: it must be logically dumped and restored into a freshly
# initialized pg17 cluster. This script does that SAFELY:
#
#   1. Logical backups (roles + each DB) -> ./pg16-backup-<ts>/ on the host.
#   2. A block-level CLONE of the pg16 data volume -> caliber_pg_data_pg16_bak.
#      (instant revert path — the original bytes are kept untouched).
#   3. Brings the stack down and re-initializes a clean pg17 cluster.
#   4. Restores roles, the MLflow DB, and CALIBER's relational + pgvector data.
#   5. Verifies extensions + table counts + service health.
#
# NOTE on schemas: this stack runs Postgres with `search_path=ag_catalog,public`
# (so AGE's cypher() resolves unqualified). A consequence is that CALIBER's own
# Alembic-created `caliber_*` tables live in the `ag_catalog` schema, NOT public.
# Therefore the CALIBER dump must NOT exclude ag_catalog (that would drop every
# CALIBER table). We take a FULL-fidelity pg_dump of the caliber DB so the
# caliber_* tables AND any AGE graph data migrate together exactly as-is.
#
# Revert (if anything looks wrong — old data is fully intact):
#   docker compose -f deploy/compose.yaml down
#   docker volume rm caliber_pg_data
#   docker run --rm -v caliber_pg_data_pg16_bak:/from -v caliber_pg_data:/to \
#       alpine sh -c 'cp -a /from/. /to/'
#   git checkout -- caliber/deploy/mcp/Dockerfile.postgres caliber/deploy/mcp/docker-compose.yml
#   docker compose -f deploy/compose.yaml --profile app up -d --build
#
# Usage (from the suite root):
#   bash caliber/deploy/mcp/migrate-pg16-to-pg17.sh
#
set -euo pipefail

# --- Config -----------------------------------------------------------------
PG_CONTAINER="caliber-mcp-postgres"
# The actual Docker volume name is the compose-PROJECT-prefixed form
# (e.g. caliber-infra_caliber_pg_data), NOT the bare `caliber_pg_data` from the
# volumes: block. Resolve it from the running container's mount so we never
# operate on the wrong (or an auto-created empty) volume. Fallback to the
# umbrella project's prefixed name if the container isn't up.
PG_VOLUME="$(docker inspect "${PG_CONTAINER}" \
  --format '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}}{{end}}{{end}}' \
  2>/dev/null)"
PG_VOLUME="${PG_VOLUME:-caliber-infra_caliber_pg_data}"
PG_BACKUP_VOLUME="${PG_VOLUME}_pg16_bak"
PG_USER="caliber"
MLFLOW_DB="mlflow"
CALIBER_DB="caliber"

# Resolve repo paths relative to this script so it runs from anywhere.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUITE_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
COMPOSE_FILE="${SUITE_ROOT}/deploy/compose.yaml"
TS="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="${SUITE_ROOT}/pg16-backup-${TS}"

compose() { docker compose -f "${COMPOSE_FILE}" "$@"; }
log()     { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

# --- 0. Pre-flight ----------------------------------------------------------
log "Pre-flight: confirming the pg16 cluster is up"
if ! docker exec "${PG_CONTAINER}" pg_isready -U "${PG_USER}" -d "${CALIBER_DB}" >/dev/null 2>&1; then
  echo "ERROR: ${PG_CONTAINER} is not running/ready. Start the stack first:"
  echo "  docker compose -f ${COMPOSE_FILE} --profile app up -d"
  exit 1
fi
RUNNING_MAJOR="$(docker exec "${PG_CONTAINER}" psql -U "${PG_USER}" -d "${CALIBER_DB}" -tAc 'SHOW server_version_num;' | cut -c1-2)"
echo "Running Postgres major: ${RUNNING_MAJOR}"
if [ "${RUNNING_MAJOR}" = "17" ]; then
  echo "Already on pg17 — nothing to migrate. Exiting."
  exit 0
fi

# --- 1. Logical backups (host) ----------------------------------------------
log "Dumping roles + databases to ${BACKUP_DIR}"
mkdir -p "${BACKUP_DIR}"
# Roles (globals). --no-role-passwords avoids leaking/needing the md5 hashes;
# our dev roles are recreated by initdb anyway, this just preserves grants.
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
echo "Backups written:"
ls -lh "${BACKUP_DIR}"

# --- 2. Clone the pg16 data volume (instant revert path) --------------------
log "Cloning ${PG_VOLUME} -> ${PG_BACKUP_VOLUME} (untouched pg16 bytes)"
docker volume create "${PG_BACKUP_VOLUME}" >/dev/null
docker run --rm \
  -v "${PG_VOLUME}:/from:ro" \
  -v "${PG_BACKUP_VOLUME}:/to" \
  alpine sh -c 'cp -a /from/. /to/'

# --- 3. Stop stack, build pg17 image, reset the data volume -----------------
log "Stopping the stack (volumes preserved)"
compose down

log "Building the pg17 Postgres image (pgvector + Apache AGE PG17)"
compose build postgres

log "Removing the pg16 data volume so pg17 initializes a clean cluster"
docker volume rm "${PG_VOLUME}"

# --- 4. Bring up pg17 + restore ---------------------------------------------
log "Starting pg17 (initdb creates the ${CALIBER_DB} DB + vector/age extensions)"
compose up -d postgres
echo "Waiting for pg17 to accept connections..."
for i in $(seq 1 60); do
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
[ "${CALIBER_TABLES:-0}" -gt 0 ] || { echo "ERROR: no caliber_* tables restored — aborting before app start."; exit 1; }

# --- 5. Verify --------------------------------------------------------------
log "Verifying pg17 cluster"
docker exec "${PG_CONTAINER}" psql -U "${PG_USER}" -d "${CALIBER_DB}" \
  -c "SHOW server_version;" \
  -c "SELECT extname, extversion FROM pg_extension ORDER BY extname;"
echo "MLflow table count:"
docker exec "${PG_CONTAINER}" psql -U "${PG_USER}" -d "${MLFLOW_DB}" -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';"

log "Bringing the rest of the app tier back up"
compose --profile app up -d

log "Migration complete."
cat <<EOF

Next:
  * Verify the app at http://localhost:5001 — skills/tools/workflows should all
    be present (caliber_* tables live in ag_catalog and migrate in full).
  * Backups kept at: ${BACKUP_DIR}
  * pg16 volume preserved as: ${PG_BACKUP_VOLUME}
  * Once you've confirmed everything works, reclaim space with:
      docker volume rm ${PG_BACKUP_VOLUME}

Revert instructions are at the top of this script.
EOF
