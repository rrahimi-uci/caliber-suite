"""eval runs (scorecard)

Revision ID: 0049
Revises: 0048
Create Date: 2026-06-19

Persist standalone evaluation runs — the scorecard surface that scores a
dataset's examples through a predict target + deterministic scorers and shows
per-example results (mirrors MLflow's evaluation UI). Distinct from the
refinement-pipeline gate, which records only an aggregate on the approval.

One row per completed run: ``aggregate`` holds the per-scorer mean bag and
``results`` the heavy per-example array (omitted from history summaries). The
scalar count/score columns are server-recomputed summaries for cheap listing.
``dataset_version`` pins the exact example set scored so a run stays
reproducible after the dataset grows.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0049"
down_revision: str | Sequence[str] | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_eval_runs",
        sa.Column("run_id", sa.String(length=64), primary_key=True),
        sa.Column("dataset_id", sa.String(length=64), nullable=False),
        sa.Column("dataset_version", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("predict_target", sa.String(length=32), nullable=False, server_default="llm"),
        sa.Column("model", sa.String(length=256), nullable=True),
        sa.Column("scorers", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("pass_threshold", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("n_examples", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("passed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("overall_score", sa.Float(), nullable=True),
        sa.Column("pass_rate", sa.Float(), nullable=True),
        sa.Column("aggregate", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("results", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="completed"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("visibility", sa.String(length=16), nullable=False, server_default="project"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["dataset_id"], ["caliber_eval_datasets.dataset_id"]),
    )
    op.create_index(
        "ix_eval_runs_dataset_created",
        "caliber_eval_runs",
        ["dataset_id", "created_at"],
    )
    op.create_index(
        "ix_eval_runs_created",
        "caliber_eval_runs",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_eval_runs_created", table_name="caliber_eval_runs")
    op.drop_index("ix_eval_runs_dataset_created", table_name="caliber_eval_runs")
    op.drop_table("caliber_eval_runs")
