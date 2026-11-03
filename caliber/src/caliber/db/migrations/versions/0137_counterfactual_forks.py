"""add isolated counterfactual fork metadata to workflow runs

Revision ID: 0137
Revises: 0136
Create Date: 2026-11-03

B09.5 stores the source-run relationship and the separately authorized fork
contract on the new run.  Both columns are nullable so existing runs retain
their exact read behavior.  The runner is deployed after this readers-first
schema change and writes only new rows; no backfill is required.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0137"
down_revision: str | Sequence[str] | None = "0136"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def _drop_sqlite_run_triggers() -> None:
    """Temporarily remove triggers that reference the table batch mode replaces."""
    if not _is_sqlite():
        return
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_caliber_workflow_runs_legacy_flag"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_caliber_run_manifests_not_legacy"))


def _restore_sqlite_run_triggers() -> None:
    if not _is_sqlite():
        return
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_caliber_run_manifests_not_legacy "
            "BEFORE INSERT ON caliber_run_manifests "
            "WHEN NOT EXISTS (SELECT 1 FROM caliber_workflow_runs "
            "WHERE workflow_run_id = NEW.workflow_run_id AND manifest_legacy = 0) "
            "BEGIN SELECT RAISE(ABORT, 'a manifest needs an existing, non-legacy run'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_caliber_workflow_runs_legacy_flag "
            "BEFORE UPDATE OF manifest_legacy ON caliber_workflow_runs "
            "WHEN OLD.manifest_legacy IS NOT NEW.manifest_legacy "
            "BEGIN SELECT RAISE(ABORT, "
            "'caliber_workflow_runs.manifest_legacy is write-once'); END"
        )
    )


def upgrade() -> None:
    # SQLite cannot ALTER TABLE ADD a foreign-key constraint. Batch mode keeps
    # the migration executable on the development dialect while emitting the
    # ordinary additive ALTERs on PostgreSQL.
    _drop_sqlite_run_triggers()
    try:
        with op.batch_alter_table("caliber_workflow_runs") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "source_run_id",
                    sa.String(length=64),
                    sa.ForeignKey(
                        "caliber_workflow_runs.workflow_run_id",
                        name="fk_workflow_runs_counterfactual_source",
                    ),
                    nullable=True,
                )
            )
            batch_op.add_column(sa.Column("counterfactual_spec", sa.JSON(), nullable=True))
    finally:
        _restore_sqlite_run_triggers()
    op.create_index(
        "ix_workflow_runs_counterfactual_source",
        "caliber_workflow_runs",
        ["source_run_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workflow_runs_counterfactual_source",
        table_name="caliber_workflow_runs",
    )
    _drop_sqlite_run_triggers()
    try:
        with op.batch_alter_table("caliber_workflow_runs") as batch_op:
            batch_op.drop_column("counterfactual_spec")
            batch_op.drop_column("source_run_id")
    finally:
        _restore_sqlite_run_triggers()
