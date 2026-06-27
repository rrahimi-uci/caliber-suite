"""job heartbeat column

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-15

Adds ``caliber_refinement_jobs.last_heartbeat_at`` so the janitor task
(``caliber.orchestrator.janitor``) can detect jobs whose worker crashed
mid-stage. The worker bumps this column at the start of every stage; a
stale value relative to the configured threshold marks the job for
reaping.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("caliber_refinement_jobs") as batch_op:
        batch_op.add_column(sa.Column("last_heartbeat_at", sa.DateTime, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("caliber_refinement_jobs") as batch_op:
        batch_op.drop_column("last_heartbeat_at")
