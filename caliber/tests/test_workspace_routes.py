"""P4-C contract tests for the provider-neutral Workspace public API."""

from __future__ import annotations

import base64
import json
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO

import pytest
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberProject,
    CaliberWorkflowFile,
    CaliberWorkspaceImportJob,
    CaliberWorkspaceRevision,
    CaliberWorkspaceRevisionResource,
    CaliberWorkspaceSource,
)
from caliber.routes import workspace as workspace_routes
from caliber.storage import StorageError
from caliber.workspace_sources import (
    WorkspaceSourceProviderError,
    WorkspaceSourceProviderRegistry,
    WorkspaceSourceVerification,
    WorkspaceSourceVerificationError,
    source_etag,
)

PREFIX = "/ajax-api/2.0/mlflow/caliber"


@dataclass
class _FakeSourceProvider:
    name: str = "github"

    def capabilities(self) -> Mapping[str, object]:
        return {"push_import": True, "reconcile": True}

    def verify(self, source: object) -> WorkspaceSourceVerification:
        return WorkspaceSourceVerification(
            canonical_repository_id=source.canonical_repository_id,  # type: ignore[attr-defined]
            capabilities=self.capabilities(),
        )


@dataclass
class _FailingSourceProvider(_FakeSourceProvider):
    def capabilities(self) -> Mapping[str, object]:
        raise WorkspaceSourceProviderError("capabilities temporarily unavailable")

    def verify(self, source: object) -> WorkspaceSourceVerification:
        raise WorkspaceSourceProviderError("source verification temporarily unavailable")


@dataclass
class _WrongIdentityProvider(_FakeSourceProvider):
    def verify(self, source: object) -> WorkspaceSourceVerification:
        return WorkspaceSourceVerification("github:wrong", self.capabilities())


def _create_project(client: TestClient, name: str = "Workspace API") -> str:
    response = client.post(f"{PREFIX}/projects", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["data"]["project_id"]


def _source_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "provider": "github",
        "provider_host": "github.com",
        "canonical_repository_id": "github:123",
        "display_path": "owner/workspace-api",
        "default_branch": "main",
        "root_path": "",
        "manifest_path": ".caliber/workspace.yaml",
        "import_mode": "push",
        "connection_ref": "secret://github-connection",
    }
    payload.update(overrides)
    return payload


def _configure_source(client: TestClient, project_id: str) -> tuple[str, str]:
    response = client.put(
        f"{PREFIX}/projects/{project_id}/source",
        json=_source_payload(),
    )
    assert response.status_code == 200, response.text
    source = response.json()["data"]["source"]
    return source["source_id"], response.headers["etag"]


def _install_fake_provider(client: TestClient) -> None:
    client.app.state.workspace_source_registry = WorkspaceSourceProviderRegistry(
        {"github": _FakeSourceProvider()}
    )


def _activate_source(client: TestClient, project_id: str) -> str:
    _source_id, etag = _configure_source(client, project_id)
    _install_fake_provider(client)
    enabled = client.post(
        f"{PREFIX}/projects/{project_id}/source:enable", headers={"If-Match": etag}
    )
    assert enabled.status_code == 200, enabled.text
    return enabled.headers["etag"]


def _bundle(*, extra: bool = False, manifest_path: str = ".caliber/workspace.yaml") -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            manifest_path,
            """apiVersion: caliber/v1alpha1
kind: Workspace
metadata:
  slug: workspace-api
resources:
  workflows:
    - name: support
      path: workflows/support.json
""",
        )
        archive.writestr("workflows/support.json", '{"name":"support"}\n')
        if extra:
            archive.writestr("docs/README.md", "a changed source tree\n")
    return output.getvalue()


def _import(
    client: TestClient,
    project_id: str,
    *,
    key: str = "import-1",
    commit: str = "commit-1",
    bundle: bytes | None = None,
    repository: str = "owner/workspace-api",
) -> object:
    return client.post(
        f"{PREFIX}/projects/{project_id}/revision-imports",
        headers={"Idempotency-Key": key},
        data={"repository": repository, "commit_sha": commit},
        files={"bundle": ("workspace.zip", bundle or _bundle(), "application/zip")},
    )


def test_source_lifecycle_is_secret_free_and_etag_protected(client: TestClient) -> None:
    project_id = _create_project(client)

    initial = client.get(f"{PREFIX}/projects/{project_id}/source")
    assert initial.status_code == 200
    assert initial.json()["data"] == {"source_mode": "caliber_managed", "source": None}
    assert client.get(f"{PREFIX}/projects/{project_id}/source/capabilities").status_code == 404

    source_id, etag = _configure_source(client, project_id)
    configured = client.get(f"{PREFIX}/projects/{project_id}/source")
    assert configured.headers["etag"] == etag
    source = configured.json()["data"]["source"]
    assert source["source_id"] == source_id
    assert source["status"] == "disabled"
    assert source["has_connection"] is True
    assert "connection_ref" not in source
    assert configured.json()["data"]["source_mode"] == "git_managed"

    missing_match = client.put(
        f"{PREFIX}/projects/{project_id}/source", json=_source_payload(default_branch="trunk")
    )
    assert missing_match.status_code == 412
    assert missing_match.headers["etag"] == etag

    stale = client.post(
        f"{PREFIX}/projects/{project_id}/source:enable",
        headers={"If-Match": '"stale"'},
    )
    assert stale.status_code == 412
    assert stale.headers["etag"] == etag

    unavailable = client.post(
        f"{PREFIX}/projects/{project_id}/source:enable", headers={"If-Match": etag}
    )
    assert unavailable.status_code == 409
    assert "source_provider_unavailable" in unavailable.json()["detail"]

    _install_fake_provider(client)
    enabled = client.post(
        f"{PREFIX}/projects/{project_id}/source:enable", headers={"If-Match": etag}
    )
    assert enabled.status_code == 200, enabled.text
    enabled_etag = enabled.headers["etag"]
    assert enabled.json()["data"]["source"]["status"] == "active"
    capabilities = client.get(f"{PREFIX}/projects/{project_id}/source/capabilities")
    assert capabilities.status_code == 200
    assert capabilities.json()["data"]["available"] is True
    assert capabilities.json()["data"]["capabilities"]["push_import"] is True

    disabled = client.post(
        f"{PREFIX}/projects/{project_id}/source:disable", headers={"If-Match": enabled_etag}
    )
    assert disabled.status_code == 200
    assert disabled.json()["data"]["source"]["status"] == "disabled"


def test_source_context_and_owner_scope_are_enforced(client: TestClient) -> None:
    project_id = _create_project(client, "Workspace source access")
    mismatch = client.get(
        f"{PREFIX}/projects/{project_id}/source",
        headers={"X-CALIBER-Project": "PRJ-other"},
    )
    assert mismatch.status_code == 400
    assert mismatch.json()["detail"] == "workspace_context_mismatch"

    forbidden = client.put(
        f"{PREFIX}/projects/{project_id}/source",
        headers={"X-CALIBER-User": "@viewer-only"},
        json=_source_payload(),
    )
    assert forbidden.status_code == 403


def test_provider_registry_rejects_identity_equivocation() -> None:
    class WrongProvider(_FakeSourceProvider):
        def verify(self, source: object) -> WorkspaceSourceVerification:
            return WorkspaceSourceVerification("github:other", self.capabilities())

    from types import SimpleNamespace

    source = SimpleNamespace(provider="github", canonical_repository_id="github:123")
    registry = WorkspaceSourceProviderRegistry({"github": WrongProvider()})
    with pytest.raises(WorkspaceSourceVerificationError, match="does not match"):
        registry.verify(source)  # type: ignore[arg-type]


def test_provider_registry_is_explicit_and_capability_failures_are_unavailable() -> None:
    registry = WorkspaceSourceProviderRegistry()
    assert registry.get("github") is None
    assert registry.capabilities("github") is None

    with pytest.raises(ValueError, match="must not be empty"):
        registry.register(_FakeSourceProvider(name="   "))

    provider = _FakeSourceProvider(name=" GitHub ")
    registry.register(provider)
    assert registry.get("GITHUB") is provider
    assert registry.capabilities("github") == {
        "push_import": True,
        "reconcile": True,
    }

    failing = WorkspaceSourceProviderRegistry({"github": _FailingSourceProvider()})
    assert failing.capabilities("github") is None


def test_import_submission_is_durable_idempotent_and_digest_pinned(client: TestClient) -> None:
    project_id = _create_project(client, "Workspace imports")
    _configure_source(client, project_id)
    _install_fake_provider(client)
    source = client.get(f"{PREFIX}/projects/{project_id}/source").json()["data"]["source"]
    enabled = client.post(
        f"{PREFIX}/projects/{project_id}/source:enable",
        headers={"If-Match": f'"{source["etag"]}"'},
    )
    assert enabled.status_code == 200, enabled.text

    first = _import(client, project_id)
    assert first.status_code == 202, first.text
    job = first.json()["data"]
    assert job["status"] == "queued"
    assert job["source_snapshot_file_id"]
    assert len(job["upload_sha256"]) == 64
    assert len(job["source_bundle_sha256"]) == 64
    assert len(job["manifest_sha256"]) == 64

    blocked_disable = client.post(
        f"{PREFIX}/projects/{project_id}/source:disable",
        headers={"If-Match": enabled.headers["etag"]},
    )
    assert blocked_disable.status_code == 409
    assert blocked_disable.json()["detail"] == "source_has_in_flight_import"

    blocked_replace = client.put(
        f"{PREFIX}/projects/{project_id}/source",
        headers={"If-Match": enabled.headers["etag"]},
        json=_source_payload(default_branch="trunk"),
    )
    assert blocked_replace.status_code == 409
    assert blocked_replace.json()["detail"] == "source_has_in_flight_import"

    detail = client.get(f"{PREFIX}/projects/{project_id}/revision-imports/{job['import_job_id']}")
    assert detail.status_code == 200
    assert detail.json()["data"]["import_job_id"] == job["import_job_id"]

    repeated = _import(client, project_id)
    assert repeated.status_code == 202
    assert repeated.json()["data"]["import_job_id"] == job["import_job_id"]

    idempotency_conflict = _import(client, project_id, commit="commit-2")
    assert idempotency_conflict.status_code == 409
    assert idempotency_conflict.json()["detail"] == "import_idempotency_conflict"

    digest_conflict = _import(client, project_id, key="import-2", bundle=_bundle(extra=True))
    assert digest_conflict.status_code == 409
    assert digest_conflict.json()["detail"] == "source_commit_digest_conflict"


def test_import_rejects_invalid_context_input_and_non_push_source(client: TestClient) -> None:
    project_id = _create_project(client, "Workspace import validation")
    _configure_source(client, project_id)

    no_key = client.post(
        f"{PREFIX}/projects/{project_id}/revision-imports",
        files={"bundle": ("workspace.zip", _bundle(), "application/zip")},
    )
    assert no_key.status_code == 400
    assert no_key.json()["detail"] == "idempotency_key_required"

    too_long_key = _import(client, project_id, key="k" * 257)
    assert too_long_key.status_code == 400
    assert too_long_key.json()["detail"] == "idempotency_key_too_long"

    missing_bundle = client.post(
        f"{PREFIX}/projects/{project_id}/revision-imports",
        headers={"Idempotency-Key": "missing-bundle"},
        data={"repository": "owner/workspace-api", "commit_sha": "commit"},
    )
    assert missing_bundle.status_code == 400
    assert missing_bundle.json()["detail"] == "multipart_field_bundle_required"

    empty_bundle = client.post(
        f"{PREFIX}/projects/{project_id}/revision-imports",
        headers={"Idempotency-Key": "empty-bundle"},
        data={"repository": "owner/workspace-api", "commit_sha": "commit"},
        files={"bundle": ("empty.zip", b"", "application/zip")},
    )
    assert empty_bundle.status_code == 400
    assert empty_bundle.json()["detail"] == "bundle_required"

    missing_repository = client.post(
        f"{PREFIX}/projects/{project_id}/revision-imports",
        headers={"Idempotency-Key": "missing-repository"},
        data={"commit_sha": "commit"},
        files={"bundle": ("workspace.zip", _bundle(), "application/zip")},
    )
    assert missing_repository.status_code == 400
    assert missing_repository.json()["detail"] == "repository_required"

    long_commit = _import(client, project_id, key="long-commit", commit="c" * 129)
    assert long_commit.status_code == 400
    assert long_commit.json()["detail"] == "commit_sha_too_long"

    wrong_repository = _import(client, project_id)
    assert wrong_repository.status_code == 409
    assert wrong_repository.json()["detail"] == "workspace_source_not_active"

    provider_pull = client.put(
        f"{PREFIX}/projects/{project_id}/source",
        headers={"If-Match": client.get(f"{PREFIX}/projects/{project_id}/source").headers["etag"]},
        json=_source_payload(import_mode="provider_pull"),
    )
    assert provider_pull.status_code == 200
    not_push = _import(client, project_id, key="import-provider-pull")
    assert not_push.status_code == 409
    assert not_push.json()["detail"] == "workspace_source_not_active"

    _install_fake_provider(client)
    provider_pull_source = client.get(f"{PREFIX}/projects/{project_id}/source")
    enabled_pull = client.post(
        f"{PREFIX}/projects/{project_id}/source:enable",
        headers={"If-Match": provider_pull_source.headers["etag"]},
    )
    assert enabled_pull.status_code == 200
    pull_import = _import(client, project_id, key="import-provider-pull-active")
    assert pull_import.status_code == 409
    assert pull_import.json()["detail"] == "source_import_mode_does_not_accept_push"


def test_source_transitions_cover_conflicts_and_provider_failures(client: TestClient) -> None:
    project_id = _create_project(client, "Workspace source transitions")
    no_source_match = client.post(
        f"{PREFIX}/projects/{project_id}/source:enable", headers={"If-Match": "*"}
    )
    assert no_source_match.status_code == 404

    no_source_capabilities = client.get(f"{PREFIX}/projects/{project_id}/source/capabilities")
    assert no_source_capabilities.status_code == 404

    missing_source_match = client.put(
        f"{PREFIX}/projects/{project_id}/source",
        headers={"If-Match": "*"},
        json=_source_payload(),
    )
    assert missing_source_match.status_code == 412
    assert missing_source_match.json()["detail"] == "source_not_configured"

    invalid_root = client.put(
        f"{PREFIX}/projects/{project_id}/source",
        json=_source_payload(root_path="../outside"),
    )
    assert invalid_root.status_code == 400
    assert invalid_root.json()["detail"].startswith("invalid_repository_path:")

    _source_id, etag = _configure_source(client, project_id)
    disabled = client.post(
        f"{PREFIX}/projects/{project_id}/source:disable", headers={"If-Match": etag}
    )
    assert disabled.status_code == 409
    assert disabled.json()["detail"] == "source_not_active"
    reconcile_disabled = client.post(
        f"{PREFIX}/projects/{project_id}/source:reconcile", headers={"If-Match": etag}
    )
    assert reconcile_disabled.status_code == 409
    assert reconcile_disabled.json()["detail"] == "source_not_active"

    _install_fake_provider(client)
    enabled = client.post(
        f"{PREFIX}/projects/{project_id}/source:enable", headers={"If-Match": etag}
    )
    assert enabled.status_code == 200
    active_etag = enabled.headers["etag"]
    enabled_again = client.post(
        f"{PREFIX}/projects/{project_id}/source:enable", headers={"If-Match": active_etag}
    )
    assert enabled_again.status_code == 409
    assert enabled_again.json()["detail"] == "source_not_disabled"

    active_replacement = client.put(
        f"{PREFIX}/projects/{project_id}/source",
        headers={"If-Match": active_etag},
        json=_source_payload(default_branch="trunk"),
    )
    assert active_replacement.status_code == 409
    assert active_replacement.json()["detail"] == "source_binding_active_disable_before_replacement"

    reconciled = client.post(
        f"{PREFIX}/projects/{project_id}/source:reconcile", headers={"If-Match": active_etag}
    )
    assert reconciled.status_code == 200
    assert reconciled.json()["data"]["source"]["status"] == "active"


def test_source_provider_failures_are_visible_and_fail_closed(client: TestClient) -> None:
    wrong_project = _create_project(client, "Workspace source identity failure")
    _configure_source(client, wrong_project)
    client.app.state.workspace_source_registry = WorkspaceSourceProviderRegistry(
        {"github": _WrongIdentityProvider()}
    )
    wrong_source = client.get(f"{PREFIX}/projects/{wrong_project}/source")
    wrong_enable = client.post(
        f"{PREFIX}/projects/{wrong_project}/source:enable",
        headers={"If-Match": wrong_source.headers["etag"]},
    )
    assert wrong_enable.status_code == 409
    assert "does not match" in wrong_enable.json()["detail"]
    wrong_state = client.get(f"{PREFIX}/projects/{wrong_project}/source")
    assert wrong_state.json()["data"]["source"]["status"] == "error"
    wrong_reconcile = client.post(
        f"{PREFIX}/projects/{wrong_project}/source:reconcile",
        headers={"If-Match": wrong_state.headers["etag"]},
    )
    assert wrong_reconcile.status_code == 409
    assert "does not match" in wrong_reconcile.json()["detail"]

    failing_project = _create_project(client, "Workspace source provider failure")
    _configure_source(client, failing_project)
    client.app.state.workspace_source_registry = WorkspaceSourceProviderRegistry(
        {"github": _FailingSourceProvider()}
    )
    failing_source = client.get(f"{PREFIX}/projects/{failing_project}/source")
    failed_enable = client.post(
        f"{PREFIX}/projects/{failing_project}/source:enable",
        headers={"If-Match": failing_source.headers["etag"]},
    )
    assert failed_enable.status_code == 409
    assert "temporarily unavailable" in failed_enable.json()["detail"]
    capabilities = client.get(f"{PREFIX}/projects/{failing_project}/source/capabilities")
    assert capabilities.status_code == 200
    assert capabilities.json()["data"]["available"] is False
    assert capabilities.json()["data"]["reason"] == "source_provider_unavailable"
    client.app.state.workspace_source_registry = object()
    with pytest.raises(RuntimeError, match="invalid type"):
        client.get(f"{PREFIX}/projects/{failing_project}/source/capabilities")


def test_manifest_and_multipart_helpers_fail_closed() -> None:
    valid_manifest = """apiVersion: caliber/v1alpha1
kind: Workspace
metadata:
  slug: helper-test
"""

    with pytest.raises(HTTPException) as bad_path:
        workspace_routes._manifest_from_bundle(b"", "../workspace.yaml")
    assert bad_path.value.status_code == 400
    assert bad_path.value.detail.startswith("invalid_manifest_path:")

    with pytest.raises(HTTPException) as bad_zip:
        workspace_routes._manifest_from_bundle(b"not a zip", "workspace.yaml")
    assert bad_zip.value.status_code == 400
    assert bad_zip.value.detail.startswith("manifest_read_failed:")

    def zip_with_manifest(content: str, *, include_directory: bool = False) -> bytes:
        output = BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            if include_directory:
                archive.writestr("nested/", "")
            archive.writestr("workspace.yaml", content)
        return output.getvalue()

    with pytest.raises(HTTPException) as missing:
        workspace_routes._manifest_from_bundle(zip_with_manifest(valid_manifest), "missing.yaml")
    assert missing.value.detail == "manifest_not_found: missing.yaml"

    with pytest.raises(HTTPException) as invalid_yaml:
        workspace_routes._manifest_from_bundle(
            zip_with_manifest("metadata: [unterminated"), "workspace.yaml"
        )
    assert invalid_yaml.value.detail.startswith("invalid_manifest_yaml:")

    with pytest.raises(HTTPException) as non_mapping:
        workspace_routes._manifest_from_bundle(zip_with_manifest("- scalar"), "workspace.yaml")
    assert non_mapping.value.detail == "manifest_must_decode_to_mapping"

    manifest, digest = workspace_routes._manifest_from_bundle(
        zip_with_manifest(valid_manifest, include_directory=True), "workspace.yaml"
    )
    assert manifest["kind"] == "Workspace"
    assert len(digest) == 64

    with pytest.raises(HTTPException) as missing_form:
        workspace_routes._form_text({}, "repository")
    assert missing_form.value.detail == "repository_required"
    assert workspace_routes._form_text({}, "repository", required=False) is None

    with pytest.raises(HTTPException) as non_text:
        workspace_routes._form_text({"repository": object()}, "repository")
    assert non_text.value.detail == "repository_must_be_text"

    with pytest.raises(HTTPException) as empty_text:
        workspace_routes._form_text({"repository": "  "}, "repository")
    assert empty_text.value.detail == "repository_required"


def test_cursor_and_page_validation_rejects_wrong_versions_and_bounds() -> None:
    old_cursor = base64.urlsafe_b64encode(
        json.dumps({"v": "workspace-v0", "created_at": "2026-09-17T12:00:00", "id": "row"}).encode()
    ).decode()
    with pytest.raises(HTTPException) as wrong_version:
        workspace_routes._parse_cursor(old_cursor)
    assert wrong_version.value.detail == "invalid_cursor"

    missing_id = base64.urlsafe_b64encode(
        json.dumps({"v": "workspace-v1", "created_at": "2026-09-17T12:00:00", "id": ""}).encode()
    ).decode()
    with pytest.raises(HTTPException) as no_id:
        workspace_routes._parse_cursor(missing_id)
    assert no_id.value.detail == "invalid_cursor"


def test_active_import_validates_repository_and_bundle_before_queueing(client: TestClient) -> None:
    project_id = _create_project(client, "Workspace active import validation")
    source_etag_value = _activate_source(client, project_id)

    mismatch = _import(
        client,
        project_id,
        key="repository-mismatch",
        repository="other-owner/other-repository",
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"] == "repository_does_not_match_source"

    invalid_bundle = _import(client, project_id, key="invalid-bundle", bundle=b"not a zip")
    assert invalid_bundle.status_code == 400
    assert invalid_bundle.json()["detail"].startswith("invalid_source_bundle:")

    missing_manifest = _import(
        client,
        project_id,
        key="missing-manifest",
        bundle=_bundle(manifest_path="other/workspace.yaml"),
    )
    assert missing_manifest.status_code == 400
    assert missing_manifest.json()["detail"] == "manifest_not_found: .caliber/workspace.yaml"

    assert source_etag_value


def test_import_requires_a_configured_source(client: TestClient) -> None:
    project_id = _create_project(client, "Workspace without source")
    response = _import(client, project_id, key="unconfigured")
    assert response.status_code == 409
    assert response.json()["detail"] == "workspace_source_not_configured"


def test_archived_workspace_and_missing_imports_are_rejected(
    client: TestClient, db_session: Session
) -> None:
    archived_id = _create_project(client, "Archived workspace")
    archived = db_session.get(CaliberProject, archived_id)
    assert archived is not None
    archived.status = "archived"
    db_session.commit()

    put = client.put(f"{PREFIX}/projects/{archived_id}/source", json=_source_payload())
    assert put.status_code == 409
    assert put.json()["detail"] == "archived_workspace"

    import_response = _import(client, archived_id, key="archived-import")
    assert import_response.status_code == 409
    assert import_response.json()["detail"] == "archived_workspace"

    missing_job = client.get(f"{PREFIX}/projects/{archived_id}/revision-imports/WSI-does-not-exist")
    assert missing_job.status_code == 404
    assert "not found" in missing_job.json()["detail"]


def test_import_list_uses_opaque_cursor_and_project_scope(client: TestClient) -> None:
    project_id = _create_project(client, "Workspace import pages")
    _configure_source(client, project_id)
    _install_fake_provider(client)
    source = client.get(f"{PREFIX}/projects/{project_id}/source").json()["data"]["source"]
    assert (
        client.post(
            f"{PREFIX}/projects/{project_id}/source:enable",
            headers={"If-Match": f'"{source["etag"]}"'},
        ).status_code
        == 200
    )
    first = _import(client, project_id, key="page-1", commit="page-1")
    second = _import(client, project_id, key="page-2", commit="page-2")
    assert first.status_code == second.status_code == 202

    page = client.get(f"{PREFIX}/projects/{project_id}/revision-imports?limit=1")
    assert page.status_code == 200
    assert len(page.json()["data"]["items"]) == 1
    cursor = page.json()["next_cursor"]
    assert cursor and "/" not in cursor
    next_page = client.get(
        f"{PREFIX}/projects/{project_id}/revision-imports?limit=1&cursor={cursor}"
    )
    assert next_page.status_code == 200
    assert len(next_page.json()["data"]["items"]) == 1
    assert next_page.json()["next_cursor"] is None

    invalid_cursor = client.get(
        f"{PREFIX}/projects/{project_id}/revision-imports?cursor=not-a-cursor"
    )
    assert invalid_cursor.status_code == 400
    assert invalid_cursor.json()["detail"] == "invalid_cursor"

    queued = client.get(f"{PREFIX}/projects/{project_id}/revision-imports?status=queued")
    assert queued.status_code == 200
    assert len(queued.json()["data"]["items"]) == 2
    invalid_status = client.get(f"{PREFIX}/projects/{project_id}/revision-imports?status=unknown")
    assert invalid_status.status_code == 400
    assert invalid_status.json()["detail"] == "invalid_import_status"
    invalid_limit = client.get(f"{PREFIX}/projects/{project_id}/revision-imports?limit=oops")
    assert invalid_limit.status_code == 400
    assert invalid_limit.json()["detail"] == "invalid_limit"
    out_of_range = client.get(f"{PREFIX}/projects/{project_id}/revision-imports?limit=0")
    assert out_of_range.status_code == 400
    assert out_of_range.json()["detail"] == "limit_must_be_between_1_and_100"


def test_import_reconcile_observes_snapshot_without_blind_retry(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client, "Workspace import recovery")
    _configure_source(client, project_id)
    _install_fake_provider(client)
    source = client.get(f"{PREFIX}/projects/{project_id}/source").json()["data"]["source"]
    assert (
        client.post(
            f"{PREFIX}/projects/{project_id}/source:enable",
            headers={"If-Match": f'"{source["etag"]}"'},
        ).status_code
        == 200
    )
    response = _import(client, project_id)
    job_id = response.json()["data"]["import_job_id"]
    job = db_session.get(CaliberWorkspaceImportJob, job_id)
    assert job is not None
    job.status = "reconcile_required"
    job.error_code = "worker_lost"
    db_session.commit()

    observed = client.post(f"{PREFIX}/projects/{project_id}/revision-imports/{job_id}:reconcile")
    assert observed.status_code == 200, observed.text
    assert observed.json()["data"]["observed"] is True
    assert observed.json()["data"]["observation"] == "local_snapshot_intact"
    assert observed.json()["data"]["job"]["status"] == "reconcile_required"
    db_session.refresh(job)
    assert job.status == "reconcile_required"
    assert job.error_code == "worker_lost"


def test_import_reconcile_reports_each_snapshot_integrity_failure(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client, "Workspace import integrity")
    _activate_source(client, project_id)
    response = _import(client, project_id, key="integrity")
    assert response.status_code == 202
    job_id = response.json()["data"]["import_job_id"]
    job = db_session.get(CaliberWorkspaceImportJob, job_id)
    assert job is not None

    queued = client.post(f"{PREFIX}/projects/{project_id}/revision-imports/{job_id}:reconcile")
    assert queued.status_code == 409
    assert queued.json()["detail"] == "import_not_reconcile_required"

    snapshot_id = job.source_snapshot_file_id
    job.status = "reconcile_required"
    job.source_snapshot_file_id = None
    db_session.commit()
    no_snapshot = client.post(f"{PREFIX}/projects/{project_id}/revision-imports/{job_id}:reconcile")
    assert no_snapshot.status_code == 409
    assert no_snapshot.json()["detail"] == "import_snapshot_unavailable"

    assert snapshot_id is not None
    job.source_snapshot_file_id = "WFF-missing"
    db_session.commit()
    missing_snapshot = client.post(
        f"{PREFIX}/projects/{project_id}/revision-imports/{job_id}:reconcile"
    )
    assert missing_snapshot.status_code == 409
    assert missing_snapshot.json()["detail"] == "import_snapshot_unavailable"

    job.source_snapshot_file_id = snapshot_id
    snapshot = db_session.get(CaliberWorkflowFile, snapshot_id)
    assert snapshot is not None
    original_sha = snapshot.sha256
    original_metadata = dict(snapshot.file_metadata or {})

    snapshot.sha256 = "0" * 64
    db_session.commit()
    bad_digest = client.post(f"{PREFIX}/projects/{project_id}/revision-imports/{job_id}:reconcile")
    assert bad_digest.status_code == 409
    assert bad_digest.json()["detail"] == "import_snapshot_digest_mismatch"

    snapshot.sha256 = original_sha
    snapshot.file_metadata = {**original_metadata, "snapshot_sha256": "wrong"}
    db_session.commit()
    bad_metadata = client.post(
        f"{PREFIX}/projects/{project_id}/revision-imports/{job_id}:reconcile"
    )
    assert bad_metadata.status_code == 409
    assert bad_metadata.json()["detail"] == "import_snapshot_metadata_mismatch"

    class BrokenStorage:
        def read_bytes(self, _row: CaliberWorkflowFile) -> bytes:
            raise StorageError("storage temporarily unavailable")

    snapshot.file_metadata = original_metadata
    db_session.commit()
    client.app.state.working_dir_service = BrokenStorage()
    unavailable = client.post(f"{PREFIX}/projects/{project_id}/revision-imports/{job_id}:reconcile")
    assert unavailable.status_code == 409
    assert "import_snapshot_unavailable" in unavailable.json()["detail"]


def _seed_revisions(
    session: Session, project_id: str, *, prefix: str = "revision"
) -> tuple[CaliberWorkspaceRevision, CaliberWorkspaceRevision]:
    base = CaliberWorkspaceRevision(
        revision_id=f"WSR-{prefix}-base",
        project_id=project_id,
        revision_number=1,
        source_commit_sha="commit-base",
        manifest={"apiVersion": "caliber/v1alpha1", "value": "base"},
        manifest_sha256="a" * 64,
        source_bundle_sha256="b" * 64,
        revision_sha256="c" * 64,
        source_attestation="caller_attested",
        status="validating",
        created_by="@test",
        created_at=datetime(2026, 9, 17, 12, 0, 0),
    )
    candidate = CaliberWorkspaceRevision(
        revision_id=f"WSR-{prefix}-candidate",
        project_id=project_id,
        revision_number=2,
        source_commit_sha="commit-candidate",
        manifest={"apiVersion": "caliber/v1alpha1", "value": "candidate"},
        manifest_sha256="d" * 64,
        source_bundle_sha256="e" * 64,
        revision_sha256="f" * 64,
        source_attestation="caller_attested",
        status="validating",
        created_by="@test",
        created_at=datetime(2026, 9, 17, 12, 1, 0),
    )
    session.add_all([base, candidate])
    session.flush()
    session.add_all(
        [
            CaliberWorkspaceRevisionResource(
                resource_pin_id=f"WSRR-{prefix}-same",
                revision_id=base.revision_id,
                resource_type="workflows",
                logical_name="same",
                resource_id="wf-1",
                version_ref="v1",
                content_sha256="1" * 64,
                purpose="runtime",
            ),
            CaliberWorkspaceRevisionResource(
                resource_pin_id=f"WSRR-{prefix}-changed-base",
                revision_id=base.revision_id,
                resource_type="workflows",
                logical_name="changed",
                resource_id="wf-2",
                version_ref="v1",
                content_sha256="2" * 64,
                purpose="runtime",
            ),
            CaliberWorkspaceRevisionResource(
                resource_pin_id=f"WSRR-{prefix}-same-candidate",
                revision_id=candidate.revision_id,
                resource_type="workflows",
                logical_name="same",
                resource_id="wf-1",
                version_ref="v1",
                content_sha256="1" * 64,
                purpose="runtime",
            ),
            CaliberWorkspaceRevisionResource(
                resource_pin_id=f"WSRR-{prefix}-changed-candidate",
                revision_id=candidate.revision_id,
                resource_type="workflows",
                logical_name="changed",
                resource_id="wf-2",
                version_ref="v2",
                content_sha256="3" * 64,
                purpose="runtime",
            ),
            CaliberWorkspaceRevisionResource(
                resource_pin_id=f"WSRR-{prefix}-added",
                revision_id=candidate.revision_id,
                resource_type="workflows",
                logical_name="added",
                resource_id="wf-3",
                version_ref="v1",
                content_sha256="4" * 64,
                purpose="runtime",
            ),
        ]
    )
    session.flush()
    base.status = "ready"
    candidate.status = "ready"
    session.commit()
    return base, candidate


def test_revision_list_detail_and_diff_are_project_scoped_and_cursor_paged(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client, "Workspace revisions")
    other_project_id = _create_project(client, "Workspace revisions other")
    base, candidate = _seed_revisions(db_session, project_id)
    other_base, _ = _seed_revisions(db_session, other_project_id, prefix="other")

    page = client.get(f"{PREFIX}/projects/{project_id}/revisions?limit=1")
    assert page.status_code == 200
    assert [item["revision_id"] for item in page.json()["data"]["items"]] == [candidate.revision_id]
    cursor = page.json()["next_cursor"]
    assert cursor
    second_page = client.get(f"{PREFIX}/projects/{project_id}/revisions?limit=1&cursor={cursor}")
    assert [item["revision_id"] for item in second_page.json()["data"]["items"]] == [
        base.revision_id
    ]
    ready = client.get(f"{PREFIX}/projects/{project_id}/revisions?status=ready")
    assert ready.status_code == 200
    assert len(ready.json()["data"]["items"]) == 2
    invalid_status = client.get(f"{PREFIX}/projects/{project_id}/revisions?status=unknown")
    assert invalid_status.status_code == 400
    assert invalid_status.json()["detail"] == "invalid_revision_status"
    invalid_limit = client.get(f"{PREFIX}/projects/{project_id}/revisions?limit=bad")
    assert invalid_limit.status_code == 400
    assert invalid_limit.json()["detail"] == "invalid_limit"
    out_of_range = client.get(f"{PREFIX}/projects/{project_id}/revisions?limit=101")
    assert out_of_range.status_code == 400
    assert out_of_range.json()["detail"] == "limit_must_be_between_1_and_100"

    detail = client.get(f"{PREFIX}/projects/{project_id}/revisions/{candidate.revision_id}")
    assert detail.status_code == 200
    assert {r["logical_name"] for r in detail.json()["data"]["resources"]} == {
        "same",
        "changed",
        "added",
    }
    hidden = client.get(f"{PREFIX}/projects/{other_project_id}/revisions/{candidate.revision_id}")
    assert hidden.status_code == 404
    assert other_base.revision_id != candidate.revision_id
    missing_revision = client.get(f"{PREFIX}/projects/{project_id}/revisions/WSR-does-not-exist")
    assert missing_revision.status_code == 404

    missing_base = client.get(
        f"{PREFIX}/projects/{project_id}/revisions/{candidate.revision_id}/diff"
    )
    assert missing_base.status_code == 400
    diff = client.get(
        f"{PREFIX}/projects/{project_id}/revisions/{candidate.revision_id}/diff?base={base.revision_id}"
    )
    assert diff.status_code == 200, diff.text
    payload = diff.json()["data"]
    assert payload["manifest_changed"] is True
    assert payload["source_bundle_changed"] is True
    assert payload["source_commit_changed"] is True
    assert [resource["logical_name"] for resource in payload["added"]] == ["added"]
    assert [resource["logical_name"] for resource in payload["removed"]] == []
    assert [resource["logical_name"] for resource in payload["changed"]] == ["changed"]
    missing_base_id = client.get(
        f"{PREFIX}/projects/{project_id}/revisions/{candidate.revision_id}/diff?base=WSR-missing"
    )
    assert missing_base_id.status_code == 404


def test_source_etag_omits_connection_secret_material(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client, "Workspace etag")
    _configure_source(client, project_id)
    response = client.get(f"{PREFIX}/projects/{project_id}/source")
    source_id = response.json()["data"]["source"]["source_id"]
    source = db_session.get(CaliberWorkspaceSource, source_id)
    assert source is not None
    before = source_etag(source)
    source.connection_ref = "secret://a-different-secret"
    changed_connection = source_etag(source)
    assert changed_connection != before
    assert "a-different-secret" not in changed_connection
    source.status = "active"
    assert source_etag(source) != changed_connection
