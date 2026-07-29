"""worker self-reported liveness

Revision ID: 0069
Revises: 0068
Create Date: 2026-07-28

Adds ``caliber_worker_heartbeats``, closing the gap that worker liveness could only
be *inferred* from ``running`` workflow-run rows.

That inference is unsound while the queue is empty: with nothing claimed there are no
rows to infer from, so an idle worker that has died looks identical to an idle worker
that is healthy, and the empty-queue state ``(queued=0, running=0, workers_alive=0)``
was encoded as healthy. The failure surfaced only once a backlog accumulated.

One row per worker identity, rewritten every poll cycle. An absent or stale row is
then positive evidence that nothing is consuming the queue, rather than an absence of
evidence. ``ticks`` distinguishes a wedged loop (timestamp advancing without ticks is
impossible; ticks frozen while the row ages means no cycle completed) from an idle one.

No backfill: a heartbeat is a live fact. Manufacturing rows for workers that may not
be running would assert liveness this migration cannot verify — the opposite of the
property being added.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0069"
down_revision: str | Sequence[str] | None = "0068"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_worker_heartbeats",
        sa.Column("worker_id", sa.String(length=128), primary_key=True),
        sa.Column("kind", sa.String(length=32), nullable=False, server_default="workflow_run"),
        sa.Column("hostname", sa.String(length=256), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column(
            "last_heartbeat_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("ticks", sa.Integer(), nullable=False, server_default="0"),
    )
    # Queue health filters by kind and orders by recency on every /system/queue read.
    op.create_index(
        "ix_worker_heartbeats_kind",
        "caliber_worker_heartbeats",
        ["kind", "last_heartbeat_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_worker_heartbeats_kind", table_name="caliber_worker_heartbeats")
    op.drop_table("caliber_worker_heartbeats")
