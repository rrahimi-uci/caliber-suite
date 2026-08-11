"""add governed openapi integration drafts

Revision ID: 0087
Revises: 0086
Create Date: 2026-08-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0087"
down_revision: str | Sequence[str] | None = "0086"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_openapi_integrations",
        sa.Column("project_id", sa.String(64), nullable=True),
        sa.Column("visibility", sa.String(16), nullable=False, server_default="project"),
        sa.Column("integration_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("owner", sa.String(256), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("last_imported_version_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_openapi_integrations_status",
        "caliber_openapi_integrations",
        ["status"],
    )
    op.create_index(
        "ix_openapi_integrations_name",
        "caliber_openapi_integrations",
        ["name"],
    )

    op.create_table(
        "caliber_openapi_integration_versions",
        sa.Column("version_id", sa.String(64), primary_key=True),
        sa.Column(
            "integration_id",
            sa.String(64),
            sa.ForeignKey("caliber_openapi_integrations.integration_id"),
            nullable=False,
        ),
        sa.Column("source_kind", sa.String(32), nullable=False, server_default="inline_text"),
        sa.Column("source_ref", sa.String(1024), nullable=False, server_default=""),
        sa.Column("spec_sha256", sa.String(64), nullable=False),
        sa.Column("openapi_version", sa.String(16), nullable=False, server_default=""),
        sa.Column("title", sa.String(256), nullable=False, server_default=""),
        sa.Column("spec_version", sa.String(64), nullable=False, server_default=""),
        sa.Column("spec_description", sa.Text(), nullable=False, server_default=""),
        sa.Column("server_urls", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("auth_schemes", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("import_warnings", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("operation_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("raw_document", sa.JSON(), nullable=False),
        sa.Column("normalized_summary", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "integration_id",
            "spec_sha256",
            name="uq_openapi_integration_version_spec_sha256",
        ),
    )
    op.create_index(
        "ix_openapi_integration_versions_integration",
        "caliber_openapi_integration_versions",
        ["integration_id", "created_at"],
    )

    op.create_table(
        "caliber_openapi_operations",
        sa.Column("operation_id", sa.String(64), primary_key=True),
        sa.Column(
            "integration_version_id",
            sa.String(64),
            sa.ForeignKey("caliber_openapi_integration_versions.version_id"),
            nullable=False,
        ),
        sa.Column("operation_key", sa.String(1024), nullable=False),
        sa.Column("method", sa.String(16), nullable=False),
        sa.Column("path", sa.String(1024), nullable=False),
        sa.Column("spec_operation_id", sa.String(256), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("tags", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("deprecated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("side_effect_level", sa.String(16), nullable=False, server_default="read"),
        sa.Column("auth_schemes", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("request_body_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("request_content_types", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("response_statuses", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("normalized_operation", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "integration_version_id",
            "method",
            "path",
            name="uq_openapi_operation_version_method_path",
        ),
    )
    op.create_index(
        "ix_openapi_operations_version",
        "caliber_openapi_operations",
        ["integration_version_id"],
    )
    op.create_index(
        "ix_openapi_operations_operation_key",
        "caliber_openapi_operations",
        ["operation_key"],
    )


def downgrade() -> None:
    op.drop_index("ix_openapi_operations_operation_key", table_name="caliber_openapi_operations")
    op.drop_index("ix_openapi_operations_version", table_name="caliber_openapi_operations")
    op.drop_table("caliber_openapi_operations")

    op.drop_index(
        "ix_openapi_integration_versions_integration",
        table_name="caliber_openapi_integration_versions",
    )
    op.drop_table("caliber_openapi_integration_versions")

    op.drop_index("ix_openapi_integrations_name", table_name="caliber_openapi_integrations")
    op.drop_index("ix_openapi_integrations_status", table_name="caliber_openapi_integrations")
    op.drop_table("caliber_openapi_integrations")
