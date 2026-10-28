"""add the consumer/source/event-id inbox

`B13.5a` (#689): an inbound event (a webhook, a connector callback) is identified by ``(consumer, source, event_id)``.
``caliber_inbox_events`` holds one row per identity, with the SHA-256 of the payload it arrived with, so a redelivery
is a no-op and a second delivery under the same id with different content is refused. The row is written in the same
transaction as the business transition it guards and as its ``processed``/``ignored`` marker. Downgrade drops the table
and with it the memory of what was already applied (it never runs where that matters).

Revision ID: 0122
Revises: 0121
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0122"
down_revision: str | Sequence[str] | None = "0121"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_inbox_events",
        sa.Column("consumer", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.String(length=160), nullable=False),
        sa.Column("payload_hash", sa.String(length=71), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="received"),
        sa.Column("received_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("consumer", "source", "event_id"),
        sa.CheckConstraint(
            "status IN ('received', 'processed', 'ignored')", name="ck_inbox_events_status"
        ),
        sa.CheckConstraint(
            "length(consumer) > 0 AND length(source) > 0 AND length(event_id) > 0",
            name="ck_inbox_events_identity_present",
        ),
        sa.CheckConstraint("length(payload_hash) = 71", name="ck_inbox_events_hash_length"),
    )
    op.create_index("ix_inbox_events_received", "caliber_inbox_events", ["received_at"])


def downgrade() -> None:
    op.drop_index("ix_inbox_events_received", table_name="caliber_inbox_events")
    op.drop_table("caliber_inbox_events")
