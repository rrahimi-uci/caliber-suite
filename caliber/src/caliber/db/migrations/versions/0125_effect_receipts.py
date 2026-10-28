"""persist immutable verified effect receipts; a proposal commits only with one

`B05.4a` (#717, ADR 0003): ``caliber_effect_receipts`` holds what the downstream said about a performed effect *after the
platform read it back* (``verified_by`` is ``query``): the proposal and dispatch ``attempt`` it answers, the proposal's
``logical_key`` (the idempotency key that was emitted), ``intent_hash``, connector identity, target resource and the resource
version it produced, the downstream's request id and the digest of its stored response, as canonical JSON bytes
(``receipt_bytes``) with their SHA-256 (``receipt_digest``; on PostgreSQL a CHECK pins it to the bytes). One authoritative
receipt per ``(proposal_id, attempt)``, and a downstream request id can belong to one proposal only
(``(connector, downstream_request_id)`` is unique). UPDATE and DELETE are refused by triggers (SQLite and PostgreSQL;
PostgreSQL adds a BEFORE TRUNCATE trigger). ``caliber_effect_proposals`` gains ``receipt_id``, and migration 0124's guard is
replaced so that a move to ``committed`` must leave ``receipt_id`` in the row (raw SQL cannot commit without a receipt).
Downgrade drops the receipts and ``receipt_id`` and restores 0124's guard.

Revision ID: 0125
Revises: 0124
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0125"
down_revision: str | Sequence[str] | None = "0124"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RECEIPTS = "caliber_effect_receipts"
_PROPOSALS = "caliber_effect_proposals"
_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "proposed": ("approved", "failed"),
    "approved": ("dispatched", "failed"),
    "dispatched": ("committed", "failed", "unknown"),
    "unknown": ("reconciled",),
    "reconciled": ("committed", "failed"),
}
_SETTLED = ("committed", "failed")
_EVIDENCE_0124 = (
    "version",
    "attempt",
    "owner_id",
    "dispatched_at",
    "unknown_at",
    "settled_at",
    "reason",
    "approval_kind",
    "approval_ref",
)
_REQUIRED_0124: dict[str, tuple[str, ...]] = {
    "approved": ("approval_kind", "approval_ref"),
    "dispatched": ("owner_id", "attempt", "dispatched_at"),
    "committed": ("settled_at",),
    "failed": ("settled_at", "reason"),
    "unknown": ("unknown_at", "reason"),
    "reconciled": ("reason",),
}
_EVIDENCE_0125 = (*_EVIDENCE_0124, "receipt_id")
_REQUIRED_0125: dict[str, tuple[str, ...]] = {**_REQUIRED_0124, "committed": ("settled_at", "receipt_id")}
_SETTLED_MESSAGE = "a settled effect proposal is final"
_TOGETHER_MESSAGE = "the evidence of an effect proposal changes only with its state"
_ILLEGAL_MESSAGE = "illegal effect proposal transition"
_VERSION_MESSAGE = "an effect proposal transition advances the version by one"
_EVIDENCE_MESSAGE = "an effect proposal transition records its evidence"
_RECEIPT_MESSAGE = "caliber_effect_receipts is immutable"


def _settled(old: str) -> str:
    return f"{old} IN ({', '.join(repr(s) for s in _SETTLED)})"


def _missing(required: dict[str, tuple[str, ...]]) -> str:
    clauses = [
        f"(NEW.state = '{state}' AND ({' OR '.join(f'NEW.{c} IS NULL' for c in columns)}))"
        for state, columns in required.items()
    ]
    return "(" + " OR ".join(clauses) + ")"


def _legal_pg() -> str:
    pairs = [
        f"(OLD.state = '{a}' AND NEW.state = '{b}')" for a, targets in _TRANSITIONS.items() for b in targets
    ]
    return "(" + " OR ".join(pairs) + ")"


def _pg_guard(schema: str, evidence: tuple[str, ...], required: dict[str, tuple[str, ...]]) -> str:
    pin = f'SET search_path = "{schema}", pg_temp'
    changed = " OR ".join(f"NEW.{c}::text IS DISTINCT FROM OLD.{c}::text" for c in evidence)
    return f"""
CREATE OR REPLACE FUNCTION "{schema}".caliber_effect_proposals_guard() RETURNS trigger LANGUAGE plpgsql {pin} AS $$
BEGIN
    IF {_settled("OLD.state")} THEN
        RAISE EXCEPTION '{_SETTLED_MESSAGE}' USING ERRCODE = '23000';
    END IF;
    IF NEW.state IS NOT DISTINCT FROM OLD.state THEN
        IF {changed} THEN
            RAISE EXCEPTION '{_TOGETHER_MESSAGE}' USING ERRCODE = '23000';
        END IF;
        RETURN NEW;
    END IF;
    IF NOT {_legal_pg()} THEN
        RAISE EXCEPTION '{_ILLEGAL_MESSAGE}: % -> %', OLD.state, NEW.state USING ERRCODE = '23000';
    END IF;
    IF NEW.version IS DISTINCT FROM OLD.version + 1 THEN
        RAISE EXCEPTION '{_VERSION_MESSAGE}' USING ERRCODE = '23000';
    END IF;
    IF {_missing(required)} THEN
        RAISE EXCEPTION '{_EVIDENCE_MESSAGE}: %', NEW.state USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$$"""


def _sqlite_guard_triggers(evidence: tuple[str, ...], required: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    changed = " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in evidence)
    return (
        f"CREATE TRIGGER trg_{_PROPOSALS}_together BEFORE UPDATE ON {_PROPOSALS} "
        f"WHEN NOT {_settled('OLD.state')} AND NEW.state IS OLD.state AND ({changed}) "
        f"BEGIN SELECT RAISE(ABORT, '{_TOGETHER_MESSAGE}'); END",
        f"CREATE TRIGGER trg_{_PROPOSALS}_evidence BEFORE UPDATE ON {_PROPOSALS} "
        f"WHEN NOT {_settled('OLD.state')} AND NEW.state IS NOT OLD.state AND {_missing(required)} "
        f"BEGIN SELECT RAISE(ABORT, '{_EVIDENCE_MESSAGE}'); END",
    )


def _pg_receipt_function(schema: str) -> str:
    return f"""
CREATE FUNCTION "{schema}".caliber_effect_receipts_immutable() RETURNS trigger LANGUAGE plpgsql
    SET search_path = "{schema}", pg_temp AS $$
BEGIN
    RAISE EXCEPTION '{_RECEIPT_MESSAGE}: % refused', TG_OP USING ERRCODE = '23000';
END;
$$"""


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        _RECEIPTS,
        sa.Column("receipt_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "proposal_id",
            sa.String(length=64),
            sa.ForeignKey(f"{_PROPOSALS}.proposal_id"),
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("logical_key", sa.String(length=64), nullable=False),
        sa.Column("intent_hash", sa.String(length=71), nullable=False),
        sa.Column("connector", sa.String(length=128), nullable=False),
        sa.Column("connector_version", sa.String(length=64), nullable=False),
        sa.Column("resource", sa.String(length=512), nullable=False),
        sa.Column("downstream_request_id", sa.String(length=256), nullable=False),
        sa.Column("resource_version", sa.String(length=256), nullable=True),
        sa.Column("response_digest", sa.String(length=71), nullable=False),
        sa.Column("verified_by", sa.String(length=8), nullable=False),
        sa.Column("verified_at", sa.DateTime(), nullable=False),
        sa.Column("receipt_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("receipt_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("proposal_id", "attempt", name="uq_effect_receipt_attempt"),
        sa.UniqueConstraint("connector", "downstream_request_id", name="uq_effect_receipt_request"),
        sa.CheckConstraint("verified_by = 'query'", name="ck_effect_receipt_verified"),
        sa.CheckConstraint("attempt >= 1", name="ck_effect_receipt_attempt"),
        sa.CheckConstraint("length(receipt_digest) = 64", name="ck_effect_receipt_digest_length"),
        sa.CheckConstraint("length(receipt_bytes) > 0", name="ck_effect_receipt_bytes_present"),
        sa.CheckConstraint("length(response_digest) = 71", name="ck_effect_receipt_response_digest"),
        sa.CheckConstraint("length(intent_hash) = 71", name="ck_effect_receipt_intent_hash"),
    )
    op.create_index("ix_effect_receipts_proposal", _RECEIPTS, ["proposal_id"])
    op.add_column(_PROPOSALS, sa.Column("receipt_id", sa.String(length=64), nullable=True))
    if _is_postgresql():
        schema = op.get_bind().execute(sa.text("SELECT current_schema()")).scalar_one()
        op.execute(
            sa.text(
                f"ALTER TABLE {_RECEIPTS} ADD CONSTRAINT ck_effect_receipt_digest_is_sha256 "
                "CHECK (receipt_digest = encode(sha256(receipt_bytes), 'hex'))"
            )
        )
        op.execute(sa.text(_pg_receipt_function(schema)))
        op.execute(
            sa.text(
                f"CREATE TRIGGER trg_{_RECEIPTS}_immutable BEFORE UPDATE OR DELETE ON {_RECEIPTS} "
                "FOR EACH ROW EXECUTE FUNCTION caliber_effect_receipts_immutable()"
            )
        )
        op.execute(
            sa.text(
                f"CREATE TRIGGER trg_{_RECEIPTS}_no_truncate BEFORE TRUNCATE ON {_RECEIPTS} "
                "FOR EACH STATEMENT EXECUTE FUNCTION caliber_effect_receipts_immutable()"
            )
        )
        op.execute(sa.text(f"REVOKE UPDATE, DELETE, TRUNCATE ON {_RECEIPTS} FROM PUBLIC"))
        op.execute(sa.text(_pg_guard(schema, _EVIDENCE_0125, _REQUIRED_0125)))
    else:
        for verb in ("UPDATE", "DELETE"):
            op.execute(
                sa.text(
                    f"CREATE TRIGGER trg_{_RECEIPTS}_no_{verb.lower()} BEFORE {verb} ON {_RECEIPTS} "
                    f"BEGIN SELECT RAISE(ABORT, '{_RECEIPT_MESSAGE}: {verb} refused'); END"
                )
            )
        for suffix in ("together", "evidence"):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{_PROPOSALS}_{suffix}"))
        for statement in _sqlite_guard_triggers(_EVIDENCE_0125, _REQUIRED_0125):
            op.execute(sa.text(statement))


def downgrade() -> None:
    if _is_postgresql():
        schema = op.get_bind().execute(sa.text("SELECT current_schema()")).scalar_one()
        op.execute(sa.text(_pg_guard(schema, _EVIDENCE_0124, _REQUIRED_0124)))
        op.drop_table(_RECEIPTS)  # drops the table's own triggers
        op.execute(sa.text("DROP FUNCTION IF EXISTS caliber_effect_receipts_immutable()"))
        op.drop_column(_PROPOSALS, "receipt_id")
    else:
        for suffix in ("together", "evidence"):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{_PROPOSALS}_{suffix}"))
        op.drop_table(_RECEIPTS)
        op.execute(sa.text(f"ALTER TABLE {_PROPOSALS} DROP COLUMN receipt_id"))
        for statement in _sqlite_guard_triggers(_EVIDENCE_0124, _REQUIRED_0124):
            op.execute(sa.text(statement))
