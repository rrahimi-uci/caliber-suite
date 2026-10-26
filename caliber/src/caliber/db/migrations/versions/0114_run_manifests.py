"""persist immutable run manifests (one row per run) and mark pre-manifest runs as legacy

`B02.1a` (#656, ADR 0003): a workflow run is pinned, at admission, to the exact release, prompt/tool/skill/
knowledge/model/policy versions and rule bundle it will use, and to who started it. The record is written once.
This migration only adds the *storage* and its guards; building and writing the manifest is B02.1b/B02.2a
(#676, #680).

* ``caliber_run_manifests``: ``manifest_bytes`` (the canonical JSON) and ``manifest_digest`` (its lowercase
  SHA-256), unique per ``workflow_run_id``. On PostgreSQL a CHECK also pins the digest to ``sha256(bytes)``.
* **Immutability, in the database.** A BEFORE UPDATE/DELETE trigger refuses the change (SQLite and PostgreSQL;
  PostgreSQL adds a BEFORE TRUNCATE trigger), so a bulk SQL statement is refused as well as an ORM one. On
  PostgreSQL UPDATE, DELETE and TRUNCATE are also revoked from PUBLIC; a deployment grants its runtime role
  only SELECT and INSERT (docs/operate/reference-profile.md), which is the layer a trigger-disabling owner or a
  ``session_replication_role = replica`` session cannot be trusted to respect.
* **Legacy runs are marked, not given manifests.** ``caliber_workflow_runs.manifest_legacy`` defaults to
  ``true`` (a metadata-only default on PostgreSQL 11+ and SQLite, so existing rows are marked legacy without
  rewriting the table). A run created by admission (#680) sets it ``false`` in the same transaction as its manifest;
  a run created by any other path stays legacy and is never given pins it never recorded. A trigger refuses a
  manifest for a legacy or unknown run, and another makes the flag write-once, so no historical pin can be
  fabricated afterwards.
* **Trigger functions are pinned** to the migration's schema with ``pg_temp`` last in ``search_path``, so a role
  cannot shadow ``caliber_workflow_runs`` with a temporary table to slip a manifest past the legacy check.

Downgrade drops the table, the triggers and the function, and the column. It is lossless for runs and loses only
manifests (a downgrade past the release that writes them discards what they recorded).

Revision ID: 0114
Revises: 0113
Create Date: 2026-10-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0114"
down_revision: str | Sequence[str] | None = "0113"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MANIFESTS = "caliber_run_manifests"
_RUNS = "caliber_workflow_runs"
_IMMUTABLE_MESSAGE = "caliber_run_manifests is immutable"
_LEGACY_MESSAGE = "a manifest needs an existing, non-legacy run"
_FLAG_MESSAGE = "caliber_workflow_runs.manifest_legacy is write-once"

# PostgreSQL: SQLSTATE 23000 (integrity_constraint_violation) so SQLAlchemy raises IntegrityError, as SQLite's
# RAISE(ABORT, ...) does.
def _pg_functions(schema: str) -> tuple[str, ...]:
    """The PostgreSQL trigger functions, pinned to ``schema`` (``pg_temp`` last, so a temp table cannot shadow)."""
    pin = f'SET search_path = "{schema}", pg_temp'
    return (
        f"""
CREATE FUNCTION "{schema}".caliber_run_manifests_immutable() RETURNS trigger LANGUAGE plpgsql {pin} AS $$
BEGIN
    RAISE EXCEPTION '{_IMMUTABLE_MESSAGE}: % refused', TG_OP USING ERRCODE = '23000';
END;
$$""",
        f"""
CREATE FUNCTION "{schema}".caliber_run_manifests_not_legacy() RETURNS trigger LANGUAGE plpgsql {pin} AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM "{schema}".{_RUNS}
        WHERE workflow_run_id = NEW.workflow_run_id AND NOT manifest_legacy
    ) THEN
        RAISE EXCEPTION '{_LEGACY_MESSAGE}' USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$$""",
        f"""
CREATE FUNCTION "{schema}".caliber_runs_legacy_flag_write_once() RETURNS trigger LANGUAGE plpgsql {pin} AS $$
BEGIN
    IF NEW.manifest_legacy IS DISTINCT FROM OLD.manifest_legacy THEN
        RAISE EXCEPTION '{_FLAG_MESSAGE}' USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$$""",
    )


_PG_TRIGGERS = (
    f"CREATE TRIGGER trg_{_MANIFESTS}_immutable BEFORE UPDATE OR DELETE ON {_MANIFESTS} "
    "FOR EACH ROW EXECUTE FUNCTION caliber_run_manifests_immutable()",
    f"CREATE TRIGGER trg_{_MANIFESTS}_no_truncate BEFORE TRUNCATE ON {_MANIFESTS} "
    "FOR EACH STATEMENT EXECUTE FUNCTION caliber_run_manifests_immutable()",
    f"CREATE TRIGGER trg_{_MANIFESTS}_not_legacy BEFORE INSERT ON {_MANIFESTS} "
    "FOR EACH ROW EXECUTE FUNCTION caliber_run_manifests_not_legacy()",
    f"CREATE TRIGGER trg_{_RUNS}_legacy_flag BEFORE UPDATE OF manifest_legacy ON {_RUNS} "
    "FOR EACH ROW EXECUTE FUNCTION caliber_runs_legacy_flag_write_once()",
)

_SQLITE_TRIGGERS = (
    f"CREATE TRIGGER trg_{_MANIFESTS}_no_update BEFORE UPDATE ON {_MANIFESTS} "
    f"BEGIN SELECT RAISE(ABORT, '{_IMMUTABLE_MESSAGE}: UPDATE refused'); END",
    f"CREATE TRIGGER trg_{_MANIFESTS}_no_delete BEFORE DELETE ON {_MANIFESTS} "
    f"BEGIN SELECT RAISE(ABORT, '{_IMMUTABLE_MESSAGE}: DELETE refused'); END",
    f"CREATE TRIGGER trg_{_MANIFESTS}_not_legacy BEFORE INSERT ON {_MANIFESTS} "
    f"WHEN NOT EXISTS (SELECT 1 FROM {_RUNS} WHERE workflow_run_id = NEW.workflow_run_id "
    f"AND manifest_legacy = 0) BEGIN SELECT RAISE(ABORT, '{_LEGACY_MESSAGE}'); END",
    f"CREATE TRIGGER trg_{_RUNS}_legacy_flag BEFORE UPDATE OF manifest_legacy ON {_RUNS} "
    "WHEN OLD.manifest_legacy IS NOT NEW.manifest_legacy "
    f"BEGIN SELECT RAISE(ABORT, '{_FLAG_MESSAGE}'); END",
)


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # The default marks every existing run legacy; no backfill UPDATE, so the runs table is not rewritten.
    op.add_column(
        _RUNS,
        sa.Column("manifest_legacy", sa.Boolean(), nullable=False, server_default=sa.true()),
    )

    op.create_table(
        _MANIFESTS,
        sa.Column("manifest_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "workflow_run_id",
            sa.String(length=64),
            sa.ForeignKey(f"{_RUNS}.workflow_run_id"),
            nullable=False,
        ),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("manifest_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("manifest_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("workflow_run_id", name="uq_run_manifest_run"),
        sa.CheckConstraint("length(manifest_digest) = 64", name="ck_run_manifest_digest_length"),
        sa.CheckConstraint("length(manifest_bytes) > 0", name="ck_run_manifest_bytes_present"),
    )

    if _is_postgresql():
        op.execute(
            sa.text(
                f"ALTER TABLE {_MANIFESTS} ADD CONSTRAINT ck_run_manifest_digest_is_sha256 "
                "CHECK (manifest_digest = encode(sha256(manifest_bytes), 'hex'))"
            )
        )
        schema = op.get_bind().execute(sa.text("SELECT current_schema()")).scalar_one()
        for statement in (*_pg_functions(schema), *_PG_TRIGGERS):
            op.execute(sa.text(statement))
        op.execute(sa.text(f"REVOKE UPDATE, DELETE, TRUNCATE ON {_MANIFESTS} FROM PUBLIC"))
    else:
        for statement in _SQLITE_TRIGGERS:
            op.execute(sa.text(statement))


def downgrade() -> None:
    if _is_postgresql():
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{_RUNS}_legacy_flag ON {_RUNS}"))
        op.drop_table(_MANIFESTS)  # drops the table's own triggers
        for function in (
            "caliber_runs_legacy_flag_write_once",
            "caliber_run_manifests_not_legacy",
            "caliber_run_manifests_immutable",
        ):
            op.execute(sa.text(f"DROP FUNCTION IF EXISTS {function}()"))
    else:
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{_RUNS}_legacy_flag"))
        op.drop_table(_MANIFESTS)
    with op.batch_alter_table(_RUNS) as batch_op:
        batch_op.drop_column("manifest_legacy")
