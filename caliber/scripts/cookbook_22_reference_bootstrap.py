#!/usr/bin/env python3
"""Seed the disposable reference-profile coordinates used by Cookbook 22.

The Lab 22 runner starts CALIBER with a project-scoped callback binding before
the HTTP server is available.  The binding is intentionally exact, so the
runner needs a deterministic project id rather than a wildcard.  This small
bootstrap creates the same disposable project, development environment and
approved runtime release on every fresh lab database.  It is idempotent so a
developer can retry a failed startup without manually repairing the database.

It is a lab fixture, not a production migration: it is invoked only by
``scripts/cookbook-env.sh`` through ``CALIBER_COOKBOOK_PRESTART``.
"""

from __future__ import annotations

from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberProject,
    CaliberProjectMember,
    CaliberWorkspaceEnvironment,
    CaliberWorkspaceRelease,
    CaliberWorkspaceRevision,
)
from caliber.db.session import create_engine_from_config, sessionmaker_from_engine
from caliber.deployment_environments import (
    WORKSPACE_ENVIRONMENT_CLASSES,
    WORKSPACE_ENVIRONMENT_DEFAULT_STATUS,
    WORKSPACE_ENVIRONMENT_NAMES,
    WORKSPACE_ENVIRONMENT_PROMOTION_ORDER,
)
from caliber.workspace_release_service import create_workspace_release

PROJECT_ID = "PRJ-cookbook-22"
PROJECT_NAME = "cookbook-22-governed-review"
REVISION_ID = "WSR-cookbook-22"
RELEASE_ID = "WSREL-cookbook-22"
APPROVER_MEMBER_ID = "PRJM-cookbook-22-approver"
ADMIN_MEMBER_ID = "PRJM-cookbook-22-admin"


def _require_identity(label: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise RuntimeError(
            f"Cookbook 22 fixture identity conflict for {label}: "
            f"expected {expected!r}, found {actual!r}"
        )


def main() -> None:  # noqa: PLR0912, PLR0915 - linear disposable fixture bootstrap
    config = CaliberConfig.load()
    engine = create_engine_from_config(config)
    try:
        factory = sessionmaker_from_engine(engine)
        with factory() as session:
            project = session.get(CaliberProject, PROJECT_ID)
            if project is None:
                project = CaliberProject(
                    project_id=PROJECT_ID,
                    tenant_id="local",
                    name=PROJECT_NAME,
                    description="Synthetic Harborlight governed evidence review",
                    owner="admin",
                    status="active",
                    storage_backend="local",
                    slug="cookbook-22-governed-review",
                    source_mode="caliber_managed",
                )
                session.add(project)
            else:
                _require_identity("project.tenant_id", project.tenant_id, "local")
                _require_identity("project.name", project.name, PROJECT_NAME)
                _require_identity("project.storage_backend", project.storage_backend, "local")
                _require_identity("project.slug", project.slug, "cookbook-22-governed-review")
                _require_identity("project.source_mode", project.source_mode, "caliber_managed")
                project.status = "active"
                project.owner = "admin"

            members = {
                ADMIN_MEMBER_ID: ("admin", "owner"),
                APPROVER_MEMBER_ID: ("lab22-approver", "reviewer"),
            }
            for member_id, (user_id, role) in members.items():
                member = session.get(CaliberProjectMember, member_id)
                if member is None:
                    session.add(
                        CaliberProjectMember(
                            member_id=member_id,
                            project_id=PROJECT_ID,
                            user_id=user_id,
                            role=role,
                            status="active",
                            created_by="cookbook-22",
                        )
                    )
                else:
                    _require_identity(
                        f"member {member_id}.project_id", member.project_id, PROJECT_ID
                    )
                    _require_identity(f"member {member_id}.user_id", member.user_id, user_id)
                    member.role = role
                    member.status = "active"

            environments: dict[str, CaliberWorkspaceEnvironment] = {}
            for name in WORKSPACE_ENVIRONMENT_NAMES:
                environment_id = f"WSE-cookbook-22-{name}"
                environment = session.get(CaliberWorkspaceEnvironment, environment_id)
                if environment is None:
                    environment = CaliberWorkspaceEnvironment(
                        environment_id=environment_id,
                        project_id=PROJECT_ID,
                        name=name,
                        environment_class=WORKSPACE_ENVIRONMENT_CLASSES[name],
                        promotion_order=WORKSPACE_ENVIRONMENT_PROMOTION_ORDER[name],
                        status=WORKSPACE_ENVIRONMENT_DEFAULT_STATUS[name],
                        created_by="cookbook-22",
                    )
                    session.add(environment)
                else:
                    _require_identity(
                        f"environment {environment_id}.project_id",
                        environment.project_id,
                        PROJECT_ID,
                    )
                    _require_identity(f"environment {environment_id}.name", environment.name, name)
                    _require_identity(
                        f"environment {environment_id}.environment_class",
                        environment.environment_class,
                        WORKSPACE_ENVIRONMENT_CLASSES[name],
                    )
                    _require_identity(
                        f"environment {environment_id}.promotion_order",
                        environment.promotion_order,
                        WORKSPACE_ENVIRONMENT_PROMOTION_ORDER[name],
                    )
                    environment.status = WORKSPACE_ENVIRONMENT_DEFAULT_STATUS[name]
                environments[name] = environment

            revision = session.get(CaliberWorkspaceRevision, REVISION_ID)
            if revision is None:
                revision = CaliberWorkspaceRevision(
                    revision_id=REVISION_ID,
                    project_id=PROJECT_ID,
                    revision_number=1,
                    source_kind="managed",
                    manifest={"apiVersion": "caliber/v1alpha1"},
                    source_attestation="fixture",
                    revision_sha256="1" * 64,
                    status="ready",
                    created_by="cookbook-22",
                    validated_by="cookbook-22",
                )
                session.add(revision)
            else:
                _require_identity("revision.project_id", revision.project_id, PROJECT_ID)
                _require_identity("revision.revision_number", revision.revision_number, 1)
                _require_identity("revision.source_kind", revision.source_kind, "managed")
                _require_identity(
                    "revision.source_attestation", revision.source_attestation, "fixture"
                )
                _require_identity("revision.revision_sha256", revision.revision_sha256, "1" * 64)
                _require_identity("revision.status", revision.status, "ready")

            session.flush()
            release = session.get(CaliberWorkspaceRelease, RELEASE_ID)
            if release is None:
                release = create_workspace_release(
                    session,
                    project_id=PROJECT_ID,
                    revision_id=REVISION_ID,
                    environment_id=environments["dev"].environment_id,
                    environment_config_sha256="2" * 64,
                    runtime_dependencies_sha256="3" * 64,
                    policy_sha256="4" * 64,
                    request_idempotency_key="cookbook-22-reference-release",
                    requested_by="cookbook-22",
                    release_id=RELEASE_ID,
                )
            else:
                _require_identity("release.project_id", release.project_id, PROJECT_ID)
                _require_identity("release.revision_id", release.revision_id, REVISION_ID)
                _require_identity(
                    "release.environment_id",
                    release.environment_id,
                    environments["dev"].environment_id,
                )
                _require_identity(
                    "release.request_idempotency_key",
                    release.request_idempotency_key,
                    "cookbook-22-reference-release",
                )
                _require_identity(
                    "release.environment_config_sha256",
                    release.environment_config_sha256,
                    "2" * 64,
                )
                _require_identity(
                    "release.runtime_dependencies_sha256",
                    release.runtime_dependencies_sha256,
                    "3" * 64,
                )
                _require_identity("release.policy_sha256", release.policy_sha256, "4" * 64)
            release.status = "approved"
            release.evaluated_by = "cookbook-22"
            environments["dev"].current_release_id = release.release_id
            session.commit()
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
