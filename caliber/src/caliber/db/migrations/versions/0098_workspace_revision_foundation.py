"""add dormant Workspace source/import/revision persistence

`P4-A` (docs/workspace-plan.md): add the source, import-job, revision and
revision-resource schema, the per-project revision counter, and the nullable
accepted-revision foreign key that was prepared by migration 0093. No import
worker, provider call, or public route is enabled by this migration.

Revision ID: 0098
Revises: 0097
Create Date: 2026-09-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0098"
down_revision: str | Sequence[str] | None = "0097"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "caliber_projects",
        sa.Column("next_revision_number", sa.Integer(), nullable=False, server_default="1"),
    )

    op.create_table(
        "caliber_workspace_sources",
        sa.Column("source_id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("caliber_projects.project_id"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_host", sa.String(256), nullable=False),
        sa.Column("canonical_repository_id", sa.String(256), nullable=False),
        sa.Column("display_path", sa.String(512), nullable=False),
        sa.Column("default_branch", sa.String(256), nullable=False, server_default="main"),
        sa.Column("root_path", sa.String(1024), nullable=False, server_default=""),
        sa.Column(
            "manifest_path",
            sa.String(1024),
            nullable=False,
            server_default=".caliber/workspace.yaml",
        ),
        sa.Column("import_mode", sa.String(24), nullable=False, server_default="push"),
        sa.Column("connection_ref", sa.String(256), nullable=True),
        sa.Column("provider_capabilities", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("external_review_policy", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column(
            "external_review_policy_version", sa.String(64), nullable=False, server_default="v1"
        ),
        sa.Column(
            "external_review_policy_sha256", sa.String(64), nullable=False, server_default=""
        ),
        sa.Column("provider_ruleset_sha256", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("last_verified_at", sa.DateTime(), nullable=True),
        sa.Column("last_reconciled_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.String(256), nullable=False, server_default=""),
        sa.Column("updated_by", sa.String(256), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "provider IN ('github', 'gitlab', 'bitbucket')",
            name="ck_workspace_source_provider",
        ),
        sa.CheckConstraint(
            "import_mode IN ('push', 'provider_pull')",
            name="ck_workspace_source_import_mode",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled', 'error')",
            name="ck_workspace_source_status",
        ),
        sa.UniqueConstraint("project_id", name="uq_workspace_source_project"),
    )
    op.create_index(
        "ix_workspace_sources_project_status",
        "caliber_workspace_sources",
        ["project_id", "status"],
    )

    op.create_table(
        "caliber_workspace_revisions",
        sa.Column("revision_id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("caliber_projects.project_id"),
            nullable=False,
        ),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column(
            "source_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_sources.source_id"),
            nullable=True,
        ),
        sa.Column("source_commit_sha", sa.String(128), nullable=True),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("source_bundle_sha256", sa.String(64), nullable=False),
        sa.Column(
            "source_snapshot_file_id",
            sa.String(64),
            sa.ForeignKey("caliber_workflow_files.file_id"),
            nullable=True,
        ),
        sa.Column(
            "source_attestation", sa.String(32), nullable=False, server_default="caller_attested"
        ),
        sa.Column("revision_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="validating"),
        sa.Column("validation_report", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(256), nullable=False, server_default=""),
        sa.Column("validated_by", sa.String(256), nullable=True),
        sa.Column("validated_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('validating', 'ready', 'invalid')",
            name="ck_workspace_revision_status",
        ),
        sa.UniqueConstraint("project_id", "revision_number", name="uq_workspace_revision_number"),
        sa.UniqueConstraint("project_id", "revision_sha256", name="uq_workspace_revision_digest"),
        sa.UniqueConstraint(
            "project_id",
            "source_id",
            "source_commit_sha",
            "source_bundle_sha256",
            "manifest_sha256",
            name="uq_workspace_revision_import_content",
        ),
        sa.UniqueConstraint(
            "source_id", "source_commit_sha", name="uq_workspace_source_commit_observation"
        ),
    )
    op.create_index(
        "ix_workspace_revisions_project_status",
        "caliber_workspace_revisions",
        ["project_id", "status"],
    )

    op.create_table(
        "caliber_workspace_revision_resources",
        sa.Column("resource_pin_id", sa.String(64), primary_key=True),
        sa.Column(
            "revision_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_revisions.revision_id"),
            nullable=False,
        ),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("logical_name", sa.String(256), nullable=False),
        sa.Column("resource_id", sa.String(128), nullable=False),
        sa.Column("version_ref", sa.String(256), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("source_path", sa.String(1024), nullable=True),
        sa.Column("source_sha256", sa.String(64), nullable=True),
        sa.Column("provider_ref", sa.String(512), nullable=True),
        sa.Column(
            "snapshot_file_id",
            sa.String(64),
            sa.ForeignKey("caliber_workflow_files.file_id"),
            nullable=True,
        ),
        sa.Column("snapshot_sha256", sa.String(64), nullable=True),
        sa.Column("purpose", sa.String(32), nullable=False, server_default="runtime"),
        sa.Column("resolution", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "revision_id",
            "resource_type",
            "logical_name",
            name="uq_workspace_revision_resource_name",
        ),
    )
    op.create_index(
        "ix_workspace_revision_resources_revision",
        "caliber_workspace_revision_resources",
        ["revision_id"],
    )

    op.create_table(
        "caliber_workspace_import_jobs",
        sa.Column("import_job_id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("caliber_projects.project_id"),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_sources.source_id"),
            nullable=False,
        ),
        sa.Column("repository", sa.String(512), nullable=False),
        sa.Column("commit_sha", sa.String(128), nullable=False),
        sa.Column("upload_sha256", sa.String(64), nullable=True),
        sa.Column("source_bundle_sha256", sa.String(64), nullable=True),
        sa.Column(
            "source_snapshot_file_id",
            sa.String(64),
            sa.ForeignKey("caliber_workflow_files.file_id"),
            nullable=True,
        ),
        sa.Column("manifest_sha256", sa.String(64), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="queued"),
        sa.Column(
            "revision_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_revisions.revision_id"),
            nullable=True,
        ),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("claimed_by", sa.String(128), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(256), nullable=False, server_default=""),
        sa.Column("updated_by", sa.String(256), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'reconcile_required')",
            name="ck_workspace_import_status",
        ),
        sa.UniqueConstraint(
            "project_id", "idempotency_key", name="uq_workspace_import_project_idempotency"
        ),
    )
    op.create_index(
        "ix_workspace_import_jobs_project_status",
        "caliber_workspace_import_jobs",
        ["project_id", "status"],
    )
    op.create_index(
        "ix_workspace_import_jobs_lease",
        "caliber_workspace_import_jobs",
        ["status", "lease_expires_at"],
    )

    # 0093 deliberately left this reference unconstrained because its target
    # table did not exist. Add it only after the revision table is present.
    with op.batch_alter_table("caliber_projects") as batch:
        batch.create_foreign_key(
            "fk_projects_accepted_revision",
            "caliber_workspace_revisions",
            ["accepted_revision_id"],
            ["revision_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("caliber_projects") as batch:
        batch.drop_constraint("fk_projects_accepted_revision", type_="foreignkey")

    op.drop_index("ix_workspace_import_jobs_lease", table_name="caliber_workspace_import_jobs")
    op.drop_index(
        "ix_workspace_import_jobs_project_status", table_name="caliber_workspace_import_jobs"
    )
    op.drop_table("caliber_workspace_import_jobs")

    op.drop_index(
        "ix_workspace_revision_resources_revision",
        table_name="caliber_workspace_revision_resources",
    )
    op.drop_table("caliber_workspace_revision_resources")

    op.drop_index("ix_workspace_revisions_project_status", table_name="caliber_workspace_revisions")
    op.drop_table("caliber_workspace_revisions")

    op.drop_index("ix_workspace_sources_project_status", table_name="caliber_workspace_sources")
    op.drop_table("caliber_workspace_sources")
    op.drop_column("caliber_projects", "next_revision_number")
