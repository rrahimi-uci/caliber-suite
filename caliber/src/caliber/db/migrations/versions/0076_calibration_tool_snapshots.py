"""snapshot the executable tool definition for durable calibration jobs

Revision ID: 0076
Revises: 0075
Create Date: 2026-07-30

Cases alone are not enough to identify a calibration. A queued job previously loaded the
live Tool row at execution time, so an edit to its module, callable, policy, or schema could
silently change what the already-submitted job executed. The worker also could not tell
whether a result still belonged to the submitted definition.

New submissions persist the JSON-serializable ``ToolSchema`` beside the case snapshot. The
column is nullable so an upgrade does not invent evidence for already-queued rows; those jobs
fail explicitly and can be resubmitted from a real current definition.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0076"
down_revision: str | Sequence[str] | None = "0075"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "caliber_calibration_jobs",
        sa.Column("tool_snapshot", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("caliber_calibration_jobs", "tool_snapshot")
