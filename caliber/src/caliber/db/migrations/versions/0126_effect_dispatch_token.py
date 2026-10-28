"""record the fencing token an effect proposal was dispatched under

`B05.5b` (#742, ADR 0003): ``caliber_effect_proposals`` gains ``dispatch_token``, the execution-owner lease's fencing token
the owner held when it moved the proposal to ``dispatched`` (evidence recorded with the move, like ``owner_id`` and
``attempt``). The reconciler needs it: an attempt is *abandoned* exactly when the lease that dispatched it has since been
taken over, i.e. its ``dispatch_token`` is lower than the token of the lease the reconciler now holds. ``owner_id`` cannot say
that (a restarted process may reuse a stable id) and the dispatch time cannot either (the application's clock is not the
database's). The migration-0125 guard is replaced so that a move to ``dispatched`` must leave a ``dispatch_token`` (raw SQL
cannot dispatch without one) and the column changes only with the state, like the other evidence. Existing rows keep ``NULL``
(none was dispatched by shipped code); a ``NULL`` token is never treated as abandoned. Downgrade restores 0125's guard and
drops the column.

Revision ID: 0126
Revises: 0125
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0126"
down_revision: str | Sequence[str] | None = "0125"
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
_NO_RECEIPT_MESSAGE = "a committed effect proposal needs a receipt of its own attempt"
_FAILED_MESSAGE = "an effect proposal with a verified receipt cannot be failed"
_CLOSED_MESSAGE = "a receipt can only be recorded for a dispatched, unknown or reconciled proposal"


def _settled(old: str) -> str:
    return f"{old} IN ({', '.join(repr(s) for s in _SETTLED)})"


def _missing(required: dict[str, tuple[str, ...]]) -> str:
    clauses = [
        f"(NEW.state = '{state}' AND ({' OR '.join(f'NEW.{c} IS NULL' for c in columns)}))"
        for state, columns in required.items()
    ]
    return "(" + " OR ".join(clauses) + ")"


def _pg_receipt_rules(enabled: bool) -> str:
    if not enabled:
        return ""
    return f"""IF NEW.state = 'committed' AND NOT EXISTS (
        SELECT 1 FROM {_RECEIPTS} r
        WHERE r.receipt_id = NEW.receipt_id AND r.proposal_id = NEW.proposal_id AND r.attempt IS NOT DISTINCT FROM NEW.attempt
    ) THEN
        RAISE EXCEPTION '{_NO_RECEIPT_MESSAGE}' USING ERRCODE = '23000';
    END IF;
    IF NEW.state = 'failed' AND EXISTS (SELECT 1 FROM {_RECEIPTS} r WHERE r.proposal_id = NEW.proposal_id) THEN
        RAISE EXCEPTION '{_FAILED_MESSAGE}' USING ERRCODE = '23000';
    END IF;"""


def _legal_pg() -> str:
    pairs = [
        f"(OLD.state = '{a}' AND NEW.state = '{b}')" for a, targets in _TRANSITIONS.items() for b in targets
    ]
    return "(" + " OR ".join(pairs) + ")"


def _pg_guard(
    schema: str, evidence: tuple[str, ...], required: dict[str, tuple[str, ...]], receipts: bool = True
) -> str:
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
    {_pg_receipt_rules(receipts)}
    RETURN NEW;
END;
$$"""


_EVIDENCE_0126 = (*_EVIDENCE_0125, "dispatch_token")
_REQUIRED_0126: dict[str, tuple[str, ...]] = {
    **_REQUIRED_0125,
    "dispatched": (*_REQUIRED_0125["dispatched"], "dispatch_token"),
}
_TOKEN_MESSAGE = "a dispatch token is a positive fencing token"


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


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.add_column(_PROPOSALS, sa.Column("dispatch_token", sa.BigInteger(), nullable=True))
    if _is_postgresql():
        schema = op.get_bind().execute(sa.text("SELECT current_schema()")).scalar_one()
        op.execute(
            sa.text(
                f"ALTER TABLE {_PROPOSALS} ADD CONSTRAINT ck_effect_proposal_dispatch_token "
                "CHECK (dispatch_token IS NULL OR dispatch_token >= 1)"
            )
        )
        op.execute(sa.text(_pg_guard(schema, _EVIDENCE_0126, _REQUIRED_0126)))
    else:
        for suffix in ("together", "evidence"):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{_PROPOSALS}_{suffix}"))
        for statement in _sqlite_guard_triggers(_EVIDENCE_0126, _REQUIRED_0126):
            op.execute(sa.text(statement))
        op.execute(
            sa.text(
                f"CREATE TRIGGER trg_{_PROPOSALS}_dispatch_token BEFORE INSERT ON {_PROPOSALS} "
                "WHEN NEW.dispatch_token IS NOT NULL AND NEW.dispatch_token < 1 "
                f"BEGIN SELECT RAISE(ABORT, '{_TOKEN_MESSAGE}'); END"
            )
        )
        op.execute(
            sa.text(
                f"CREATE TRIGGER trg_{_PROPOSALS}_dispatch_token_update BEFORE UPDATE ON {_PROPOSALS} "
                "WHEN NEW.dispatch_token IS NOT NULL AND NEW.dispatch_token < 1 "
                f"BEGIN SELECT RAISE(ABORT, '{_TOKEN_MESSAGE}'); END"
            )
        )


def downgrade() -> None:
    if _is_postgresql():
        schema = op.get_bind().execute(sa.text("SELECT current_schema()")).scalar_one()
        op.execute(sa.text(_pg_guard(schema, _EVIDENCE_0125, _REQUIRED_0125)))
        op.execute(sa.text(f"ALTER TABLE {_PROPOSALS} DROP CONSTRAINT ck_effect_proposal_dispatch_token"))
        op.drop_column(_PROPOSALS, "dispatch_token")
    else:
        for suffix in ("together", "evidence", "dispatch_token", "dispatch_token_update"):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{_PROPOSALS}_{suffix}"))
        op.execute(sa.text(f"ALTER TABLE {_PROPOSALS} DROP COLUMN dispatch_token"))
        for statement in _sqlite_guard_triggers(_EVIDENCE_0125, _REQUIRED_0125):
            op.execute(sa.text(statement))
