"""track and retry incident resolution notifications

Revision ID: 0080
Revises: 0079
Create Date: 2026-07-31

``notified_at`` records only the opening notification. Without an independent resolution
marker, a failed ``slo.incident.resolved`` publication could not be distinguished from a
successful one and therefore could never be retried safely.

The new nullable marker means "pending" only for resolutions created after this migration.
Every row already resolved at upgrade time is backfilled from its durable resolution time
(falling back to ``opened_at`` for malformed legacy history), deliberately marking that
history handled. Replaying historical all-clear events during an upgrade would be both
surprising and operationally dangerous.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0080"
down_revision: str | Sequence[str] | None = "0079"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "caliber_incidents",
        sa.Column("resolved_notified_at", sa.DateTime(), nullable=True),
    )
    # Null is the runtime retry signal. Do not assign that meaning retroactively: there is
    # no durable evidence telling us whether old resolution events were published, and an
    # upgrade must not emit a wave of historical all-clears.
    op.execute(
        sa.text(
            """
            UPDATE caliber_incidents
            SET resolved_notified_at = COALESCE(resolved_at, opened_at)
            WHERE status = 'resolved'
            """
        )
    )


def downgrade() -> None:
    op.drop_column("caliber_incidents", "resolved_notified_at")
