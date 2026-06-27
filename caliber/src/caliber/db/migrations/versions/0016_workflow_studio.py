"""workflow studio tables

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-30

Adds the Workflow Studio data model (plan §14): workflows, immutable/draft
versions, deployment aliases, the tool registry, the workflow-to-trace index,
and CALIBER workflow patch candidates.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_workflows",
        sa.Column("workflow_id", sa.String(128), primary_key=True),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("owner", sa.String(256), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("default_experiment_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )

    op.create_table(
        "caliber_workflow_versions",
        sa.Column("version_id", sa.String(64), primary_key=True),
        sa.Column(
            "workflow_id",
            sa.String(128),
            sa.ForeignKey("caliber_workflows.workflow_id"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("manifest", sa.JSON, nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("compiler_version", sa.String(64), nullable=True),
        sa.Column("compiled_artifact_uri", sa.String(512), nullable=True),
        sa.Column("validation_report", sa.JSON, nullable=True),
        sa.Column("created_by", sa.String(256), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("published_by", sa.String(256), nullable=True),
        sa.Column("published_at", sa.DateTime, nullable=True),
        sa.UniqueConstraint("workflow_id", "version_number", name="uq_workflow_version_number"),
    )
    op.create_index(
        "ix_workflow_versions_workflow",
        "caliber_workflow_versions",
        ["workflow_id", "version_number"],
    )

    op.create_table(
        "caliber_workflow_deployments",
        sa.Column("deployment_id", sa.String(64), primary_key=True),
        sa.Column(
            "workflow_id",
            sa.String(128),
            sa.ForeignKey("caliber_workflows.workflow_id"),
            nullable=False,
        ),
        sa.Column("alias", sa.String(64), nullable=False),
        sa.Column(
            "version_id",
            sa.String(64),
            sa.ForeignKey("caliber_workflow_versions.version_id"),
            nullable=False,
        ),
        sa.Column("environment", sa.String(32), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("deployed_by", sa.String(256), nullable=True),
        sa.Column("deployed_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("rollback_checkpoint", sa.JSON, nullable=False, server_default="[]"),
        sa.UniqueConstraint("workflow_id", "alias", name="uq_workflow_deployment_alias"),
    )

    op.create_table(
        "caliber_tool_registry",
        sa.Column("tool_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("module_path", sa.String(512), nullable=False),
        sa.Column("callable_name", sa.String(128), nullable=False),
        sa.Column("input_schema", sa.JSON, nullable=True),
        sa.Column("output_schema", sa.JSON, nullable=True),
        sa.Column("side_effect_level", sa.String(16), nullable=False, server_default="read"),
        sa.Column("requires_approval", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("allow_in_preview", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("secret_refs", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("owner", sa.String(256), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("deprecated_at", sa.DateTime, nullable=True),
        sa.Column("successor_tool_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
        sa.UniqueConstraint("name", "version", name="uq_tool_name_version"),
    )
    op.create_index("ix_tool_registry_name", "caliber_tool_registry", ["name"])

    op.create_table(
        "caliber_workflow_runs",
        sa.Column("workflow_run_id", sa.String(64), primary_key=True),
        sa.Column("workflow_id", sa.String(128), nullable=False),
        sa.Column("workflow_version_id", sa.String(64), nullable=True),
        sa.Column("deployment_alias", sa.String(64), nullable=True),
        sa.Column("mlflow_run_id", sa.String(64), nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("session_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="completed"),
        sa.Column("started_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime, nullable=True),
        sa.Column("summary", sa.JSON, nullable=True),
    )
    op.create_index(
        "ix_workflow_runs_workflow", "caliber_workflow_runs", ["workflow_id", "started_at"]
    )
    op.create_index("ix_workflow_runs_trace", "caliber_workflow_runs", ["trace_id"])

    op.create_table(
        "caliber_workflow_patches",
        sa.Column("patch_id", sa.String(64), primary_key=True),
        sa.Column("job_id", sa.String(64), nullable=True),
        sa.Column("workflow_id", sa.String(128), nullable=False),
        sa.Column("base_version_id", sa.String(64), nullable=False),
        sa.Column("candidate_manifest", sa.JSON, nullable=False),
        sa.Column("semantic_ops", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("patch_summary", sa.Text, nullable=False, server_default=""),
        sa.Column("graph_diff", sa.JSON, nullable=True),
        sa.Column("risk_summary", sa.Text, nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_workflow_patches_job", "caliber_workflow_patches", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_workflow_patches_job", table_name="caliber_workflow_patches")
    op.drop_table("caliber_workflow_patches")
    op.drop_index("ix_workflow_runs_trace", table_name="caliber_workflow_runs")
    op.drop_index("ix_workflow_runs_workflow", table_name="caliber_workflow_runs")
    op.drop_table("caliber_workflow_runs")
    op.drop_index("ix_tool_registry_name", table_name="caliber_tool_registry")
    op.drop_table("caliber_tool_registry")
    op.drop_table("caliber_workflow_deployments")
    op.drop_index("ix_workflow_versions_workflow", table_name="caliber_workflow_versions")
    op.drop_table("caliber_workflow_versions")
    op.drop_table("caliber_workflows")
