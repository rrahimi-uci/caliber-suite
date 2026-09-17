"""add Workspace Change Request and review history

P4-D foundation: persist append-only Change Request heads, reviewer decisions,
checks, normalized provider attestations, and immutable semantic-version claims
and tags. Public acceptance remains a service-only primitive until Phase 5
provides QA release evidence.

Revision ID: 0100
Revises: 0099
Create Date: 2026-09-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0100"
down_revision: str | Sequence[str] | None = "0099"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_workspace_change_requests",
        sa.Column("change_request_id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id", sa.String(64), sa.ForeignKey("caliber_projects.project_id"), nullable=False
        ),
        sa.Column(
            "base_revision_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_revisions.revision_id"),
            nullable=True,
        ),
        sa.Column(
            "current_head_revision_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_revisions.revision_id"),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(256), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("head_generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(24), nullable=False, server_default="draft"),
        sa.Column("review_backend", sa.String(24), nullable=False, server_default="caliber"),
        sa.Column("accepted_at", sa.DateTime(), nullable=True),
        sa.Column("accepted_by", sa.String(256), nullable=True),
        sa.Column("closed_reason", sa.Text(), nullable=True),
        sa.Column("lock_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('draft', 'open', 'changes_requested', 'technically_approved', "
            "'qa_in_progress', 'out_of_date', 'accepted', 'closed')",
            name="ck_workspace_change_request_status",
        ),
        sa.CheckConstraint(
            "review_backend IN ('caliber', 'source_provider')",
            name="ck_workspace_change_request_review_backend",
        ),
        sa.CheckConstraint("head_generation >= 1", name="ck_workspace_change_request_generation"),
        sa.CheckConstraint("lock_version >= 1", name="ck_workspace_change_request_lock_version"),
    )
    op.create_index(
        "ix_workspace_change_requests_project_status",
        "caliber_workspace_change_requests",
        ["project_id", "status"],
    )

    op.create_table(
        "caliber_workspace_change_request_heads",
        sa.Column("head_id", sa.String(64), primary_key=True),
        sa.Column(
            "change_request_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_change_requests.change_request_id"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column(
            "revision_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_revisions.revision_id"),
            nullable=False,
        ),
        sa.Column("revision_sha256", sa.String(64), nullable=False),
        sa.Column("review_policy_version", sa.String(64), nullable=False, server_default="v1"),
        sa.Column("review_policy_sha256", sa.String(64), nullable=False, server_default=""),
        sa.Column("changed_by", sa.String(256), nullable=False),
        sa.Column("change_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "change_request_id", "generation", name="uq_workspace_change_request_head_generation"
        ),
    )
    op.create_index(
        "ix_workspace_change_request_heads_request",
        "caliber_workspace_change_request_heads",
        ["change_request_id", "generation"],
    )

    op.create_table(
        "caliber_workspace_change_request_reviewers",
        sa.Column("reviewer_id", sa.String(64), primary_key=True),
        sa.Column(
            "change_request_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_change_requests.change_request_id"),
            nullable=False,
        ),
        sa.Column("user_id", sa.String(256), nullable=False),
        sa.Column("assigned_by", sa.String(256), nullable=False),
        sa.Column("assigned_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("removed_by", sa.String(256), nullable=True),
        sa.Column("removed_at", sa.DateTime(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
    )
    op.create_index(
        "ix_workspace_change_request_reviewers_request",
        "caliber_workspace_change_request_reviewers",
        ["change_request_id", "active"],
    )
    op.create_index(
        "uq_workspace_change_request_active_reviewer",
        "caliber_workspace_change_request_reviewers",
        ["change_request_id", "user_id"],
        unique=True,
        sqlite_where=sa.text("active = 1"),
        postgresql_where=sa.text("active = true"),
    )

    op.create_table(
        "caliber_workspace_change_request_comments",
        sa.Column("comment_id", sa.String(64), primary_key=True),
        sa.Column(
            "change_request_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_change_requests.change_request_id"),
            nullable=False,
        ),
        sa.Column(
            "head_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_change_request_heads.head_id"),
            nullable=True,
        ),
        sa.Column("resource_type", sa.String(64), nullable=True),
        sa.Column("resource_name", sa.String(256), nullable=True),
        sa.Column("source_path", sa.String(1024), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("author", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_workspace_change_request_comments_request",
        "caliber_workspace_change_request_comments",
        ["change_request_id", "created_at"],
    )

    op.create_table(
        "caliber_workspace_change_request_checks",
        sa.Column("check_id", sa.String(64), primary_key=True),
        sa.Column(
            "head_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_change_request_heads.head_id"),
            nullable=False,
        ),
        sa.Column("check_name", sa.String(128), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("implementation_version", sa.String(64), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("evidence_ref", sa.String(512), nullable=True),
        sa.Column("evidence_digest", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("claimed_by", sa.String(256), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("head_id", "check_name", "attempt_number", name="uq_workspace_check_attempt"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'passed', 'failed', 'cancelled')",
            name="ck_workspace_change_request_check_status",
        ),
        sa.CheckConstraint("attempt_number >= 1", name="ck_workspace_change_request_check_attempt"),
    )
    op.create_index(
        "ix_workspace_change_request_checks_head",
        "caliber_workspace_change_request_checks",
        ["head_id", "created_at"],
    )
    op.create_index(
        "uq_workspace_change_request_active_check",
        "caliber_workspace_change_request_checks",
        ["head_id", "check_name"],
        unique=True,
        sqlite_where=sa.text("status IN ('queued', 'running')"),
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )

    op.create_table(
        "caliber_workspace_change_request_reviews",
        sa.Column("review_id", sa.String(64), primary_key=True),
        sa.Column(
            "change_request_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_change_requests.change_request_id"),
            nullable=False,
        ),
        sa.Column(
            "head_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_change_request_heads.head_id"),
            nullable=False,
        ),
        sa.Column(
            "reviewer_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_change_request_reviewers.reviewer_id"),
            nullable=False,
        ),
        sa.Column("decision", sa.String(24), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column("actor_role", sa.String(32), nullable=False),
        sa.Column("actor_scopes", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "decision IN ('approve', 'request_changes')",
            name="ck_workspace_change_request_review_decision",
        ),
    )
    op.create_index(
        "ix_workspace_change_request_reviews_request",
        "caliber_workspace_change_request_reviews",
        ["change_request_id", "created_at"],
    )

    op.create_table(
        "caliber_workspace_external_review_attestations",
        sa.Column("attestation_id", sa.String(64), primary_key=True),
        sa.Column(
            "change_request_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_change_requests.change_request_id"),
            nullable=False,
        ),
        sa.Column(
            "head_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_change_request_heads.head_id"),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_sources.source_id"),
            nullable=False,
        ),
        sa.Column("provider_change_request_id", sa.String(256), nullable=False),
        sa.Column("provider_url", sa.String(1024), nullable=True),
        sa.Column("provider_head_commit", sa.String(128), nullable=False),
        sa.Column("provider_resulting_commit", sa.String(128), nullable=False),
        sa.Column("source_tree_sha256", sa.String(64), nullable=False),
        sa.Column("workspace_revision_sha256", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("policy_sha256", sa.String(64), nullable=False),
        sa.Column("provider_ruleset_sha256", sa.String(64), nullable=True),
        sa.Column("required_checks", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("trusted_check_sources", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("check_conclusions", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("review_actors", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("merge_method", sa.String(32), nullable=True),
        sa.Column("merge_actor", sa.String(256), nullable=True),
        sa.Column("merged_at", sa.DateTime(), nullable=True),
        sa.Column("provider_event_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("adapter_version", sa.String(64), nullable=False),
        sa.Column("verified_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("verification_input_digest", sa.String(64), nullable=False),
        sa.Column("coverage_digest", sa.String(64), nullable=True),
        sa.Column("uncovered_commits", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("uncovered_paths", sa.JSON(), nullable=False, server_default="[]"),
        sa.UniqueConstraint(
            "change_request_id",
            "head_id",
            "provider_change_request_id",
            "provider_resulting_commit",
            "verification_input_digest",
            name="uq_workspace_external_attestation_input",
        ),
        sa.CheckConstraint(
            "status IN ('verified', 'insufficient', 'stale', 'revoked')",
            name="ck_workspace_external_attestation_status",
        ),
    )
    op.create_index(
        "ix_workspace_external_attestations_head",
        "caliber_workspace_external_review_attestations",
        ["head_id", "status"],
    )

    op.create_table(
        "caliber_workspace_version_claims",
        sa.Column("claim_id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id", sa.String(64), sa.ForeignKey("caliber_projects.project_id"), nullable=False
        ),
        sa.Column(
            "change_request_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_change_requests.change_request_id"),
            nullable=False,
        ),
        sa.Column("semantic_version", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="reserved"),
        sa.Column("claimed_by", sa.String(256), nullable=False),
        sa.Column("claimed_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("accepted_at", sa.DateTime(), nullable=True),
        sa.Column("abandoned_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("project_id", "semantic_version", name="uq_workspace_version_claim_version"),
        sa.CheckConstraint(
            "status IN ('reserved', 'accepted', 'abandoned')",
            name="ck_workspace_version_claim_status",
        ),
    )
    op.create_index(
        "uq_workspace_reserved_version_claim_request",
        "caliber_workspace_version_claims",
        ["change_request_id"],
        unique=True,
        sqlite_where=sa.text("status = 'reserved'"),
        postgresql_where=sa.text("status = 'reserved'"),
    )

    op.create_table(
        "caliber_workspace_version_tags",
        sa.Column("tag_id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id", sa.String(64), sa.ForeignKey("caliber_projects.project_id"), nullable=False
        ),
        sa.Column(
            "revision_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_revisions.revision_id"),
            nullable=False,
        ),
        sa.Column(
            "change_request_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_change_requests.change_request_id"),
            nullable=False,
        ),
        sa.Column("tag", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("created_by", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("project_id", "tag", name="uq_workspace_version_tag"),
        sa.CheckConstraint(
            "kind IN ('qa_candidate', 'accepted')", name="ck_workspace_version_tag_kind"
        ),
    )


def downgrade() -> None:
    op.drop_table("caliber_workspace_version_tags")
    op.drop_index(
        "uq_workspace_reserved_version_claim_request", table_name="caliber_workspace_version_claims"
    )
    op.drop_table("caliber_workspace_version_claims")
    op.drop_index(
        "ix_workspace_external_attestations_head",
        table_name="caliber_workspace_external_review_attestations",
    )
    op.drop_table("caliber_workspace_external_review_attestations")
    op.drop_index(
        "ix_workspace_change_request_reviews_request",
        table_name="caliber_workspace_change_request_reviews",
    )
    op.drop_table("caliber_workspace_change_request_reviews")
    op.drop_index(
        "uq_workspace_change_request_active_check",
        table_name="caliber_workspace_change_request_checks",
    )
    op.drop_index(
        "ix_workspace_change_request_checks_head",
        table_name="caliber_workspace_change_request_checks",
    )
    op.drop_table("caliber_workspace_change_request_checks")
    op.drop_index(
        "ix_workspace_change_request_comments_request",
        table_name="caliber_workspace_change_request_comments",
    )
    op.drop_table("caliber_workspace_change_request_comments")
    op.drop_index(
        "uq_workspace_change_request_active_reviewer",
        table_name="caliber_workspace_change_request_reviewers",
    )
    op.drop_index(
        "ix_workspace_change_request_reviewers_request",
        table_name="caliber_workspace_change_request_reviewers",
    )
    op.drop_table("caliber_workspace_change_request_reviewers")
    op.drop_index(
        "ix_workspace_change_request_heads_request",
        table_name="caliber_workspace_change_request_heads",
    )
    op.drop_table("caliber_workspace_change_request_heads")
    op.drop_index(
        "ix_workspace_change_requests_project_status",
        table_name="caliber_workspace_change_requests",
    )
    op.drop_table("caliber_workspace_change_requests")
