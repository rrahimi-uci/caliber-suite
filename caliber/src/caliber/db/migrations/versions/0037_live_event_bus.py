"""database live event bus

Revision ID: 0037
Revises: 0036
Create Date: 2026-06-13

Adds the shared live-event table used by the database-backed SSE/event bus
backend for multi-replica workflow monitoring and approval updates.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | Sequence[str] | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_live_events",
        sa.Column("event_id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("origin", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_live_events_created", "caliber_live_events", ["created_at"])
    op.create_index(
        "ix_live_events_origin_id",
        "caliber_live_events",
        ["origin", "event_id"],
    )
    op.create_index(
        "ix_live_events_type_created",
        "caliber_live_events",
        ["event_type", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_live_events_type_created", table_name="caliber_live_events")
    op.drop_index("ix_live_events_origin_id", table_name="caliber_live_events")
    op.drop_index("ix_live_events_created", table_name="caliber_live_events")
    op.drop_table("caliber_live_events")
