"""prompt test runs

Revision ID: 0045
Revises: 0044
Create Date: 2026-06-17

Persist ad-hoc prompt-test runs from the Prompts tab so each run and its
per-case judge verdicts survive a page refresh. One row per completed run:
``results`` holds the full per-case array; the scalar count/score columns are
server-recomputed summaries for cheap history listing.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045"
down_revision: str | Sequence[str] | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_prompt_test_runs",
        sa.Column("test_run_id", sa.String(length=64), primary_key=True),
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("prompt_name", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("prompt_alias", sa.String(length=64), nullable=True),
        sa.Column("prompt_version", sa.Integer(), nullable=True),
        sa.Column("model", sa.String(length=256), nullable=True),
        sa.Column("eval_dataset_id", sa.String(length=64), nullable=True),
        sa.Column("test_set_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("passed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("partial_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("overall_score", sa.Float(), nullable=True),
        sa.Column("results", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("mlflow_run_id", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="completed"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_prompt_test_runs_agent_created",
        "caliber_prompt_test_runs",
        ["agent_id", "created_at"],
    )
    op.create_index(
        "ix_prompt_test_runs_created",
        "caliber_prompt_test_runs",
        ["created_at"],
    )
    op.create_index(
        "ix_caliber_prompt_test_runs_agent_id",
        "caliber_prompt_test_runs",
        ["agent_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_caliber_prompt_test_runs_agent_id",
        table_name="caliber_prompt_test_runs",
    )
    op.drop_index(
        "ix_prompt_test_runs_created",
        table_name="caliber_prompt_test_runs",
    )
    op.drop_index(
        "ix_prompt_test_runs_agent_created",
        table_name="caliber_prompt_test_runs",
    )
    op.drop_table("caliber_prompt_test_runs")
