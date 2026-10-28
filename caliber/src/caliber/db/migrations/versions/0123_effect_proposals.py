"""persist effect proposals with an immutable intent and a durable occurrence

`B05.1` (#700, ADR 0003): ``caliber_effect_proposals`` holds one row per logical effect, unique per
``(tenant_id, workflow_run_id, node_id, occurrence_id)`` (and per ``logical_key``, the SHA-256 of those four). The
*intent* (connector identity and version, effect class, target resource, arguments and the expected resource version) is
stored as canonical JSON (``intent_bytes``) with its algorithm version and its ``sha256:`` digest (on PostgreSQL a CHECK
pins the digest to the bytes), and is immutable: a BEFORE UPDATE trigger refuses a change to any column but ``state``
(SQLite and PostgreSQL), a BEFORE DELETE trigger refuses deletion, and PostgreSQL adds a BEFORE TRUNCATE trigger (the
trigger functions are pinned to the migration's schema with ``pg_temp`` last in ``search_path``). On SQLite (the development
dialect) the triggers refuse UPDATE and DELETE statements, but ``INSERT OR REPLACE`` and unenforced foreign keys can still be
used from raw SQL; the ORM guard covers the supported paths. ``state`` is the only
column that may change; the transition rules arrive with B05.2 (#705). Nothing writes the table until a dispatcher does.
Downgrade drops the table and the functions and loses the proposals (it never runs where they matter).

Revision ID: 0123
Revises: 0122
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0123"
down_revision: str | Sequence[str] | None = "0122"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "caliber_effect_proposals"
_FROZEN = (
    "proposal_id",
    "tenant_id",
    "workflow_run_id",
    "node_id",
    "occurrence_id",
    "logical_key",
    "connector",
    "connector_version",
    "effect_class",
    "resource",
    "args",
    "expected_resource_version",
    "intent_algorithm",
    "intent_bytes",
    "intent_hash",
    "created_at",
)
_FROZEN_MESSAGE = "the intent of caliber_effect_proposals is immutable; only state may change"
_DELETE_MESSAGE = "caliber_effect_proposals is immutable: DELETE refused"
_EFFECT_CLASSES = "'read', 'reversible_write', 'irreversible_write', 'export'"
_STATES = "'proposed', 'approved', 'dispatched', 'committed', 'failed', 'unknown', 'reconciled'"


def _pg_functions(schema: str) -> tuple[str, ...]:
    pin = f'SET search_path = "{schema}", pg_temp'
    changed = " OR ".join(f"NEW.{c}::text IS DISTINCT FROM OLD.{c}::text" for c in _FROZEN)
    return (
        f"""
CREATE FUNCTION "{schema}".caliber_effect_proposals_frozen() RETURNS trigger LANGUAGE plpgsql {pin} AS $$
BEGIN
    IF {changed} THEN
        RAISE EXCEPTION '{_FROZEN_MESSAGE}' USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$$""",
        f"""
CREATE FUNCTION "{schema}".caliber_effect_proposals_no_delete() RETURNS trigger LANGUAGE plpgsql {pin} AS $$
BEGIN
    RAISE EXCEPTION '{_DELETE_MESSAGE.replace("DELETE", "%")}', TG_OP USING ERRCODE = '23000';
END;
$$""",
    )


_PG_TRIGGERS = (
    f"CREATE TRIGGER trg_{_TABLE}_frozen BEFORE UPDATE ON {_TABLE} "
    "FOR EACH ROW EXECUTE FUNCTION caliber_effect_proposals_frozen()",
    f"CREATE TRIGGER trg_{_TABLE}_no_delete BEFORE DELETE ON {_TABLE} "
    "FOR EACH ROW EXECUTE FUNCTION caliber_effect_proposals_no_delete()",
    f"CREATE TRIGGER trg_{_TABLE}_no_truncate BEFORE TRUNCATE ON {_TABLE} "
    "FOR EACH STATEMENT EXECUTE FUNCTION caliber_effect_proposals_no_delete()",
)


def _sqlite_triggers() -> tuple[str, ...]:
    changed = " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in _FROZEN)
    return (
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
        sa.Column("proposal_id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column(
            "workflow_run_id",
            sa.String(length=64),
            sa.ForeignKey("caliber_workflow_runs.workflow_run_id"),
            nullable=False,
        ),
        sa.Column("node_id", sa.String(length=128), nullable=False),
        sa.Column("occurrence_id", sa.String(length=128), nullable=False),
        sa.Column("logical_key", sa.String(length=64), nullable=False),
        sa.Column("connector", sa.String(length=128), nullable=False),
        sa.Column("connector_version", sa.String(length=64), nullable=False),
        sa.Column("effect_class", sa.String(length=32), nullable=False),
        sa.Column("resource", sa.String(length=512), nullable=False),
        sa.Column("args", sa.JSON(), nullable=False),
        sa.Column("expected_resource_version", sa.String(length=256), nullable=True),
        sa.Column("intent_algorithm", sa.String(length=16), nullable=False),
        sa.Column("intent_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("intent_hash", sa.String(length=71), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="proposed"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "tenant_id",
            "workflow_run_id",
            "node_id",
            "occurrence_id",
            name="uq_effect_proposal_occurrence",
        ),
        sa.UniqueConstraint("logical_key", name="uq_effect_proposal_logical_key"),
        sa.CheckConstraint(f"effect_class IN ({_EFFECT_CLASSES})", name="ck_effect_proposal_class"),
        sa.CheckConstraint(f"state IN ({_STATES})", name="ck_effect_proposal_state"),
        sa.CheckConstraint("length(logical_key) = 64", name="ck_effect_proposal_key_length"),
        sa.CheckConstraint("length(intent_hash) = 71", name="ck_effect_proposal_hash_length"),
        sa.CheckConstraint("length(intent_bytes) > 0", name="ck_effect_proposal_intent_present"),
        sa.CheckConstraint(
            "length(node_id) > 0 AND length(occurrence_id) > 0 AND length(connector) > 0 "
            "AND length(connector_version) > 0 AND length(resource) > 0",
            name="ck_effect_proposal_identity_present",
        ),
    )
    op.create_index("ix_effect_proposals_run", _TABLE, ["workflow_run_id"])
    if _is_postgresql():
        schema = op.get_bind().execute(sa.text("SELECT current_schema()")).scalar_one()
        op.execute(
            sa.text(
                f"ALTER TABLE {_TABLE} ADD CONSTRAINT ck_effect_proposal_hash_is_sha256 "
                "CHECK (intent_hash = 'sha256:' || encode(sha256(intent_bytes), 'hex'))"
            )
        )
        for statement in (*_pg_functions(schema), *_PG_TRIGGERS):
            op.execute(sa.text(statement))
        op.execute(sa.text(f"REVOKE DELETE, TRUNCATE ON {_TABLE} FROM PUBLIC"))
    else:
        for statement in _sqlite_triggers():
            op.execute(sa.text(statement))


def downgrade() -> None:
    op.drop_table(_TABLE)  # drops the table's own triggers
    if _is_postgresql():
        for function in ("caliber_effect_proposals_frozen", "caliber_effect_proposals_no_delete"):
            op.execute(sa.text(f"DROP FUNCTION IF EXISTS {function}()"))
