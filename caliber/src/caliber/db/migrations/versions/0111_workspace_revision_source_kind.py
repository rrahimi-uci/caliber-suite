"""add ``source_kind`` discriminator to Workspace revisions for managed snapshots

`P4-B`/`P4-C` (docs/workspace-plan.md `P0-B` item 9, "per-adapter
reconstructability strategy"): ``POST /projects/{id}/revisions:snapshot``
(the never-built route named by section 12.2's API table) captures a
project's *current live CALIBER resource state* as an immutable revision,
independent of the Git-import path ``workspace_import_materializer.py``
already owns. A managed snapshot has no Git commit and no uploaded source
bundle, but ``caliber_workspace_revisions.manifest_sha256``/
``source_bundle_sha256`` were both ``NOT NULL`` -- both are Git-import
concepts (the digest of the committed manifest file, the digest of the
uploaded ZIP) with no managed equivalent, so representing a source-less
revision needs a schema decision, not a workaround.

Decision: add a ``source_kind`` discriminator column (``'git'`` |
``'managed'``, matching every other closed-vocabulary column in this table)
and make both digest columns nullable, tied to the discriminator by a CHECK
constraint mirroring migration `0106`'s ``ck_rework_task_exactly_one_source``
precedent -- a git-sourced revision must still carry both digests exactly as
before (no behavior change for the only kind that exists today), and a
managed revision must carry neither. ``revision_sha256`` (already NOT NULL,
already the canonical content digest the import materializer computes over
its own package descriptor) remains the single integrity anchor for *both*
kinds; a managed revision's descriptor is simply a different, smaller
ingredient list (no source/commit/bundle fields) computed by the new
``routes/workspace.py::snapshot_revision`` route, not by this migration.

Existing rows are unaffected: ``source_kind`` backfills to ``'git'`` (the
only kind ever written), which already satisfies the new CHECK constraint
since every existing row already has both digests populated.

Revision ID: 0111
Revises: 0110
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0111"
down_revision: str | Sequence[str] | None = "0110"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DIGEST_CHECK_NAME = "ck_workspace_revision_source_kind_digest"
_KIND_CHECK_NAME = "ck_workspace_revision_source_kind"


def upgrade() -> None:
    with op.batch_alter_table("caliber_workspace_revisions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "source_kind", sa.String(length=16), nullable=False, server_default="git"
            )
        )
        batch_op.alter_column(
            "manifest_sha256",
            existing_type=sa.String(length=64),
            nullable=True,
        )
        batch_op.alter_column(
            "source_bundle_sha256",
            existing_type=sa.String(length=64),
            nullable=True,
        )
        batch_op.create_check_constraint(
            _KIND_CHECK_NAME,
            "source_kind IN ('git', 'managed')",
        )
        batch_op.create_check_constraint(
            _DIGEST_CHECK_NAME,
            "(source_kind = 'git' AND manifest_sha256 IS NOT NULL "
            "AND source_bundle_sha256 IS NOT NULL) "
            "OR (source_kind = 'managed' AND manifest_sha256 IS NULL "
            "AND source_bundle_sha256 IS NULL)",
        )


def downgrade() -> None:
    # Symmetric with 0106's downgrade policy: this assumes no managed-kind
    # revision exists yet. A managed row would violate the restored NOT NULL
    # constraints on manifest_sha256/source_bundle_sha256 -- downgrading past
    # this revision once one exists requires an operational decision
    # (delete or backfill fabricated digests for the managed rows), not an
    # automated one, exactly as 0106's downgrade already documents for its
    # own analogous exactly-one-source column pair.
    with op.batch_alter_table("caliber_workspace_revisions") as batch_op:
        batch_op.drop_constraint(_DIGEST_CHECK_NAME, type_="check")
        batch_op.drop_constraint(_KIND_CHECK_NAME, type_="check")
        batch_op.alter_column(
            "source_bundle_sha256",
            existing_type=sa.String(length=64),
            nullable=False,
        )
        batch_op.alter_column(
            "manifest_sha256",
            existing_type=sa.String(length=64),
            nullable=False,
        )
        batch_op.drop_column("source_kind")
