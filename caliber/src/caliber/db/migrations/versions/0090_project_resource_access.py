"""add project memberships and resource access roles

Revision ID: 0090
Revises: 0089
Create Date: 2026-08-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0090"
down_revision: str | Sequence[str] | None = "0089"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_project_members",
        sa.Column("member_id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("caliber_projects.project_id"),
            nullable=False,
        ),
        sa.Column("user_id", sa.String(256), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="viewer"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_by", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint("project_id", "user_id", name="uq_project_member_user"),
    )
    op.create_index(
        "ix_project_members_project_status",
        "caliber_project_members",
        ["project_id", "status"],
    )
    op.create_index(
        "ix_project_members_user_status",
        "caliber_project_members",
        ["user_id", "status"],
    )
    # Existing projects already have an owner. Materialize that relationship so
    # the new membership API has one consistent source of truth after upgrade.
    op.execute(
        sa.text(
            "INSERT INTO caliber_project_members "
            "(member_id, project_id, user_id, role, status, created_by) "
            "SELECT 'PRJM-' || project_id, project_id, owner, 'owner', 'active', owner "
            "FROM caliber_projects WHERE owner <> ''"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_project_members_user_status", table_name="caliber_project_members")
    op.drop_index("ix_project_members_project_status", table_name="caliber_project_members")
    op.drop_table("caliber_project_members")
