"""assistant context attachments

Revision ID: 0050
Revises: 0049
Create Date: 2026-06-20

Adds ``caliber_assistant_attachments`` — per-session context the user attaches to
Aria (the Caliber Assistant) so the engine can ground its replies, mirroring a
code assistant's "+ add files" affordance. Each row captures one piece of context
of a given ``kind``:

* ``object_file``     — a file already in the object store (bucket + key);
* ``upload``          — a file uploaded directly through the Aria panel (stored
                        to the object store, then referenced like ``object_file``);
* ``library_resource``— an existing CALIBER asset (prompt / skill / tool /
                        workflow / knowledge_base) referenced by id;
* ``text_snippet``    — a freeform pasted text block.

``content_text`` holds a capped, plain-text snapshot resolved at attach time so
prompt injection is uniform across kinds and survives later edits to the source.
``metadata`` carries kind-specific pointers (bucket/key, resource version, size).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0050"
down_revision: str | Sequence[str] | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_assistant_attachments",
        sa.Column("attachment_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=64),
            sa.ForeignKey("caliber_assistant_sessions.session_id"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("ref_type", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("ref_id", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("name", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("content_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("bytes_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_by", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_asst_attachment_session",
        "caliber_assistant_attachments",
        ["session_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_asst_attachment_session",
        table_name="caliber_assistant_attachments",
    )
    op.drop_table("caliber_assistant_attachments")
