"""add the event outbox and the consumer inbox

`B13.4` (#685): a business event is recorded in the transaction of the transition that caused it
(``caliber_event_outbox``) and handed to each consumer's durable inbox (``caliber_event_inbox``, primary key
``(consumer, event_id)``, so redelivery is a no-op) by a retryable publisher. Both tables start empty and nothing
reads them until ``CALIBER_EVENT_OUTBOX_ENABLED`` (always on in the reference profile). Downgrade drops them, and
with them any event not yet delivered.

Revision ID: 0119
Revises: 0118
Create Date: 2026-10-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0119"
down_revision: str | Sequence[str] | None = "0118"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_event_outbox",
        sa.Column("event_id", sa.String(length=160), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("envelope", sa.JSON(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("claimed_by", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("event_id"),
        sa.CheckConstraint("status IN ('pending', 'delivered', 'dead')", name="ck_event_outbox_status"),
    )
    op.create_index("ix_event_outbox_due", "caliber_event_outbox", ["status", "next_attempt_at"])
    op.create_index(
        "ix_event_outbox_aggregate",
        "caliber_event_outbox",
        ["aggregate_type", "aggregate_id", "created_at"],
    )
    op.create_table(
        "caliber_event_inbox",
        sa.Column("consumer", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.String(length=160), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("envelope", sa.JSON(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("consumer", "event_id"),
    )
    op.create_index(
        "ix_event_inbox_pending", "caliber_event_inbox", ["consumer", "processed_at", "received_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_event_inbox_pending", table_name="caliber_event_inbox")
    op.drop_table("caliber_event_inbox")
    op.drop_index("ix_event_outbox_aggregate", table_name="caliber_event_outbox")
    op.drop_index("ix_event_outbox_due", table_name="caliber_event_outbox")
    op.drop_table("caliber_event_outbox")
