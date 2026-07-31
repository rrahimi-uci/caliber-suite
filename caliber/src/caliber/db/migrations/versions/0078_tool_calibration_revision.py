"""fence calibration attribution with a monotonic tool revision

Revision ID: 0078
Revises: 0077
Create Date: 2026-07-31

A worker used to compare a freshly loaded tool with its submitted snapshot and then write
``last_calibration``. An edit committed between those two statements could still receive
the stale result. A revision incremented by every supported definition/fixture mutation
turns attribution into one conditional UPDATE that is atomic on SQLite and PostgreSQL.

Existing rows start at revision 1. Their old ``last_calibration`` payloads have no revision
provenance, so the migration clears them rather than presenting unverifiable evidence as
current. Existing queued snapshots predate the embedded revision; the worker treats a
missing snapshot revision as 1 and still compares the full definition before attempting
the conditional write.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0078"
down_revision: str | Sequence[str] | None = "0077"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "caliber_tool_registry",
        sa.Column("calibration_revision", sa.Integer(), nullable=False, server_default="1"),
    )
    # Pre-0078 results carry no revision, so there is no atomic evidence that they belong
    # to the definition visible at migration time. Preserve the historical test-run rows,
    # but invalidate this denormalized "current" pointer rather than assigning it revision
    # 1 by fiat.
    op.execute(sa.text("UPDATE caliber_tool_registry SET last_calibration = NULL"))


def downgrade() -> None:
    op.drop_column("caliber_tool_registry", "calibration_revision")
