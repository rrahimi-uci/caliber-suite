"""B14.3a evidence archive export and persistence contract."""

from __future__ import annotations

import hashlib
import importlib
import io
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.audit import configure_redactor
from caliber.auth import CaliberIdentity
from caliber.contracts import evidence_package_digest, validate_record
from caliber.db.models import (
    CaliberEffectProposal,
    CaliberEvidenceArchive,
    CaliberProject,
    CaliberProjectMember,
    CaliberRunManifest,
    CaliberRuntimeApprovalRequest,
    CaliberWorkflow,
    CaliberWorkflowRun,
)
from caliber.evidence import (
    EvidenceExportError,
    EvidencePackage,
    EvidencePackageError,
    archive_member_name,
    assemble,
    export_archive,
    payload_bytes_for_package,
)
from caliber.redaction import IDENTITY_REDACTOR, Redactor
from caliber.storage import LocalStorageBackend, StorageConflictError, StorageError
from tests.workflow_helpers import PREFIX

export_module = importlib.import_module("caliber.evidence.export")

RUN_ID = "run-evidence-export"
PROJECT_ID = "project-evidence-export"
OWNER = "@evidence-export-owner"
IDENTITY = CaliberIdentity(
    user_id=OWNER,
    scopes=frozenset({"caliber.operator", "caliber.approver"}),
    active_project_id=PROJECT_ID,
)


def _seed_run(
    session: Session,
    *,
    manifest_bytes: bytes | None = None,
    include_manifest: bool = True,
    include_approval: bool = False,
) -> None:
    session.add(
        CaliberProject(
            project_id=PROJECT_ID,
            name="Evidence export project",
            owner=OWNER,
            storage_backend="local",
        )
    )
    session.add(
        CaliberWorkflowRun(
            workflow_run_id=RUN_ID,
            workflow_id="workflow-evidence-export",
            project_id=PROJECT_ID,
            tenant_id="tenant-evidence-export",
            created_by=OWNER,
            status="completed",
            queued_at=datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc),
            completed_at=datetime(2026, 11, 3, 9, 1, tzinfo=timezone.utc),
            manifest_legacy=not include_manifest,
        )
    )
    raw_manifest = manifest_bytes or b'{"email":"alice@example.com","run":"export"}'
    if include_manifest:
        session.add(
            CaliberRunManifest(
                manifest_id="manifest-export",
                workflow_run_id=RUN_ID,
                schema_version="v1",
                manifest_bytes=raw_manifest,
                manifest_digest=hashlib.sha256(raw_manifest).hexdigest(),
                created_at=datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc),
            )
        )
    intent = b'{"connector":"document-request","resource":"case-export"}'
    session.add(
        CaliberEffectProposal(
            proposal_id="proposal-export",
            tenant_id="tenant-evidence-export",
            workflow_run_id=RUN_ID,
            node_id="node-export",
            occurrence_id="occurrence-export",
            logical_key="a" * 64,
            connector="document-request",
            connector_version="v1",
            effect_class="reversible_write",
            resource="case-export",
            args={},
            expected_resource_version="1",
            intent_algorithm="sha256",
            intent_bytes=intent,
            intent_hash="sha256:" + hashlib.sha256(intent).hexdigest(),
            state="failed",
            version=1,
            created_at=datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc),
        )
    )
    if include_approval:
        session.add(
            CaliberRuntimeApprovalRequest(
                runtime_approval_id="approval-export",
                workflow_run_id=RUN_ID,
                project_id=PROJECT_ID,
                node_id="node-export",
                status="approved",
                requested_at=datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc),
                decided_at=datetime(2026, 11, 3, 9, 2, tzinfo=timezone.utc),
                decided_by=OWNER,
                decision_reason="approved for export test",
                policy_snapshot={"allowed_responses": ["approve", "reject"], "risk_class": "high"},
                proposal_id="proposal-export",
                intent_hash="sha256:" + "a" * 64,
                evidence_hash="sha256:" + "b" * 64,
                policy_digest="sha256:" + "c" * 64,
                expected_resource_version="1",
                expires_at=datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc),
                requested_by=OWNER,
            )
        )
    session.commit()


def test_export_archive_shape_and_idempotent_scope_cache(
    db_session: Session, tmp_path: object
) -> None:
    _seed_run(db_session)
    backend = LocalStorageBackend(str(tmp_path))

    result = export_archive(
        db_session,
        run_id=RUN_ID,
        identity=IDENTITY,
        backend=backend,
        actor=OWNER,
    )

    with ZipFile(io.BytesIO(result.archive_bytes)) as archive:
        names = archive.namelist()
        assert names == [
            "manifest.json",
            *sorted(
                archive_member_name(str(member["member_id"]))
                for member in result.manifest["members"]
            ),
        ]
        assert archive.getinfo("manifest.json").date_time == (1980, 1, 1, 0, 0, 0)
        manifest_bytes = archive.read("manifest.json")
        assert json.loads(manifest_bytes) == result.manifest
        validate_record("evidence_package", result.manifest)
        assert result.manifest["package_sha256"] == evidence_package_digest(result.manifest)
        for member in result.manifest["members"]:
            payload = archive.read(archive_member_name(str(member["member_id"])))
            assert hashlib.sha256(payload).hexdigest() == member["payload_sha256"]

    row = db_session.execute(select(CaliberEvidenceArchive)).scalar_one()
    assert result.digest == result.archive_sha256
    assert row.member_ids_json == [member["member_id"] for member in result.manifest["members"]]
    assert row.package_sha256 == result.package_sha256
    assert row.authorization_scope_digest == result.authorization_scope_digest
    assert row.object_key.endswith(f"/{result.package_sha256}.zip")
    assert result.authorization_scope_digest in row.object_key

    again = export_archive(
        db_session,
        run_id=RUN_ID,
        identity=IDENTITY,
        backend=backend,
        actor=OWNER,
    )
    assert again.archive_id == result.archive_id
    assert again.archive_bytes == result.archive_bytes
    assert db_session.execute(select(CaliberEvidenceArchive)).scalars().all() == [row]


def test_redaction_changes_payload_view_digest_and_object_key(
    db_session: Session, tmp_path: object
) -> None:
    _seed_run(db_session)
    backend = LocalStorageBackend(str(tmp_path))
    try:
        configure_redactor(IDENTITY_REDACTOR)
        clear = export_archive(
            db_session,
            run_id=RUN_ID,
            identity=IDENTITY,
            backend=backend,
            actor=OWNER,
        )
        configure_redactor(Redactor.from_patterns((r"alice@example.com",)))
        configured_clear = export_archive(
            db_session,
            run_id=RUN_ID,
            identity=IDENTITY,
            backend=backend,
            actor=OWNER,
        )
        redacted = export_archive(
            db_session,
            run_id=RUN_ID,
            identity=IDENTITY,
            backend=backend,
            actor=OWNER,
            redact=True,
        )
    finally:
        configure_redactor(IDENTITY_REDACTOR)

    assert clear.package_sha256 != redacted.package_sha256
    assert configured_clear.archive_id == clear.archive_id
    assert configured_clear.archive_bytes == clear.archive_bytes
    assert clear.object_key != redacted.object_key
    assert redacted.manifest["view"]["redaction"] == "redacted"
    with ZipFile(io.BytesIO(redacted.archive_bytes)) as archive:
        payloads = [
            archive.read(archive_member_name(str(member["member_id"])))
            for member in redacted.manifest["members"]
        ]
    assert b"alice@example.com" not in b"".join(payloads)


def test_forced_redaction_uses_default_patterns_when_none_are_configured(
    db_session: Session, tmp_path: object
) -> None:
    _seed_run(db_session)
    backend = LocalStorageBackend(str(tmp_path))
    try:
        configure_redactor(IDENTITY_REDACTOR)
        result = export_archive(
            db_session,
            run_id=RUN_ID,
            identity=IDENTITY,
            backend=backend,
            redact=True,
        )
    finally:
        configure_redactor(IDENTITY_REDACTOR)

    assert result.manifest["view"]["redaction"] == "redacted"
    with ZipFile(io.BytesIO(result.archive_bytes)) as archive:
        assert b"alice@example.com" not in archive.read(
            archive_member_name("run_manifest:manifest-export")
        )


def test_redaction_refuses_non_json_payloads(db_session: Session, tmp_path: object) -> None:
    _seed_run(db_session, manifest_bytes=b"not-json")
    try:
        configure_redactor(IDENTITY_REDACTOR)
        with pytest.raises(EvidenceExportError, match="non-JSON"):
            export_archive(
                db_session,
                run_id=RUN_ID,
                identity=IDENTITY,
                backend=LocalStorageBackend(str(tmp_path)),
                redact=True,
            )
    finally:
        configure_redactor(IDENTITY_REDACTOR)


def test_export_rejects_invalid_backend_and_project_storage_mismatch(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = SimpleNamespace(
        record={"project_id": "missing-project"},
        package_bytes=b"",
        package_sha256="a" * 64,
    )
    monkeypatch.setattr(export_module, "assemble", lambda *args, **kwargs: package)
    with pytest.raises(EvidenceExportError, match="must implement"):
        export_archive(
            db_session,
            run_id=RUN_ID,
            identity=IDENTITY,
            backend=object(),  # type: ignore[arg-type]
        )

    db_session.add(
        CaliberProject(
            project_id="mismatch-project",
            name="Mismatch project",
            storage_backend="s3",
        )
    )
    db_session.commit()
    package.record["project_id"] = "mismatch-project"
    with pytest.raises(EvidenceExportError, match="does not match"):
        export_archive(
            db_session,
            run_id=RUN_ID,
            identity=IDENTITY,
            backend=LocalStorageBackend("/tmp"),
        )


def test_export_rejects_a_disappeared_project(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = SimpleNamespace(
        record={"project_id": "missing-project"},
        package_bytes=b"",
        package_sha256="a" * 64,
    )
    monkeypatch.setattr(export_module, "assemble", lambda *args, **kwargs: package)
    with pytest.raises(EvidencePackageError, match="project disappeared"):
        export_archive(
            db_session,
            run_id=RUN_ID,
            identity=IDENTITY,
            backend=LocalStorageBackend("/tmp"),
        )


def test_archive_refuses_storage_key_collision(
    db_session: Session, tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_run(db_session)
    backend = LocalStorageBackend(str(tmp_path))
    original = backend.write_bytes

    def write_collision(*args: object, **kwargs: object) -> object:
        key = str(args[0])
        original(key, b"different bytes", overwrite=True)
        return original(key, b"different bytes", overwrite=False)

    monkeypatch.setattr(backend, "write_bytes", write_collision)
    with pytest.raises(EvidenceExportError, match="different bytes"):
        export_archive(
            db_session,
            run_id=RUN_ID,
            identity=IDENTITY,
            backend=backend,
            actor=OWNER,
        )


def test_archive_conditional_create_reads_the_matching_existing_object(
    db_session: Session, tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_run(db_session)
    backend = LocalStorageBackend(str(tmp_path))
    original = backend.write_bytes

    def write_then_conflict(*args: object, **kwargs: object) -> object:
        original(str(args[0]), args[1], overwrite=True)
        raise StorageConflictError("already exists")

    monkeypatch.setattr(backend, "write_bytes", write_then_conflict)
    result = export_archive(
        db_session,
        run_id=RUN_ID,
        identity=IDENTITY,
        backend=backend,
        actor=OWNER,
    )
    assert result.archive_bytes
    assert db_session.execute(select(CaliberEvidenceArchive)).scalar_one().object_key


def test_archive_conditional_create_fails_when_the_winner_is_unavailable(
    db_session: Session, tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_run(db_session)
    backend = LocalStorageBackend(str(tmp_path))

    def write_conflict(*args: object, **kwargs: object) -> object:
        raise StorageConflictError("already exists")

    def read_unavailable(*args: object, **kwargs: object) -> bytes:
        raise StorageError("unavailable")

    monkeypatch.setattr(backend, "write_bytes", write_conflict)
    monkeypatch.setattr(backend, "read_bytes", read_unavailable)
    with pytest.raises(EvidenceExportError, match="concurrent evidence archive is unavailable"):
        export_archive(
            db_session,
            run_id=RUN_ID,
            identity=IDENTITY,
            backend=backend,
            actor=OWNER,
        )


def test_archive_storage_failure_is_reported(
    db_session: Session, tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_run(db_session)
    backend = LocalStorageBackend(str(tmp_path))

    def write_failure(*args: object, **kwargs: object) -> object:
        raise StorageError("write failed")

    monkeypatch.setattr(backend, "write_bytes", write_failure)
    with pytest.raises(EvidenceExportError, match="storage failed"):
        export_archive(
            db_session,
            run_id=RUN_ID,
            identity=IDENTITY,
            backend=backend,
            actor=OWNER,
        )


def test_archive_database_race_returns_the_concurrent_locator(
    db_session: Session, tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_run(db_session)
    backend = LocalStorageBackend(str(tmp_path))
    real_execute = db_session.execute
    state: dict[str, object] = {}

    class ConcurrentResult:
        def scalar_one_or_none(self) -> object:
            return state["row"]

    def fake_flush(*args: object, **kwargs: object) -> None:
        row = next(item for item in db_session.new if isinstance(item, CaliberEvidenceArchive))
        state["row"] = SimpleNamespace(
            archive_id=row.archive_id,
            workflow_run_id=row.workflow_run_id,
            object_key=row.object_key,
            archive_sha256=row.archive_sha256,
            package_sha256=row.package_sha256,
            authorization_scope_digest=row.authorization_scope_digest,
        )
        raise IntegrityError("duplicate archive", {}, Exception("duplicate archive"))

    def execute(statement: object, *args: object, **kwargs: object) -> object:
        if state and "caliber_evidence_archives" in str(statement):
            return ConcurrentResult()
        return real_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "flush", fake_flush)
    monkeypatch.setattr(db_session, "execute", execute)
    result = export_archive(
        db_session,
        run_id=RUN_ID,
        identity=IDENTITY,
        backend=backend,
        actor=OWNER,
    )
    assert result.archive_bytes


def test_persisted_archive_integrity_and_manifest_contract_are_verified(
    db_session: Session, tmp_path: object
) -> None:
    _seed_run(db_session)
    backend = LocalStorageBackend(str(tmp_path))
    result = export_archive(
        db_session,
        run_id=RUN_ID,
        identity=IDENTITY,
        backend=backend,
        actor=OWNER,
    )
    row = db_session.execute(select(CaliberEvidenceArchive)).scalar_one()

    def row_for(
        data: bytes,
        *,
        package_sha256: str = row.package_sha256,
        archive_sha256: str | None = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            archive_id=row.archive_id,
            workflow_run_id=row.workflow_run_id,
            object_key=row.object_key,
            archive_sha256=archive_sha256 or hashlib.sha256(data).hexdigest(),
            package_sha256=package_sha256,
            authorization_scope_digest=row.authorization_scope_digest,
        )

    reader = export_module._read_existing

    class UnavailableBackend:
        def read_bytes(self, key: str) -> bytes:
            raise StorageError("unavailable")

    with pytest.raises(EvidenceExportError, match="persisted evidence archive is unavailable"):
        reader(UnavailableBackend(), row_for(b"unavailable"))

    with pytest.raises(EvidenceExportError, match="content digest"):
        reader(
            backend,
            row_for(
                b"tampered",
                package_sha256=row.package_sha256,
                archive_sha256="0" * 64,
            ),
        )

    bad_zip = b"not-a-zip"
    backend.write_bytes(row.object_key, bad_zip, media_type="application/zip", overwrite=True)
    with pytest.raises(EvidenceExportError, match="no valid manifest"):
        reader(backend, row_for(bad_zip, package_sha256=row.package_sha256))

    def archive_with_manifest(manifest: object) -> bytes:
        output = io.BytesIO()
        with ZipFile(output, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest).encode())
        return output.getvalue()

    non_object = archive_with_manifest([])
    backend.write_bytes(row.object_key, non_object, media_type="application/zip", overwrite=True)
    with pytest.raises(EvidenceExportError, match="not an object"):
        reader(backend, row_for(non_object, package_sha256=row.package_sha256))

    invalid = archive_with_manifest({})
    backend.write_bytes(row.object_key, invalid, media_type="application/zip", overwrite=True)
    with pytest.raises(EvidenceExportError, match="manifest is invalid"):
        reader(backend, row_for(invalid, package_sha256=row.package_sha256))

    backend.write_bytes(
        row.object_key, result.archive_bytes, media_type="application/zip", overwrite=True
    )
    with pytest.raises(EvidenceExportError, match="does not match its locator"):
        reader(backend, row_for(result.archive_bytes, package_sha256="f" * 64))


def test_payload_materialization_covers_legacy_and_bound_approval_members(
    db_session: Session,
) -> None:
    _seed_run(db_session, include_manifest=False, include_approval=True)
    package = assemble(db_session, run_id=RUN_ID, identity=IDENTITY)
    payloads = payload_bytes_for_package(db_session, package)

    assert "run_manifest:run-evidence-export" not in payloads
    assert "proposal:proposal-export" in payloads
    assert "approval_request:approval-export" in payloads
    assert "approval_decision:approval-export" in payloads


def test_payload_materialization_refuses_disappeared_or_changed_members(
    db_session: Session,
) -> None:
    _seed_run(db_session)
    package = assemble(db_session, run_id=RUN_ID, identity=IDENTITY)

    class GoneSession:
        def get(self, *args: object, **kwargs: object) -> None:
            return None

    with pytest.raises(EvidencePackageError, match="disappeared"):
        payload_bytes_for_package(GoneSession(), package)  # type: ignore[arg-type]

    changed = EvidencePackage(
        record={
            **package.record,
            "members": [
                {
                    **package.record["members"][0],
                    "payload_sha256": "f" * 64,
                }
            ],
        },
        package_bytes=package.package_bytes,
        package_sha256=package.package_sha256,
    )
    with pytest.raises(EvidencePackageError, match="changed after assembly"):
        payload_bytes_for_package(db_session, changed)


def test_prepare_manifest_normalizes_contract_validation_errors(
    db_session: Session, tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_run(db_session)
    package = assemble(db_session, run_id=RUN_ID, identity=IDENTITY)
    payloads = payload_bytes_for_package(db_session, package)

    def invalid_validator(*args: object, **kwargs: object) -> None:
        raise ValueError("contract failure")

    monkeypatch.setattr(export_module, "validate_record", invalid_validator)
    with pytest.raises(EvidenceExportError, match="export manifest is invalid"):
        export_module._prepare_manifest(package, payloads, redact=False)


def test_object_key_rejects_unsafe_scope_identifiers() -> None:
    with pytest.raises(EvidenceExportError, match="unsafe identifier"):
        export_module._object_key(
            tenant_id="../tenant",
            project_id="project",
            run_id="run",
            scope_digest="a" * 64,
            view_digest="b" * 64,
        )


def test_evidence_route_downloads_persisted_archive(
    client: TestClient, db_session: Session
) -> None:
    project_id = "project-evidence-route"
    run_id = "run-evidence-route"
    workflow_id = "workflow-evidence-route"
    db_session.add(
        CaliberProject(
            project_id=project_id,
            name="Evidence route project",
            owner="@test",
            storage_backend="local",
        )
    )
    db_session.add(
        CaliberWorkflow(
            workflow_id=workflow_id,
            name="Evidence route workflow",
            owner="@test",
            project_id=project_id,
            visibility="project",
        )
    )
    db_session.add(
        CaliberWorkflowRun(
            workflow_run_id=run_id,
            workflow_id=workflow_id,
            project_id=project_id,
            tenant_id="tenant-route",
            created_by="@test",
            status="completed",
            manifest_legacy=False,
        )
    )
    raw = b'{"route":"evidence"}'
    db_session.add(
        CaliberRunManifest(
            manifest_id="manifest-route",
            workflow_run_id=run_id,
            schema_version="v1",
            manifest_bytes=raw,
            manifest_digest=hashlib.sha256(raw).hexdigest(),
        )
    )
    db_session.commit()

    # ``create_app`` installs its configured redactor globally; keep this route
    # test's owner-authorized view from leaking process state into B14.2 tests.
    configure_redactor(IDENTITY_REDACTOR)
    response = client.get(
        f"{PREFIX}/workflow-runs/{run_id}/evidence",
        headers={"X-CALIBER-Project": project_id, "X-CALIBER-User": "@test"},
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/zip")
    assert response.headers["content-disposition"].endswith(f'filename="evidence-{run_id}.zip"')
    with ZipFile(io.BytesIO(response.content)) as archive:
        assert "manifest.json" in archive.namelist()
        assert json.loads(archive.read("manifest.json"))["workflow_run_id"] == run_id


def test_evidence_route_redacts_for_a_project_viewer(
    client: TestClient, db_session: Session
) -> None:
    project_id = "project-evidence-viewer"
    run_id = "run-evidence-viewer"
    workflow_id = "workflow-evidence-viewer"
    viewer = "@evidence-viewer"
    owner = "@evidence-owner"
    db_session.add(
        CaliberProject(
            project_id=project_id,
            name="Evidence viewer project",
            owner=owner,
            storage_backend="local",
        )
    )
    db_session.add(
        CaliberProjectMember(
            member_id="member-evidence-viewer",
            project_id=project_id,
            user_id=viewer,
            role="viewer",
            status="active",
            created_by=owner,
        )
    )
    db_session.add(
        CaliberWorkflow(
            workflow_id=workflow_id,
            name="Evidence viewer workflow",
            owner=owner,
            project_id=project_id,
            visibility="project",
        )
    )
    db_session.add(
        CaliberWorkflowRun(
            workflow_run_id=run_id,
            workflow_id=workflow_id,
            project_id=project_id,
            tenant_id="tenant-viewer",
            created_by=owner,
            status="completed",
            manifest_legacy=False,
        )
    )
    raw = b'{"email":"viewer-target@example.com","route":"redacted"}'
    db_session.add(
        CaliberRunManifest(
            manifest_id="manifest-viewer",
            workflow_run_id=run_id,
            schema_version="v1",
            manifest_bytes=raw,
            manifest_digest=hashlib.sha256(raw).hexdigest(),
        )
    )
    db_session.commit()

    configure_redactor(IDENTITY_REDACTOR)
    response = client.get(
        f"{PREFIX}/workflow-runs/{run_id}/evidence",
        headers={"X-CALIBER-Project": project_id, "X-CALIBER-User": viewer},
    )
    assert response.status_code == 200, response.text
    with ZipFile(io.BytesIO(response.content)) as archive:
        payload = archive.read(archive_member_name("run_manifest:manifest-viewer"))
    assert b"viewer-target@example.com" not in payload
    assert b"[REDACTED]" in payload
