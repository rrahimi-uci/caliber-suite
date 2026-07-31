"""lease accepted webhook deliveries to one live dispatcher replica

Revision ID: 0077
Revises: 0076
Create Date: 2026-07-30

An accepted row used to say only that some process had not settled a delivery. Every replica
therefore swept every row on startup, including work another live replica was actively
delivering. Ownership plus a renewable lease makes recovery a conditional claim instead of
an uncoordinated global delete.

Legacy rows receive an empty owner and a null lease. Both mean unowned/expired, so the first
new dispatcher can recover them without manufacturing a live owner during migration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0077"
down_revision: str | Sequence[str] | None = "0076"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "caliber_webhook_accepted_events",
        sa.Column("owner_id", sa.String(length=128), nullable=False, server_default=""),
    )
    op.add_column(
        "caliber_webhook_accepted_events",
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_caliber_webhook_accepted_events_lease",
        "caliber_webhook_accepted_events",
        ["lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_caliber_webhook_accepted_events_lease",
        table_name="caliber_webhook_accepted_events",
    )
    op.drop_column("caliber_webhook_accepted_events", "lease_expires_at")
    op.drop_column("caliber_webhook_accepted_events", "owner_id")
