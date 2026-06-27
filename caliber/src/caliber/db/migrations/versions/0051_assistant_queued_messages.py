"""assistant queued / steer messages

Revision ID: 0051
Revises: 0050
Create Date: 2026-06-20

Adds ``caliber_assistant_queued_messages`` — the backing store for Aria's
"add to queue" and "steer" affordances, mirroring a code assistant's ability to
stack follow-up turns or inject a course-correction while a turn is in flight.

Each row is a not-yet-sent user turn for a session. ``kind`` distinguishes a
plain queued follow-up (``queued``) from a priority steer (``steer``); ``position``
orders the queue, with steer rows taking a lower position so they jump to the
front. ``status`` tracks ``pending`` rows until they are dispatched (sent as a real
message) or cancelled. ``mode`` records the interaction mode the turn should run in
when it is finally dispatched.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0051"
down_revision: str | Sequence[str] | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_assistant_queued_messages",
        sa.Column("queue_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=64),
            sa.ForeignKey("caliber_assistant_sessions.session_id"),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("mode", sa.String(length=16), nullable=False, server_default="build"),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("created_by", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_asst_queue_session",
        "caliber_assistant_queued_messages",
        ["session_id", "status", "position"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_asst_queue_session",
        table_name="caliber_assistant_queued_messages",
    )
    op.drop_table("caliber_assistant_queued_messages")
