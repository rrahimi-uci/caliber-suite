"""tool test runs

Revision ID: 0046
Revises: 0045
Create Date: 2026-06-18

Persist durable tool-test runs from the Tools tab so each run and its per-case
verdicts survive a page refresh — the tool analog of the prompt test-run table
(0045). One row per completed run; ``kind`` records the producing surface
(sandbox single-invoke, saved-fixtures suite, or LLM-judged hardening).
``results`` holds the full per-case array; the scalar count/score columns are
server-recomputed summaries for cheap history listing.

Also adds a dedicated ``baseline_run_id`` column to ``caliber_tool_registry``
(tools have no JSON config bag like the prompt target's ``optimizer_config``,
so a column is the cleanest place to pin the comparison baseline).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046"
down_revision: str | Sequence[str] | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_tool_test_runs",
        sa.Column("test_run_id", sa.String(length=64), primary_key=True),
        sa.Column("tool_id", sa.String(length=64), nullable=False),
        sa.Column("tool_version", sa.String(length=32), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="suite"),
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
        "ix_tool_test_runs_tool_created",
        "caliber_tool_test_runs",
        ["tool_id", "created_at"],
    )
    op.create_index(
        "ix_tool_test_runs_created",
        "caliber_tool_test_runs",
        ["created_at"],
    )
    op.create_index(
        "ix_caliber_tool_test_runs_tool_id",
        "caliber_tool_test_runs",
        ["tool_id"],
    )

    op.add_column(
        "caliber_tool_registry",
        sa.Column("baseline_run_id", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("caliber_tool_registry", "baseline_run_id")
    op.drop_index(
        "ix_caliber_tool_test_runs_tool_id",
        table_name="caliber_tool_test_runs",
    )
    op.drop_index(
        "ix_tool_test_runs_created",
        table_name="caliber_tool_test_runs",
    )
    op.drop_index(
        "ix_tool_test_runs_tool_created",
        table_name="caliber_tool_test_runs",
    )
    op.drop_table("caliber_tool_test_runs")
