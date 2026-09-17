"""P4-A tests for dormant Workspace revision persistence and invariants."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from caliber.db import Base
from caliber.db.models import (
    CaliberProject,
    CaliberWorkflowFile,
    CaliberWorkspaceImportJob,
    CaliberWorkspaceRevision,
    CaliberWorkspaceRevisionResource,
    CaliberWorkspaceSource,
    WorkspaceRevisionImmutableError,
    WorkspaceSnapshotRetentionError,
)
from caliber.ids import (
    new_workspace_import_id,
    new_workspace_revision_id,
    new_workspace_revision_resource_id,
    new_workspace_source_id,
)
from caliber.workspace_revisions import (
    RevisionAllocationError,
    allocate_revision_number,
    assert_revision_mutable,
    assert_snapshot_deletable,
    finalize_revision,
)


def _project(session: Session, project_id: str = "PRJ-revision") -> CaliberProject:
    project = CaliberProject(project_id=project_id, name=f"Project {project_id}")
    session.add(project)
    session.flush()
    return project


def _revision(
    project_id: str,
    *,
    revision_id: str = "WSR-test",
    status: str = "validating",
    source_snapshot_file_id: str | None = None,
) -> CaliberWorkspaceRevision:
    return CaliberWorkspaceRevision(
        revision_id=revision_id,
        project_id=project_id,
        revision_number=1,
        manifest={"apiVersion": "caliber/v1alpha1"},
        manifest_sha256="a" * 64,
        source_bundle_sha256="b" * 64,
        source_snapshot_file_id=source_snapshot_file_id,
        revision_sha256="c" * 64,
        status=status,
    )


def _snapshot_file(file_id: str) -> CaliberWorkflowFile:
    return CaliberWorkflowFile(
        file_id=file_id,
        tenant_id="local",
        project_id="PRJ-revision",
        kind="workspace",
        name=file_id,
        relative_path=file_id,
        file_ref=f"projects/PRJ-revision/{file_id}",
        storage_backend="local",
        storage_uri=f"file:///{file_id}",
        object_key=file_id,
        media_type="application/octet-stream",
        size_bytes=1,
        sha256="d" * 64,
        status="available",
    )


def test_workspace_ids_use_the_plan_prefixes() -> None:
    assert new_workspace_source_id().startswith("WSS-")
    assert new_workspace_import_id().startswith("WSI-")
    assert new_workspace_revision_id().startswith("WSR-")
    assert new_workspace_revision_resource_id().startswith("WSRR-")


def test_allocator_reserves_monotonic_numbers_and_advances_project_counter(
    db_session: Session,
) -> None:
    project = _project(db_session)
    db_session.commit()

    assert allocate_revision_number(db_session, project.project_id) == 1
    db_session.commit()
    assert allocate_revision_number(db_session, project.project_id) == 2
    db_session.commit()

    db_session.refresh(project)
    assert project.next_revision_number == 3


def test_allocator_fails_closed_for_unknown_or_invalid_projects(db_session: Session) -> None:
    with pytest.raises(RevisionAllocationError, match="was not found"):
        allocate_revision_number(db_session, "PRJ-missing")

    project = _project(db_session, "PRJ-invalid-counter")
    project.next_revision_number = 0
    db_session.commit()
    with pytest.raises(RevisionAllocationError, match="invalid next revision"):
        allocate_revision_number(db_session, project.project_id)

    with pytest.raises(ValueError, match="max_attempts"):
        allocate_revision_number(db_session, project.project_id, max_attempts=0)


def test_allocator_uses_compare_and_set_when_expected_counter_is_stale(
    db_session: Session,
) -> None:
    project = _project(db_session, "PRJ-cas")
    db_session.commit()
    assert allocate_revision_number(db_session, project.project_id) == 1
    db_session.commit()

    # A retry can still succeed after another allocator moved the counter.
    assert allocate_revision_number(db_session, project.project_id) == 2


def test_allocator_reports_contention_after_bounded_compare_and_set_retries() -> None:
    class AlwaysContendedSession:
        def scalar(self, statement: object) -> int:
            return 1

        def execute(self, statement: object) -> SimpleNamespace:
            return SimpleNamespace(rowcount=0)

    with pytest.raises(RevisionAllocationError, match="after 2"):
        allocate_revision_number(AlwaysContendedSession(), "PRJ-contention", max_attempts=2)  # type: ignore[arg-type]


def test_finalizing_revision_makes_it_immutable(db_session: Session) -> None:
    project = _project(db_session)
    revision = _revision(project.project_id)
    db_session.add(revision)
    db_session.commit()

    finalize_revision(
        db_session,
        revision,
        "ready",
        validation_report={"errors": []},
        validated_by="@reviewer",
    )
    db_session.commit()
    assert revision.validated_by == "@reviewer"

    revision.manifest = {"changed": True}
    with pytest.raises(WorkspaceRevisionImmutableError, match="terminal"):
        db_session.flush()
    db_session.rollback()

    with pytest.raises(WorkspaceRevisionImmutableError, match="already"):
        finalize_revision(db_session, revision, "invalid")

    validating = _revision(project.project_id, revision_id="WSR-validating")
    db_session.add(validating)
    assert_revision_mutable(validating)
    validating.status = "unexpected"
    with pytest.raises(WorkspaceRevisionImmutableError, match="unknown mutable state"):
        assert_revision_mutable(validating)
    db_session.rollback()


def test_finalizing_revision_requires_a_terminal_status(db_session: Session) -> None:
    project = _project(db_session, "PRJ-finalize-status")
    revision = _revision(project.project_id, revision_id="WSR-finalize-status")
    db_session.add(revision)
    db_session.commit()

    with pytest.raises(ValueError, match="terminal revision status"):
        finalize_revision(db_session, revision, "validating")


def test_source_and_import_job_schema_is_project_bound_and_closed(db_session: Session) -> None:
    project = _project(db_session, "PRJ-source")
    source = CaliberWorkspaceSource(
        source_id="WSS-source",
        project_id=project.project_id,
        provider="github",
        provider_host="github.com",
        canonical_repository_id="repo-1",
        display_path="owner/repo",
    )
    db_session.add(source)
    db_session.flush()
    assert source.manifest_path == ".caliber/workspace.yaml"
    assert source.import_mode == "push"
    assert source.status == "active"

    job = CaliberWorkspaceImportJob(
        import_job_id="WSI-source",
        project_id=project.project_id,
        source_id=source.source_id,
        repository="owner/repo",
        commit_sha="1" * 40,
        idempotency_key="import-1",
    )
    db_session.add(job)
    db_session.commit()
    assert job.status == "queued"

    _project(db_session, "PRJ-invalid-source")
    invalid_source = CaliberWorkspaceSource(
        source_id="WSS-invalid",
        project_id="PRJ-invalid-source",
        provider="not-a-provider",
        provider_host="example.invalid",
        canonical_repository_id="repo-invalid",
        display_path="owner/invalid",
    )
    db_session.add(invalid_source)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_revision_pins_cannot_change_after_terminal_validation(db_session: Session) -> None:
    project = _project(db_session, "PRJ-pins")
    revision = _revision(project.project_id, revision_id="WSR-pins")
    db_session.add(revision)
    db_session.commit()
    finalize_revision(db_session, revision, "ready")
    db_session.commit()

    pin = CaliberWorkspaceRevisionResource(
        resource_pin_id="WSRR-pins",
        revision_id=revision.revision_id,
        resource_type="workflow",
        logical_name="demo",
        resource_id="WF-demo",
        version_ref="WFV-demo@1",
        content_sha256="e" * 64,
    )
    db_session.add(pin)
    with pytest.raises(WorkspaceRevisionImmutableError, match="terminal"):
        db_session.flush()
    db_session.rollback()

    with pytest.raises(WorkspaceRevisionImmutableError, match="terminal"):
        assert_revision_mutable(revision)


def test_ready_revision_retains_source_and_resource_snapshots(db_session: Session) -> None:
    project = _project(db_session, "PRJ-retention")
    source_file = _snapshot_file("FILE-source")
    resource_file = _snapshot_file("FILE-resource")
    db_session.add_all([source_file, resource_file])
    revision = _revision(
        project.project_id,
        revision_id="WSR-retention",
        source_snapshot_file_id=source_file.file_id,
    )
    db_session.add(revision)
    resource = CaliberWorkspaceRevisionResource(
        resource_pin_id="WSRR-retention",
        revision_id=revision.revision_id,
        resource_type="skill",
        logical_name="demo",
        resource_id="SK-demo",
        version_ref="SKV-demo@1",
        content_sha256="f" * 64,
        snapshot_file_id=resource_file.file_id,
    )
    db_session.add(resource)
    finalize_revision(db_session, revision, "ready")
    db_session.commit()

    for file_id in (source_file.file_id, resource_file.file_id):
        with pytest.raises(WorkspaceSnapshotRetentionError, match="retained"):
            assert_snapshot_deletable(db_session, file_id)
        db_session.delete(db_session.get(CaliberWorkflowFile, file_id))
        with pytest.raises(WorkspaceSnapshotRetentionError, match="retained"):
            db_session.flush()
        db_session.rollback()


def test_invalid_revision_does_not_retain_physical_snapshot(db_session: Session) -> None:
    project = _project(db_session, "PRJ-invalid-retention")
    snapshot = _snapshot_file("FILE-invalid")
    db_session.add(snapshot)
    revision = _revision(
        project.project_id,
        revision_id="WSR-invalid-retention",
        source_snapshot_file_id=snapshot.file_id,
    )
    db_session.add(revision)
    db_session.commit()
    finalize_revision(db_session, revision, "invalid", validation_report={"error": "bad pin"})
    db_session.commit()

    assert_snapshot_deletable(db_session, snapshot.file_id)
    db_session.delete(snapshot)
    db_session.commit()
    assert db_session.get(CaliberWorkflowFile, snapshot.file_id) is None


def test_workspace_revision_schema_supports_idempotent_content_and_project_scope(
    db_session: Session,
) -> None:
    project = _project(db_session, "PRJ-constraints")
    revision = _revision(project.project_id, revision_id="WSR-constraints")
    db_session.add(revision)
    db_session.commit()

    duplicate = _revision(project.project_id, revision_id="WSR-duplicate")
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_migration_and_model_metadata_include_p4a_tables() -> None:
    expected = {
        "caliber_workspace_sources",
        "caliber_workspace_import_jobs",
        "caliber_workspace_revisions",
        "caliber_workspace_revision_resources",
    }
    assert expected <= set(Base.metadata.tables)
    assert "next_revision_number" in Base.metadata.tables["caliber_projects"].columns
