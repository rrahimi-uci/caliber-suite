"""persist scoped model/read observations for offline replay

Revision ID: 0136
Revises: 0135
Create Date: 2026-11-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0136"
down_revision: str | Sequence[str] | None = "0135"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_step_observations",
        sa.Column("observation_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "workflow_run_id",
            sa.String(length=64),
            sa.ForeignKey("caliber_workflow_runs.workflow_run_id"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            sa.String(length=64),
            sa.ForeignKey("caliber_projects.project_id"),
            nullable=True,
        ),
        sa.Column("principal", sa.String(length=256), nullable=False),
        sa.Column("node_id", sa.String(length=128), nullable=False),
        sa.Column("occurrence_id", sa.String(length=128), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("observation_kind", sa.String(length=16), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("content_size", sa.Integer(), nullable=False),
        sa.Column("storage_backend", sa.String(length=16), nullable=False),
        sa.Column("content_ref", sa.String(length=1024), nullable=False),
        sa.Column("object_version_id", sa.String(length=256), nullable=True),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("provider_version", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "workflow_run_id",
            "node_id",
            "occurrence_id",
            "attempt",
            "observation_kind",
            name="uq_step_observation_occurrence",
        ),
        sa.CheckConstraint(
            "observation_kind IN ('model', 'read')",
            name="ck_step_observation_kind",
        ),
        sa.CheckConstraint("attempt >= 1", name="ck_step_observation_attempt"),
        sa.CheckConstraint("content_size >= 0", name="ck_step_observation_content_size"),
        sa.CheckConstraint("length(request_digest) = 64", name="ck_step_observation_request_digest"),
        sa.CheckConstraint("length(content_hash) = 64", name="ck_step_observation_content_hash"),
        sa.CheckConstraint("length(content_ref) > 0", name="ck_step_observation_content_ref"),
    )
    op.create_index(
        "ix_step_observations_run",
        "caliber_step_observations",
        ["workflow_run_id", "created_at"],
    )
    op.create_index(
        "ix_step_observations_scope",
        "caliber_step_observations",
        ["project_id", "principal", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_step_observations_scope", table_name="caliber_step_observations")
    op.drop_index("ix_step_observations_run", table_name="caliber_step_observations")
    op.drop_table("caliber_step_observations")
