---
audience:
  - operator
doc_type: runbook
product_area: operations
stability: ga
prerequisites:
  - A CALIBER deployment or disaster-recovery review
reviewed_on: 2026-10-14
version_applicability: current main branch docs contract
tags:
  - backup
  - recovery
  - storage
  - rollback
---

# Backup and recovery

Use this page when you need to know what CALIBER state must be preserved, how to copy
each part, and where disaster recovery stops and asset-specific rollback begins.

The Beta restore smoke is now rehearsed offline against the deterministic downstream fixture.
It restores a coordinated CALIBER database artifact after an external mutation is committed
but before its receipt is recorded, preserves the admitted run's pins and artifact identifiers,
and settles the unresolved effect by authoritative lookup. It does not claim that a database-only
restore replaces restoring MLflow, object storage, workflow storage, or the secret-encryption key.

## At a glance

| State | Where it lives | Holds | Preserve it? |
| --- | --- | --- | --- |
| CALIBER database | `CALIBER_DATABASE_URL` | projects, members, accounts, jobs, approvals, release operations, the audit log, test sets, skills, workflows, knowledge-base metadata | yes |
| **MLflow backend store** | the `--backend-store-uri` you gave `mlflow server` | **prompt versions and their aliases**, experiments, runs and traces | **yes** |
| MLflow artifact root | the `--default-artifact-root` of your `mlflow server` | run and trace artifacts | yes |
| Object store | the S3 or MinIO buckets you use: any bucket you uploaded to, each knowledge base's source bucket, `CALIBER_LOG_BUCKET` and `CALIBER_WORKFLOW_RUN_ARTIFACT_BUCKET` (`CALIBER_OBJECT_STORE_*` sets only the connection) | uploads, knowledge-base sources and built indexes, log sink files, persisted run artifacts | yes, if you use them |
| Workflow storage | `CALIBER_WORKFLOW_STORAGE_*` (the bucket in `CALIBER_WORKFLOW_STORAGE_BUCKET` when the backend is `s3`) | run workspaces, uploads and file-backed workflow evidence | yes, if you use workflows |
| Secret encryption key | the source named by `CALIBER_SECRET_ENCRYPTION_KEY_SOURCE` | the key that decrypts stored secrets | yes, **separately** |
| Generated docs and UI assets | repository build output | nothing unique | no, rebuild |

The row to notice is the MLflow backend store. **Prompt versions and the `prod` alias live there, not
in the CALIBER database.** The CALIBER database holds the audit log and the release records that point
at those versions. A backup of the CALIBER database alone restores a history that names versions the
registry no longer has.

## 1. Back up the stores together

Take the CALIBER database and the MLflow backend store at the same point in time, so releases
recorded in one match the versions in the other. If they share one database or one SQLite file,
one backup covers both.

### PostgreSQL

```bash
# CALIBER database
pg_dump --format=custom --file=caliber.dump "$CALIBER_DATABASE_URL"
# MLflow backend store: the same URI you passed to `mlflow server --backend-store-uri`
# (skip if it is the same database as above)
pg_dump --format=custom --file=mlflow.dump "$MLFLOW_BACKEND_STORE_URI"
```

Use a libpq URL (`postgresql://user@host/db`); replace the `+psycopg` driver suffix if your URL has one.
Restore with `pg_restore --clean --if-exists --dbname="$TARGET_URL" caliber.dump`.

In the bundled Compose stack both stores are databases on the `caliber-mcp-postgres` container: `caliber`
for CALIBER and `mlflow` for MLflow (its `MLFLOW_BACKEND_URI`). For example:

```bash
docker exec caliber-mcp-postgres pg_dump -U caliber --format=custom caliber > caliber.dump
docker exec caliber-mcp-postgres pg_dump -U caliber --format=custom mlflow > mlflow.dump
```

### SQLite

```bash
# Online, consistent copy; safe while the server runs
sqlite3 caliber.db ".backup 'caliber.backup.db'"
sqlite3 mlflow.db  ".backup 'mlflow.backup.db'"
```

Copying the file with `cp` while the server is writing can produce a torn copy. Use `.backup`, or stop
the server first. Restore by putting the backup file back at the path in the URL.

### Files and buckets

```bash
# MLflow artifact root and workflow storage on local disk
rsync -a /path/to/mlflow-artifacts/ /backup/mlflow-artifacts/
rsync -a /path/to/workflow-storage/ /backup/workflow-storage/

# Object-store buckets (MinIO shown; use `aws s3 sync` for AWS S3). The bundled stack's
# buckets are `mlflow` (artifact root), `caliber-workspaces` (workflow storage) and `caliber-log`.
mc mirror --preserve local/caliber-workspaces /backup/caliber-workspaces
```

### The secret encryption key

Back up the key behind `CALIBER_SECRET_ENCRYPTION_KEY_SOURCE` somewhere other than the database backup.
Without it, restored secrets cannot be decrypted.

## 2. Distinguish disaster recovery from feature rollback

Restoring the deployment is not the same as rolling back a governed release. After a restore,
use the product's own controls: roll a prompt back from its version history, or use the recovery
console on the Releases page for a release that was left incomplete.

## 3. After a restore

Run `caliber-migrate` against the restored CALIBER database so its schema matches the installed
release (the bundled CALIBER container runs `alembic upgrade head` each time it starts), then verify:

- `GET /ajax-api/2.0/mlflow/caliber/readiness` reports `ready: true`, including the `schema` check
- the version history of a prompt you know (Prompts, then its Author stage) shows the versions and the
  live badge you expect
- the Releases page lists what was live, and the Audit Log shows the history
- on the Releases page, **Reconcile releases** reports nothing incomplete. A release that was in flight
  when the backup was taken can be left `applying` or `prepared`, and the console resolves it against the
  registry.

## 4. Related docs

- [Storage and state](../operate/storage-and-state.md)
- [Health and readiness](../operate/health-and-readiness.md)
- [Alpha scope and limitations](../start/alpha-scope.md)
- [Operations runbook](runbook.md)
- [The refinement loop](../architecture/refinement-loop.md)

## Restore smoke

Run the deterministic smoke from the repository root:

```bash
scripts/verify-restore.sh --python caliber/.venv/bin/python
```

The smoke creates an admitted run with a canonical run manifest containing workflow and policy
pins plus an exact artifact ID and checksum. A real downstream fixture commits one document request;
the dispatcher is stopped before the receipt is persisted, so CALIBER retains an unresolved
`dispatched` proposal while the downstream retains exactly one mutation. The SQLite database is
copied to a named backup artifact, the target is deliberately changed, and that exact artifact is
restored. The test then verifies the run pins, manifest bytes/digest, artifact ID, and unresolved
proposal are present again before a new owner performs the authoritative lookup.

The expected recovery result is one committed proposal, one verified receipt, and exactly one
downstream mutation. A missing lookup is not treated as proof that no external write occurred, and
the smoke never retries the effect. If the restored proposal is already committed, or the
downstream mutation count becomes two, stop the release investigation: the restore/reconciliation
boundary has been violated. This is an offline SQLite artifact smoke; the CI PostgreSQL migration
parity and EGI scenario jobs remain required for the supported reference profile.
