"""add immutable Workspace runtime lineage coordinates

P5-D gives runs, release evidence, and Workspace provider operations one
provider-neutral lineage contract.  The link columns are nullable so legacy
rows remain readable; strict Workspace execution refuses an absent or stale
lineage instead of reconstructing it from mutable aliases.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0105"
down_revision: str | Sequence[str] | None = "0104"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "caliber_release_operations",
    "caliber_regression_runs",
    "caliber_eval_runs",
    "caliber_workflow_runs",
    "caliber_knowledge_base_runs",
    "caliber_prompt_test_runs",
    "caliber_tool_test_runs",
    "caliber_skill_test_runs",
    "caliber_knowledge_base_test_runs",
    "caliber_assistant_runs",
    "caliber_workspace_release_evidence",
    "caliber_workspace_release_evaluations",
    "caliber_workspace_release_operations",
)


def upgrade() -> None:
    op.create_table(
        "caliber_workspace_runtime_lineage",
        sa.Column("lineage_id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("caliber_projects.project_id"),
            nullable=False,
        ),
        sa.Column(
            "workspace_release_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_releases.release_id"),
            nullable=False,
        ),
        sa.Column(
            "revision_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_revisions.revision_id"),
            nullable=False,
        ),
        sa.Column(
            "environment_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_environments.environment_id"),
            nullable=False,
        ),
        sa.Column("consumer_kind", sa.String(32), nullable=False),
        sa.Column("consumer_id", sa.String(128), nullable=False),
        sa.Column("model_id", sa.String(256), nullable=True),
        sa.Column("config_sha256", sa.String(64), nullable=False),
        sa.Column("runtime_dependencies_sha256", sa.String(64), nullable=False),
        sa.Column("policy_sha256", sa.String(64), nullable=False),
        sa.Column(
            "eligibility_status",
            sa.String(16),
            nullable=False,
            server_default="eligible",
        ),
        sa.Column("eligibility_reason", sa.String(4000), nullable=False, server_default=""),
        sa.Column("strict_execution", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(256), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "consumer_kind", "consumer_id", name="uq_workspace_runtime_lineage_consumer"
        ),
        sa.CheckConstraint(
            "consumer_kind IN ('run', 'evidence', 'provider_operation')",
            name="ck_workspace_runtime_lineage_consumer_kind",
        ),
        sa.CheckConstraint(
            "eligibility_status IN ('eligible', 'blocked', 'stale')",
            name="ck_workspace_runtime_lineage_eligibility",
        ),
    )
    op.create_index(
        "ix_workspace_runtime_lineage_project_created",
        "caliber_workspace_runtime_lineage",
        ["project_id", "created_at"],
    )
    op.create_index(
        "ix_workspace_runtime_lineage_release_environment",
        "caliber_workspace_runtime_lineage",
        ["workspace_release_id", "environment_id"],
    )

    for table_name in _TABLES:
        with op.batch_alter_table(table_name) as batch:
            batch.add_column(
                sa.Column(
                    "runtime_lineage_id",
                    sa.String(64),
                    sa.ForeignKey(
                        "caliber_workspace_runtime_lineage.lineage_id",
                        name=f"fk_{table_name}_runtime_lineage",
                    ),
                    nullable=True,
                )
            )
            batch.create_index(f"ix_{table_name}_runtime_lineage", ["runtime_lineage_id"])


def downgrade() -> None:
    for table_name in reversed(_TABLES):
        with op.batch_alter_table(table_name) as batch:
            batch.drop_index(f"ix_{table_name}_runtime_lineage")
            batch.drop_column("runtime_lineage_id")
    op.drop_index(
        "ix_workspace_runtime_lineage_release_environment",
        table_name="caliber_workspace_runtime_lineage",
    )
    op.drop_index(
        "ix_workspace_runtime_lineage_project_created",
        table_name="caliber_workspace_runtime_lineage",
    )
    op.drop_table("caliber_workspace_runtime_lineage")
