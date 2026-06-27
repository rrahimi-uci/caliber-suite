"""Add skill_name to refinement_jobs for skill-targeted optimization.

Revision ID: 0015
Revises: 0014
"""

from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "caliber_refinement_jobs",
        sa.Column("skill_name", sa.String(128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("caliber_refinement_jobs", "skill_name")
