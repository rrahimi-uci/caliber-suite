"""persist run-bound grants, immutable except for revocation

`B03.2` (#664): a workflow run acts under a grant bound to the exact run and grant id, to a subject and workload
(`caliber.policy.principal`), a project, an explicit set of allowed actions and resources, a policy version and an
expiry. This migration adds only the storage and its guards; issuing and checking grants is
``caliber.policy.grants``.

* ``caliber_run_grants``: no fallback key (a run may have several grants; the caller names one).
* **Immutable but for revocation, in the database.** A BEFORE UPDATE trigger refuses any change except to
  ``revoked_at``/``revoked_by``/``revoke_reason``, and refuses every change once ``revoked_at`` is set, so a grant
  can be revoked once and never un-revoked or edited afterwards. A CHECK makes a revocation whole: ``revoked_at``,
  ``revoked_by`` and ``revoke_reason`` are all null or all set. A BEFORE DELETE trigger refuses deletion
  (PostgreSQL adds a BEFORE TRUNCATE trigger and revokes ``DELETE`` and ``TRUNCATE`` from ``PUBLIC``). The trigger
  functions are pinned to the migration's schema with ``pg_temp`` last in ``search_path``.

Downgrade drops the table and the functions, losing the grants (never runs).

Revision ID: 0115
Revises: 0114
Create Date: 2026-10-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0115"
down_revision: str | Sequence[str] | None = "0114"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "caliber_run_grants"
_FROZEN = (
    "grant_id",
    "workflow_run_id",
    "subject_user_id",
    "subject_auth_method",
    "workload_id",
    "workload_name",
    "workflow_version_digest",
    "project_id",
    "allowed_actions",
    "allowed_resources",
    "policy_version",
    "expires_at",
    "created_at",
)
_FROZEN_MESSAGE = "caliber_run_grants is immutable except for revocation"
_REVOKED_MESSAGE = "a revoked grant stays revoked"
_DELETE_MESSAGE = "caliber_run_grants is immutable: DELETE refused"


def _pg_functions(schema: str) -> tuple[str, ...]:
    pin = f'SET search_path = "{schema}", pg_temp'
    changed = " OR ".join(
        f"NEW.{c}::text IS DISTINCT FROM OLD.{c}::text" for c in _FROZEN
    )
    return (
        f"""
CREATE FUNCTION "{schema}".caliber_run_grants_frozen() RETURNS trigger LANGUAGE plpgsql {pin} AS $$
BEGIN
    IF OLD.revoked_at IS NOT NULL THEN
        RAISE EXCEPTION '{_REVOKED_MESSAGE}' USING ERRCODE = '23000';
    END IF;
    IF {changed} THEN
        RAISE EXCEPTION '{_FROZEN_MESSAGE}' USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$$""",
        f"""
CREATE FUNCTION "{schema}".caliber_run_grants_no_delete() RETURNS trigger LANGUAGE plpgsql {pin} AS $$
BEGIN
    RAISE EXCEPTION '{_DELETE_MESSAGE.replace("DELETE", "%")}', TG_OP USING ERRCODE = '23000';
END;
$$""",
    )


_PG_TRIGGERS = (
    f"CREATE TRIGGER trg_{_TABLE}_frozen BEFORE UPDATE ON {_TABLE} "
    "FOR EACH ROW EXECUTE FUNCTION caliber_run_grants_frozen()",
    f"CREATE TRIGGER trg_{_TABLE}_no_delete BEFORE DELETE ON {_TABLE} "
    "FOR EACH ROW EXECUTE FUNCTION caliber_run_grants_no_delete()",
    f"CREATE TRIGGER trg_{_TABLE}_no_truncate BEFORE TRUNCATE ON {_TABLE} "
    "FOR EACH STATEMENT EXECUTE FUNCTION caliber_run_grants_no_delete()",
)


def _sqlite_triggers() -> tuple[str, ...]:
    changed = " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in _FROZEN)
    return (
        f"CREATE TRIGGER trg_{_TABLE}_revoked BEFORE UPDATE ON {_TABLE} "
        f"WHEN OLD.revoked_at IS NOT NULL BEGIN SELECT RAISE(ABORT, '{_REVOKED_MESSAGE}'); END",
        f"CREATE TRIGGER trg_{_TABLE}_frozen BEFORE UPDATE ON {_TABLE} "
        f"WHEN {changed} BEGIN SELECT RAISE(ABORT, '{_FROZEN_MESSAGE}'); END",
        f"CREATE TRIGGER trg_{_TABLE}_no_delete BEFORE DELETE ON {_TABLE} "
        f"BEGIN SELECT RAISE(ABORT, '{_DELETE_MESSAGE}'); END",
    )


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("grant_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "workflow_run_id",
            sa.String(length=64),
            sa.ForeignKey("caliber_workflow_runs.workflow_run_id"),
            nullable=False,
        ),
        sa.Column("subject_user_id", sa.String(length=256), nullable=False),
        sa.Column("subject_auth_method", sa.String(length=32), nullable=False),
        sa.Column("workload_id", sa.String(length=80), nullable=False),
        sa.Column("workload_name", sa.String(length=256), nullable=False),
        sa.Column("workflow_version_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "project_id",
            sa.String(length=64),
            sa.ForeignKey("caliber_projects.project_id"),
            nullable=False,
        ),
        sa.Column("allowed_actions", sa.JSON(), nullable=False),
        sa.Column("allowed_resources", sa.JSON(), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_by", sa.String(length=256), nullable=True),
        sa.Column("revoke_reason", sa.Text(), nullable=True),
        sa.CheckConstraint("length(workload_id) = 67", name="ck_run_grant_workload_id"),
        sa.CheckConstraint("length(workflow_version_digest) = 64", name="ck_run_grant_digest"),
        sa.CheckConstraint(
            "(revoked_at IS NULL AND revoked_by IS NULL AND revoke_reason IS NULL) OR "
            "(revoked_at IS NOT NULL AND revoked_by IS NOT NULL AND revoke_reason IS NOT NULL)",
            name="ck_run_grant_revocation_whole",
        ),
    )
    op.create_index("ix_run_grants_run", _TABLE, ["workflow_run_id"])
    op.create_index("ix_run_grants_project", _TABLE, ["project_id"])

    if _is_postgresql():
        schema = op.get_bind().execute(sa.text("SELECT current_schema()")).scalar_one()
        for statement in (*_pg_functions(schema), *_PG_TRIGGERS):
            op.execute(sa.text(statement))
        op.execute(sa.text(f"REVOKE DELETE, TRUNCATE ON {_TABLE} FROM PUBLIC"))
    else:
        for statement in _sqlite_triggers():
            op.execute(sa.text(statement))


def downgrade() -> None:
    op.drop_table(_TABLE)  # drops the table's own triggers
    if _is_postgresql():
        for function in ("caliber_run_grants_frozen", "caliber_run_grants_no_delete"):
            op.execute(sa.text(f"DROP FUNCTION IF EXISTS {function}()"))
