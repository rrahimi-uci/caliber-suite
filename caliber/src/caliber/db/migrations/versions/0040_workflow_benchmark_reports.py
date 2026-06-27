"""workflow benchmark reports

Revision ID: 0040
Revises: 0039
Create Date: 2026-06-15

Persist workflow benchmark scorecards so bakeoff evidence can move beyond
browser-local drafts and become a project/user-scoped artifact in CALIBER.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040"
down_revision: str | Sequence[str] | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_workflow_benchmark_reports",
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column(
            "visibility",
            sa.String(length=16),
            nullable=False,
            server_default="project",
        ),
        sa.Column("report_id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("owner", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="draft"),
        sa.Column("worksheet", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_wf_benchmark_reports_owner_status",
        "caliber_workflow_benchmark_reports",
        ["owner", "status"],
    )
    op.create_index(
        "ix_wf_benchmark_reports_project_status",
        "caliber_workflow_benchmark_reports",
        ["project_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_wf_benchmark_reports_project_status",
        table_name="caliber_workflow_benchmark_reports",
    )
    op.drop_index(
        "ix_wf_benchmark_reports_owner_status",
        table_name="caliber_workflow_benchmark_reports",
    )
    op.drop_table("caliber_workflow_benchmark_reports")
