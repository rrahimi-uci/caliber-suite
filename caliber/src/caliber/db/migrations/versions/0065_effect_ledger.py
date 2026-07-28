"""at-most-once external effect ledger

Revision ID: 0065
Revises: 0064
Create Date: 2026-07-28

Adds ``caliber_effect_ledger``. Crash recovery was at-least-once for side effects:
an expired lease resets a run to ``queued`` and, without a wait/approval
checkpoint, the worker restarts it from the beginning — so an outbound webhook or
API mutation completed just before the crash fired again.

``effect_key`` is the primary key, derived from
``(workflow_run_id, node_id, canonical inputs)`` and deliberately not salted with
the attempt number, so a restarted run's replay collides with its earlier attempt.
Making it the primary key lets the database arbitrate concurrent claims instead of
application ordering.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0065"
down_revision: str | Sequence[str] | None = "0064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_effect_ledger",
        sa.Column("effect_key", sa.String(length=64), primary_key=True),
        sa.Column("workflow_run_id", sa.String(length=64), nullable=False),
        sa.Column("node_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="in_progress"),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_effect_ledger_run", "caliber_effect_ledger", ["workflow_run_id"])


def downgrade() -> None:
    op.drop_index("ix_effect_ledger_run", table_name="caliber_effect_ledger")
    op.drop_table("caliber_effect_ledger")
