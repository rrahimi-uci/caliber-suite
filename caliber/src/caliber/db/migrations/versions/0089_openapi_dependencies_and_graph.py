"""add openapi operation dependencies, graph snapshot, and tool packs

Revision ID: 0089
Revises: 0088
Create Date: 2026-08-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0089"
down_revision: str | Sequence[str] | None = "0088"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "caliber_openapi_integration_versions",
        sa.Column("graph_snapshot", sa.JSON(), nullable=True),
    )
    op.add_column(
        "caliber_openapi_integration_versions",
        sa.Column("dependency_detected_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "caliber_openapi_tool_drafts",
        sa.Column(
            "additional_operation_ids",
            sa.JSON(),
            nullable=False,
            server_default="[]",
        ),
    )

    op.create_table(
        "caliber_openapi_operation_dependencies",
        sa.Column("dependency_id", sa.String(64), primary_key=True),
        sa.Column(
            "integration_version_id",
            sa.String(64),
            sa.ForeignKey("caliber_openapi_integration_versions.version_id"),
            nullable=False,
        ),
        sa.Column(
            "from_operation_id",
            sa.String(64),
            sa.ForeignKey("caliber_openapi_operations.operation_id"),
            nullable=False,
        ),
        sa.Column(
            "to_operation_id",
            sa.String(64),
            sa.ForeignKey("caliber_openapi_operations.operation_id"),
            nullable=False,
        ),
        sa.Column("dependency_type", sa.String(32), nullable=False),
        sa.Column("confidence", sa.String(16), nullable=False, server_default="medium"),
        sa.Column("source", sa.String(32), nullable=False, server_default="rule_inference"),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("binding_field_map", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="suggested"),
        sa.Column("confirmed_by", sa.String(256), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        # Inlined into create_table rather than a follow-up create_unique_constraint:
        # SQLite has no ALTER support for adding a constraint to an existing table
        # outside of batch mode, so the constraint must be present at creation time.
        sa.UniqueConstraint(
            "integration_version_id",
            "from_operation_id",
            "to_operation_id",
            "dependency_type",
            name="uq_openapi_dependency_edge",
        ),
    )
    op.create_index(
        "ix_openapi_dependencies_version",
        "caliber_openapi_operation_dependencies",
        ["integration_version_id"],
    )
    op.create_index(
        "ix_openapi_dependencies_from",
        "caliber_openapi_operation_dependencies",
        ["from_operation_id"],
    )
    op.create_index(
        "ix_openapi_dependencies_to",
        "caliber_openapi_operation_dependencies",
        ["to_operation_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_openapi_dependencies_to", table_name="caliber_openapi_operation_dependencies"
    )
    op.drop_index(
        "ix_openapi_dependencies_from", table_name="caliber_openapi_operation_dependencies"
    )
    op.drop_index(
        "ix_openapi_dependencies_version", table_name="caliber_openapi_operation_dependencies"
    )
    op.drop_table("caliber_openapi_operation_dependencies")

    op.drop_column("caliber_openapi_tool_drafts", "additional_operation_ids")
    op.drop_column("caliber_openapi_integration_versions", "dependency_detected_at")
    op.drop_column("caliber_openapi_integration_versions", "graph_snapshot")
