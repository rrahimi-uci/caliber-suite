"""add the single-owner execution lease table

`B13.2` (#668, ADR 0002): in the reference profile exactly one process holds the execution-owner lease and only
the holder runs the background loops. This migration adds the lease table, one row per lease name, holding the
owner, a monotonic fencing token and the expiry. Expiry is decided by the database's own clock, never by a
process's, and every transition is a compare-and-set on the exact owner and token (``caliber.ownership``).

The table starts empty (no lease is held until a process acquires one) and nothing reads it until the lifespan wiring
(#673), so this changes no behaviour. Downgrade drops it.

Revision ID: 0116
Revises: 0115
Create Date: 2026-10-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0116"
down_revision: str | Sequence[str] | None = "0115"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_execution_owner",
        sa.Column("lease_name", sa.String(length=64), primary_key=True),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(), nullable=False),
        sa.Column("renewed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("fencing_token >= 1", name="ck_execution_owner_token"),
    )


def downgrade() -> None:
    op.drop_table("caliber_execution_owner")
