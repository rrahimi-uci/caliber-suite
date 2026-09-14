"""add project-scoping lookup indexes (isolation closure, item 3)

`P2` (docs/workspace-plan.md Phase 2 item 3): a machine-readable pass over
`db/resource_inventory.py`'s classification found 11 "visibility" tables
(`project_id` + `visibility`, queried everywhere through
`db/scoping.py::apply_visibility_filter`'s `project_id = ? AND visibility =
'project'` predicate) and 4 "project_only" run/file tables (`project_id`
alone, no `visibility` column) with no index at all covering `project_id` --
every one of today's project-scoped list/detail queries on these tables was
a full table scan filtered in memory. Purely additive: new indexes only, no
column changes, no backfill, no `ForeignKey` added on any `project_id`
column -- that remains explicitly deferred (`db/legacy_data_report.py`'s
`orphan_project_id_report`, not yet written) until an orphan-reference audit
exists, since an FK on a column already carrying stray/legacy values would
fail to apply or silently corrupt data on the affected rows. A handful of
tables in the same "visibility" family already carried a composite index
leading with `project_id` (`caliber_knowledge_bases`, `caliber_release_
candidates`, `caliber_workflow_benchmark_reports`) and are left untouched.

Revision ID: 0095
Revises: 0094
Create Date: 2026-09-14
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0095"
down_revision: str | Sequence[str] | None = "0094"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (table, index_name) for the 11 "visibility"-shaped tables gaining a
#: `(project_id, visibility)` composite index.
_VISIBILITY_INDEXES = [
    ("caliber_agent_config", "ix_agent_config_project_visibility"),
    ("caliber_aria_plans", "ix_aria_plans_project_visibility"),
    ("caliber_eval_datasets", "ix_eval_datasets_project_visibility"),
    ("caliber_eval_runs", "ix_eval_runs_project_visibility"),
    ("caliber_judges", "ix_judges_project_visibility"),
    ("caliber_llm_model_pricing", "ix_llm_model_pricing_project_visibility"),
    ("caliber_openapi_integrations", "ix_openapi_integrations_project_visibility"),
    ("caliber_review_queues", "ix_review_queues_project_visibility"),
    ("caliber_skills", "ix_skills_project_visibility"),
    ("caliber_tool_registry", "ix_tool_registry_project_visibility"),
    ("caliber_workflows", "ix_workflows_project_visibility"),
]

#: (table, index_name) for the 4 "project_only" run/file tables gaining a
#: plain `(project_id,)` index.
_PROJECT_ONLY_INDEXES = [
    ("caliber_workflow_runs", "ix_workflow_runs_project"),
    ("caliber_workflow_run_events", "ix_workflow_run_events_project"),
    ("caliber_workflow_run_checkpoints", "ix_workflow_run_checkpoints_project"),
    ("caliber_workflow_files", "ix_workflow_files_project"),
]


def upgrade() -> None:
    for table, name in _VISIBILITY_INDEXES:
        op.create_index(name, table, ["project_id", "visibility"])
    for table, name in _PROJECT_ONLY_INDEXES:
        op.create_index(name, table, ["project_id"])


def downgrade() -> None:
    for table, name in [*_VISIBILITY_INDEXES, *_PROJECT_ONLY_INDEXES]:
        op.drop_index(name, table_name=table)
