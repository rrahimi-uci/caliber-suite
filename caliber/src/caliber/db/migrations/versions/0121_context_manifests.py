"""persist immutable context manifests

`B07.3a` (#704, ADR 0003): the ordered, labelled context one agent step was given is recorded once, before the provider
sees any of it. ``caliber_context_manifests`` holds the canonical bytes and their lowercase SHA-256 (on PostgreSQL a CHECK
pins the digest to ``sha256(bytes)``), unique per ``(workflow_run_id, step_request_id, revision)``: a re-assembly is a new
revision. A BEFORE UPDATE/DELETE trigger refuses any change (SQLite and PostgreSQL; PostgreSQL adds a BEFORE TRUNCATE
trigger and revokes ``UPDATE``, ``DELETE`` and ``TRUNCATE`` from ``PUBLIC``; the trigger function is pinned to the
migration's schema with ``pg_temp`` last in ``search_path``). Downgrade drops the table and the function and loses the
manifests (it never runs where they matter).

Revision ID: 0121
Revises: 0120
Create Date: 2026-10-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0121"
down_revision: str | Sequence[str] | None = "0120"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "caliber_context_manifests"
_MESSAGE = "caliber_context_manifests is immutable"


def _pg_function(schema: str) -> str:
    return f"""
CREATE FUNCTION "{schema}".caliber_context_manifests_immutable() RETURNS trigger LANGUAGE plpgsql
    SET search_path = "{schema}", pg_temp AS $$
BEGIN
    RAISE EXCEPTION '{_MESSAGE}: % refused', TG_OP USING ERRCODE = '23000';
END;
$$"""


_PG_TRIGGERS = (
    f"CREATE TRIGGER trg_{_TABLE}_immutable BEFORE UPDATE OR DELETE ON {_TABLE} "
    "FOR EACH ROW EXECUTE FUNCTION caliber_context_manifests_immutable()",
    f"CREATE TRIGGER trg_{_TABLE}_no_truncate BEFORE TRUNCATE ON {_TABLE} "
    "FOR EACH STATEMENT EXECUTE FUNCTION caliber_context_manifests_immutable()",
)
_SQLITE_TRIGGERS = (
    f"CREATE TRIGGER trg_{_TABLE}_no_update BEFORE UPDATE ON {_TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{_MESSAGE}: UPDATE refused'); END",
    f"CREATE TRIGGER trg_{_TABLE}_no_delete BEFORE DELETE ON {_TABLE} "
    f"BEGIN SELECT RAISE(ABORT, '{_MESSAGE}: DELETE refused'); END",
)


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("context_manifest_id", sa.String(length=64), primary_key=True),
        sa.Column("workflow_run_id", sa.String(length=64), nullable=False),
        sa.Column("step_request_id", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False, server_default="v1"),
        sa.Column("manifest_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("manifest_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["workflow_run_id"], ["caliber_workflow_runs.workflow_run_id"]),
        sa.UniqueConstraint(
            "workflow_run_id", "step_request_id", "revision", name="uq_context_manifest_step_revision"
        ),
        sa.CheckConstraint("length(manifest_digest) = 64", name="ck_context_manifest_digest_length"),
        sa.CheckConstraint("length(manifest_bytes) > 0", name="ck_context_manifest_bytes_present"),
        sa.CheckConstraint("revision >= 1", name="ck_context_manifest_revision"),
    )
    if _is_postgresql():
        schema = op.get_bind().execute(sa.text("SELECT current_schema()")).scalar_one()
        op.execute(
            sa.text(
                f"ALTER TABLE {_TABLE} ADD CONSTRAINT ck_context_manifest_digest_is_sha256 "
                "CHECK (manifest_digest = encode(sha256(manifest_bytes), 'hex'))"
            )
        )
        for statement in (_pg_function(schema), *_PG_TRIGGERS):
            op.execute(sa.text(statement))
        op.execute(sa.text(f"REVOKE UPDATE, DELETE, TRUNCATE ON {_TABLE} FROM PUBLIC"))
    else:
        for statement in _SQLITE_TRIGGERS:
            op.execute(sa.text(statement))


def downgrade() -> None:
    op.drop_table(_TABLE)  # drops the table's own triggers
    if _is_postgresql():
        op.execute(sa.text("DROP FUNCTION IF EXISTS caliber_context_manifests_immutable()"))
