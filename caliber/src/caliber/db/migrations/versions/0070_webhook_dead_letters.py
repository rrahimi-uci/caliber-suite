"""durable webhook dead letters

Revision ID: 0070
Revises: 0069
Create Date: 2026-07-28

Adds ``caliber_webhook_dead_letters``, replacing the in-memory ring as the record of
outbound events that could not be delivered.

The ring closed the *silent* half of webhook loss — an operator could see which events
failed — but it was itself lost on restart, so the record of a failure disappeared
exactly when someone rebooted to fix the receiver. A delivery failure is durable state:
it means a downstream system was never told something happened.

Two producers write here:

* delivery exhaustion, after every retry attempt failed; and
* **enqueue overflow**, when events arrive faster than delivery drains them. That case
  previously had no record at all: the event bus dropped events into a
  ``logger.warning`` once a subscriber queue filled, so a slow receiver could shed
  load invisibly.

``status`` lets an operator mark a row handled without deleting the evidence, so the
list is a work queue rather than an ever-growing wall.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0070"
down_revision: str | Sequence[str] | None = "0069"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_webhook_dead_letters",
        sa.Column("dead_letter_id", sa.String(length=64), primary_key=True),
        sa.Column("url", sa.String(length=1024), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=True),
        # The whole event, so a resolved failure can actually be replayed by hand
        # rather than merely acknowledged.
        sa.Column("event", sa.JSON(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="exhausted"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("failed_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("acknowledged_by", sa.String(length=256), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
    )
    # The operations list filters by status and orders by recency.
    op.create_index(
        "ix_webhook_dead_letters_status",
        "caliber_webhook_dead_letters",
        ["status", "failed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_dead_letters_status", table_name="caliber_webhook_dead_letters")
    op.drop_table("caliber_webhook_dead_letters")
