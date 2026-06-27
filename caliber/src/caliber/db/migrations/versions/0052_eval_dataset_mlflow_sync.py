"""eval dataset → MLflow GenAI dataset sync linkage

Revision ID: 0052
Revises: 0051
Create Date: 2026-06-21

Adds nullable MLflow-sync columns to ``caliber_eval_datasets``. CALIBER stays the
source of truth for test sets; these columns record the last push to MLflow 3.14's
native ``mlflow.genai.datasets`` registry so the revamped dataset UI and
source-trace lineage become available without migrating the data out of Postgres:

* ``mlflow_dataset_id``      — the MLflow EvaluationDataset id (``d-...``);
* ``mlflow_synced_at``       — when the last push happened;
* ``mlflow_synced_version``  — the CALIBER ``version`` captured at sync time, so
                               the UI can flag "synced, but changed since";
* ``mlflow_record_count``    — number of records pushed;
* ``mlflow_digest``          — MLflow's content digest of the synced records.

All nullable / no server default — a dataset that has never been synced has them
all null. This is a pure additive column migration (safe, reversible).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0052"
down_revision: str | Sequence[str] | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "caliber_eval_datasets"
_COLUMN_NAMES = (
    "mlflow_dataset_id",
    "mlflow_synced_at",
    "mlflow_synced_version",
    "mlflow_record_count",
    "mlflow_digest",
)


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("mlflow_dataset_id", sa.String(length=64), nullable=True))
    op.add_column(_TABLE, sa.Column("mlflow_synced_at", sa.DateTime(), nullable=True))
    op.add_column(_TABLE, sa.Column("mlflow_synced_version", sa.Integer(), nullable=True))
    op.add_column(_TABLE, sa.Column("mlflow_record_count", sa.Integer(), nullable=True))
    op.add_column(_TABLE, sa.Column("mlflow_digest", sa.String(length=64), nullable=True))


def downgrade() -> None:
    for name in reversed(_COLUMN_NAMES):
        op.drop_column(_TABLE, name)
