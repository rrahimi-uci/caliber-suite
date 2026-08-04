"""add explicit operator resolution and retry lineage for calibration jobs

Revision ID: 0083
Revises: 0082
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0083"
down_revision: str | Sequence[str] | None = "0082"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("caliber_calibration_jobs") as batch:
        batch.add_column(sa.Column("retry_of_job_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("resolution", sa.String(32), nullable=True))
        batch.add_column(sa.Column("resolution_reason", sa.Text(), nullable=True))
        batch.add_column(sa.Column("resolved_by", sa.String(256), nullable=True))
        batch.add_column(sa.Column("resolved_at", sa.DateTime(), nullable=True))
        batch.create_foreign_key(
            "fk_calibration_jobs_retry_of",
            "caliber_calibration_jobs",
            ["retry_of_job_id"],
            ["job_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("caliber_calibration_jobs") as batch:
        batch.drop_constraint("fk_calibration_jobs_retry_of", type_="foreignkey")
        batch.drop_column("resolved_at")
        batch.drop_column("resolved_by")
        batch.drop_column("resolution_reason")
        batch.drop_column("resolution")
        batch.drop_column("retry_of_job_id")
