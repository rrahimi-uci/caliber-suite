"""add Workspace release and operation foundation

P5-A adds the durable Workspace release/evaluation/evidence/decision and
operation/item contracts.  The services that use this schema are provider-free
and do not yet modify environment pointers or invoke external systems.

Revision ID: 0102
Revises: 0101
Create Date: 2026-09-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0102"
down_revision: str | Sequence[str] | None = "0101"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_workspace_releases",
        sa.Column("release_id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("caliber_projects.project_id"),
            nullable=False,
        ),
        sa.Column(
            "revision_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_revisions.revision_id"),
            nullable=False,
        ),
        sa.Column(
            "environment_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_environments.environment_id"),
            nullable=False,
        ),
        sa.Column(
            "change_request_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_change_requests.change_request_id"),
            nullable=True,
        ),
        sa.Column(
            "change_request_head_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_change_request_heads.head_id"),
            nullable=True,
        ),
        sa.Column(
            "version_tag_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_version_tags.tag_id"),
            nullable=True,
        ),
        sa.Column(
            "predecessor_release_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_releases.release_id"),
            nullable=True,
        ),
        sa.Column("environment_config_sha256", sa.String(64), nullable=False),
        sa.Column("runtime_dependencies_sha256", sa.String(64), nullable=False),
        sa.Column("policy_sha256", sa.String(64), nullable=False),
        sa.Column("request_idempotency_key", sa.String(256), nullable=False),
        sa.Column("evaluation_evidence_sha256", sa.String(64), nullable=True),
        sa.Column("decision_set_sha256", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("requested_by", sa.String(256), nullable=False, server_default=""),
        sa.Column("requested_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("evaluated_by", sa.String(256), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(), nullable=True),
        sa.Column("lock_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_summary", sa.String(4000), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "project_id",
            "request_idempotency_key",
            name="uq_workspace_release_project_idempotency",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'evaluating', 'blocked', 'rejected', 'approved', "
            "'awaiting_quality_signoff', 'awaiting_approval')",
            name="ck_workspace_release_status",
        ),
        sa.CheckConstraint("lock_version >= 1", name="ck_workspace_release_lock_version"),
    )
    op.create_index(
        "ix_workspace_releases_project_status",
        "caliber_workspace_releases",
        ["project_id", "status"],
    )
    op.create_index(
        "ix_workspace_releases_environment_status",
        "caliber_workspace_releases",
        ["environment_id", "status"],
    )

    op.create_table(
        "caliber_workspace_release_evaluations",
        sa.Column("evaluation_id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("caliber_projects.project_id"),
            nullable=False,
        ),
        sa.Column(
            "workspace_release_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_releases.release_id"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("evaluation_plan_sha256", sa.String(64), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("claimed_by", sa.String(256), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("linked_evaluation_run_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("gate_verdict_id", sa.String(64), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_summary", sa.String(4000), nullable=True),
        sa.Column("requested_by", sa.String(256), nullable=False, server_default=""),
        sa.Column("requested_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("started_by", sa.String(256), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_by", sa.String(256), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "project_id", "idempotency_key", name="uq_workspace_release_eval_project_key"
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_workspace_release_evaluation_status",
        ),
        sa.CheckConstraint("attempt_number >= 1", name="ck_workspace_release_eval_attempt_number"),
    )
    op.create_index(
        "uq_workspace_release_eval_active",
        "caliber_workspace_release_evaluations",
        ["workspace_release_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('queued', 'running')"),
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index(
        "ix_workspace_release_evaluations_release_status",
        "caliber_workspace_release_evaluations",
        ["workspace_release_id", "status"],
    )
    op.create_index(
        "ix_workspace_release_evaluations_lease",
        "caliber_workspace_release_evaluations",
        ["status", "lease_expires_at"],
    )

    op.create_table(
        "caliber_workspace_release_evidence",
        sa.Column("evidence_id", sa.String(64), primary_key=True),
        sa.Column(
            "workspace_release_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_releases.release_id"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("evidence_ref", sa.String(512), nullable=False),
        sa.Column("evidence_sha256", sa.String(64), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("recorded_by", sa.String(256), nullable=False, server_default=""),
        sa.Column("recorded_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "workspace_release_id",
            "kind",
            "evidence_ref",
            name="uq_workspace_release_evidence_ref",
        ),
        sa.CheckConstraint(
            "kind IN ('evaluation_run', 'gate_verdict', 'release_candidate', "
            "'config_snapshot', 'provider_preflight')",
            name="ck_workspace_release_evidence_kind",
        ),
    )
    op.create_index(
        "ix_workspace_release_evidence_release",
        "caliber_workspace_release_evidence",
        ["workspace_release_id", "kind"],
    )

    op.create_table(
        "caliber_workspace_release_decisions",
        sa.Column("decision_id", sa.String(64), primary_key=True),
        sa.Column(
            "workspace_release_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_releases.release_id"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("decision", sa.String(8), nullable=False),
        sa.Column("rationale", sa.String(4000), nullable=False),
        sa.Column("decided_by", sa.String(256), nullable=False),
        sa.Column("actor_role_snapshot", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("effective_scope_snapshot", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("revision_sha256", sa.String(64), nullable=False),
        sa.Column("environment_config_sha256", sa.String(64), nullable=False),
        sa.Column("runtime_dependencies_sha256", sa.String(64), nullable=False),
        sa.Column("gate_evidence_sha256", sa.String(64), nullable=False),
        sa.Column("policy_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "workspace_release_id", "kind", name="uq_workspace_release_decision_kind"
        ),
        sa.CheckConstraint(
            "kind IN ('quality', 'release')", name="ck_workspace_release_decision_kind"
        ),
        sa.CheckConstraint(
            "decision IN ('go', 'no_go')", name="ck_workspace_release_decision_value"
        ),
    )
    op.create_index(
        "ix_workspace_release_decisions_release",
        "caliber_workspace_release_decisions",
        ["workspace_release_id", "kind"],
    )

    op.create_table(
        "caliber_workspace_break_glass_authorizations",
        sa.Column("authorization_id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("caliber_projects.project_id"),
            nullable=False,
        ),
        sa.Column(
            "workspace_release_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_releases.release_id"),
            nullable=False,
        ),
        sa.Column(
            "environment_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_environments.environment_id"),
            nullable=False,
        ),
        sa.Column("reason", sa.String(4000), nullable=False),
        sa.Column("incident_ref", sa.String(256), nullable=False),
        sa.Column("authorization_ref", sa.String(256), nullable=False),
        sa.Column("authorized_by", sa.String(256), nullable=False),
        sa.Column("credential_kind", sa.String(64), nullable=False),
        sa.Column("credential_id", sa.String(256), nullable=False),
        sa.Column("revision_sha256", sa.String(64), nullable=False),
        sa.Column("environment_config_sha256", sa.String(64), nullable=False),
        sa.Column("runtime_dependencies_sha256", sa.String(64), nullable=False),
        sa.Column("gate_evidence_sha256", sa.String(64), nullable=False),
        sa.Column("policy_sha256", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("expires_at > created_at", name="ck_workspace_break_glass_expiry"),
    )
    op.create_index(
        "ix_workspace_break_glass_release",
        "caliber_workspace_break_glass_authorizations",
        ["workspace_release_id"],
    )

    op.create_table(
        "caliber_workspace_release_operations",
        sa.Column("operation_id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("caliber_projects.project_id"),
            nullable=False,
        ),
        sa.Column(
            "workspace_release_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_releases.release_id"),
            nullable=False,
        ),
        sa.Column(
            "environment_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_environments.environment_id"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column(
            "target_release_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_releases.release_id"),
            nullable=True,
        ),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("expected_current_release_id", sa.String(64), nullable=True),
        sa.Column("expected_environment_lock_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="prepared"),
        sa.Column("lock_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("requested_by", sa.String(256), nullable=False, server_default=""),
        sa.Column("requested_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("applied_by", sa.String(256), nullable=True),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("completed_by", sa.String(256), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("observation_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_observed_at", sa.DateTime(), nullable=True),
        sa.Column(
            "break_glass_authorization_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_break_glass_authorizations.authorization_id"),
            nullable=True,
            unique=True,
        ),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_summary", sa.String(4000), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "project_id",
            "kind",
            "idempotency_key",
            name="uq_workspace_release_operation_project_key",
        ),
        sa.CheckConstraint(
            "kind IN ('apply', 'rollback')", name="ck_workspace_release_operation_kind"
        ),
        sa.CheckConstraint(
            "status IN ('prepared', 'applying', 'applied', 'failed', "
            "'reconcile_required', 'cancelled')",
            name="ck_workspace_release_operation_status",
        ),
        sa.CheckConstraint(
            "kind = 'rollback' OR target_release_id IS NULL",
            name="ck_workspace_release_operation_rollback_target",
        ),
        sa.CheckConstraint(
            "kind <> 'rollback' OR target_release_id IS NOT NULL",
            name="ck_workspace_release_operation_rollback_target_required",
        ),
        sa.CheckConstraint(
            "target_release_id IS NULL OR target_release_id <> workspace_release_id",
            name="ck_workspace_release_operation_target_prior",
        ),
        sa.CheckConstraint(
            "expected_environment_lock_version >= 1",
            name="ck_workspace_operation_expected_lock",
        ),
        sa.CheckConstraint("lock_version >= 1", name="ck_workspace_release_operation_lock_version"),
        sa.CheckConstraint(
            "observation_count >= 0", name="ck_workspace_release_operation_observations"
        ),
    )
    op.create_index(
        "uq_workspace_release_operation_environment_active",
        "caliber_workspace_release_operations",
        ["environment_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('prepared', 'applying', 'reconcile_required')"),
        postgresql_where=sa.text("status IN ('prepared', 'applying', 'reconcile_required')"),
    )
    op.create_index(
        "ix_workspace_release_operations_release_status",
        "caliber_workspace_release_operations",
        ["workspace_release_id", "status"],
    )

    op.create_table(
        "caliber_workspace_release_operation_items",
        sa.Column("operation_item_id", sa.String(64), primary_key=True),
        sa.Column(
            "workspace_release_operation_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_release_operations.operation_id"),
            nullable=False,
        ),
        sa.Column(
            "revision_resource_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_revision_resources.resource_pin_id"),
            nullable=False,
        ),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("target_ref", sa.String(512), nullable=False),
        sa.Column("before_ref", sa.String(512), nullable=True),
        sa.Column("after_ref", sa.String(512), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="prepared"),
        sa.Column("provider_operation_ref", sa.String(512), nullable=True),
        sa.Column("provider_result", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_summary", sa.String(4000), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "workspace_release_operation_id",
            "revision_resource_id",
            "target_ref",
            name="uq_workspace_release_operation_item_target",
        ),
        sa.CheckConstraint(
            "action IN ('no_op', 'bind', 'promote', 'activate', 'publish', 'verify')",
            name="ck_workspace_release_operation_item_action",
        ),
        sa.CheckConstraint(
            "status IN ('prepared', 'applying', 'applied', 'failed', "
            "'reconcile_required', 'rolled_back')",
            name="ck_workspace_release_operation_item_status",
        ),
    )
    op.create_index(
        "ix_workspace_release_operation_items_operation",
        "caliber_workspace_release_operation_items",
        ["workspace_release_operation_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workspace_release_operation_items_operation",
        table_name="caliber_workspace_release_operation_items",
    )
    op.drop_table("caliber_workspace_release_operation_items")
    op.drop_index(
        "ix_workspace_release_operations_release_status",
        table_name="caliber_workspace_release_operations",
    )
    op.drop_index(
        "uq_workspace_release_operation_environment_active",
        table_name="caliber_workspace_release_operations",
    )
    op.drop_table("caliber_workspace_release_operations")
    op.drop_index(
        "ix_workspace_break_glass_release",
        table_name="caliber_workspace_break_glass_authorizations",
    )
    op.drop_table("caliber_workspace_break_glass_authorizations")
    op.drop_index(
        "ix_workspace_release_decisions_release",
        table_name="caliber_workspace_release_decisions",
    )
    op.drop_table("caliber_workspace_release_decisions")
    op.drop_index(
        "ix_workspace_release_evidence_release",
        table_name="caliber_workspace_release_evidence",
    )
    op.drop_table("caliber_workspace_release_evidence")
    op.drop_index(
        "ix_workspace_release_evaluations_lease",
        table_name="caliber_workspace_release_evaluations",
    )
    op.drop_index(
        "ix_workspace_release_evaluations_release_status",
        table_name="caliber_workspace_release_evaluations",
    )
    op.drop_index(
        "uq_workspace_release_eval_active",
        table_name="caliber_workspace_release_evaluations",
    )
    op.drop_table("caliber_workspace_release_evaluations")
    op.drop_index(
        "ix_workspace_releases_environment_status",
        table_name="caliber_workspace_releases",
    )
    op.drop_index(
        "ix_workspace_releases_project_status",
        table_name="caliber_workspace_releases",
    )
    op.drop_table("caliber_workspace_releases")
