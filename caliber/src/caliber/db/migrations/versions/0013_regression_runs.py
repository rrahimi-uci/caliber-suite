"""regression replay runs

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-16

Adds ``caliber_regression_runs`` — a durable replay/eval record used by the
approval endpoint as the "no approval without passing replay" gate.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_regression_runs",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column(
            "job_id",
            sa.String(64),
            sa.ForeignKey("caliber_refinement_jobs.job_id"),
            nullable=False,
        ),
        sa.Column(
            "approval_id",
            sa.String(64),
            sa.ForeignKey("caliber_approval_requests.approval_id"),
            nullable=True,
        ),
        sa.Column(
            "agent_id",
            sa.String(64),
            sa.ForeignKey("caliber_agent_config.agent_id"),
            nullable=False,
        ),
        sa.Column("candidate_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("required_for_approval", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("failure_reason", sa.Text, nullable=True),
        sa.Column("dataset_ids", sa.JSON, nullable=False),
        sa.Column("trace_sample_ids", sa.JSON, nullable=False),
        sa.Column("baseline_scores", sa.JSON, nullable=True),
        sa.Column("candidate_scores", sa.JSON, nullable=False),
        sa.Column("deltas", sa.JSON, nullable=False),
        sa.Column("regressions", sa.JSON, nullable=False),
        sa.Column("gate", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime, nullable=True),
    )
    op.create_index(
        "ix_regression_runs_approval_created",
        "caliber_regression_runs",
        ["approval_id", "created_at"],
    )
    op.create_index(
        "ix_regression_runs_job_created",
        "caliber_regression_runs",
        ["job_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_regression_runs_job_created", table_name="caliber_regression_runs")
    op.drop_index("ix_regression_runs_approval_created", table_name="caliber_regression_runs")
    op.drop_table("caliber_regression_runs")
