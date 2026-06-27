"""custom LLM judges (MLflow 3.14 make_judge)

Revision ID: 0053
Revises: 0052
Create Date: 2026-06-21

Adds ``caliber_judges`` — reusable custom LLM judges authored by operators and
selected as scorers in eval runs. Each row is a name + natural-language
``instructions`` (referencing the ``{{ inputs }}`` / ``{{ outputs }}`` /
``{{ expectations }}`` evaluation variables) + an optional model identifier. The
eval runner rebuilds the judge via ``mlflow.genai.make_judge`` at evaluate-time
and hands it to ``mlflow.genai.evaluate``; CALIBER stays the source of truth.

Scoping (``project_id`` / ``visibility``) and the lifecycle ``status`` mirror the
other CALIBER assets (skills, eval datasets).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0053"
down_revision: str | Sequence[str] | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_judges",
        sa.Column("judge_id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("instructions", sa.Text(), nullable=False, server_default=""),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("feedback_value_type", sa.String(length=16), nullable=True),
        sa.Column("owner", sa.String(length=256), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("visibility", sa.String(length=16), nullable=False, server_default="project"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("name", name="uq_judge_name"),
    )


def downgrade() -> None:
    op.drop_table("caliber_judges")
