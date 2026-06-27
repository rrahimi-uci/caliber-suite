"""structured human-review queues

Revision ID: 0054
Revises: 0053
Create Date: 2026-06-21

Adds ``caliber_review_queues`` + ``caliber_review_items`` — the CALIBER-native
analogue of MLflow's Review Queues (which are Databricks-only). A queue carries a
label schema of review ``questions`` (pass/fail, categorical, numeric, free-text)
and a set of assigned reviewers; each item pins one observed trace. Reviewer
answers are written back onto the trace as MLflow assessments / expectations via
the OSS ``mlflow.log_feedback`` / ``mlflow.log_expectation`` primitives, and the
resulting assessment ids are recorded on the item for provenance.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0054"
down_revision: str | Sequence[str] | None = "0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_review_queues",
        sa.Column("queue_id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("questions", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("reviewers", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("owner", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("visibility", sa.String(length=16), nullable=False, server_default="project"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("name", name="uq_review_queue_name"),
    )
    op.create_table(
        "caliber_review_items",
        sa.Column("item_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "queue_id",
            sa.String(length=64),
            sa.ForeignKey("caliber_review_queues.queue_id"),
            nullable=False,
        ),
        sa.Column("trace_id", sa.String(length=256), nullable=False),
        sa.Column("experiment_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("assigned_to", sa.String(length=256), nullable=True),
        sa.Column("answers", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("assessment_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("completed_by", sa.String(length=256), nullable=True),
        sa.UniqueConstraint("queue_id", "trace_id", name="uq_review_item_queue_trace"),
    )
    op.create_index("ix_review_item_queue", "caliber_review_items", ["queue_id"])


def downgrade() -> None:
    op.drop_index("ix_review_item_queue", table_name="caliber_review_items")
    op.drop_table("caliber_review_items")
    op.drop_table("caliber_review_queues")
