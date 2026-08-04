"""add release candidates, signoffs, and report jobs

Revision ID: 0085
Revises: 0084
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0085"
down_revision: str | Sequence[str] | None = "0084"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_release_candidates",
        sa.Column("candidate_id", sa.String(64), primary_key=True),
        sa.Column("project_id", sa.String(64), nullable=True),
        sa.Column("visibility", sa.String(16), nullable=False, server_default="project"),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("artifact_type", sa.String(64), nullable=False),
        sa.Column("artifact_ref", sa.String(512), nullable=False),
        sa.Column("version_ref", sa.String(256), nullable=False),
        sa.Column("criteria", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("waivers", sa.JSON(), nullable=False),
        sa.Column("required_score", sa.Float(), nullable=False),
        sa.Column("weighted_score", sa.Float(), nullable=True),
        sa.Column("blockers", sa.JSON(), nullable=False),
        sa.Column("planned_action", sa.JSON(), nullable=False),
        sa.Column("rollback_target", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("owner", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_release_candidate_project_status",
        "caliber_release_candidates",
        ["project_id", "status"],
    )
    op.create_table(
        "caliber_release_signoffs",
        sa.Column("signoff_id", sa.String(64), primary_key=True),
        sa.Column(
            "candidate_id",
            sa.String(64),
            sa.ForeignKey("caliber_release_candidates.candidate_id"),
            nullable=False,
        ),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("decided_by", sa.String(256), nullable=False),
        sa.Column("candidate_snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_release_signoff_candidate_created",
        "caliber_release_signoffs",
        ["candidate_id", "created_at"],
    )
    op.create_table(
        "caliber_release_report_jobs",
        sa.Column("report_job_id", sa.String(64), primary_key=True),
        sa.Column(
            "candidate_id",
            sa.String(64),
            sa.ForeignKey("caliber_release_candidates.candidate_id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("format", sa.String(32), nullable=False),
        sa.Column("report", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_release_report_candidate_created",
        "caliber_release_report_jobs",
        ["candidate_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_release_report_candidate_created", table_name="caliber_release_report_jobs")
    op.drop_table("caliber_release_report_jobs")
    op.drop_index("ix_release_signoff_candidate_created", table_name="caliber_release_signoffs")
    op.drop_table("caliber_release_signoffs")
    op.drop_index("ix_release_candidate_project_status", table_name="caliber_release_candidates")
    op.drop_table("caliber_release_candidates")
