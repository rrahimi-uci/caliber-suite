"""D3 / #1088: generated workflow source stays inside the reference deployment.

The route-level unit tests prove the profile filter and development compatibility.  These public-path
scenarios add the deployment boundary: a migrated app on PostgreSQL must redact the generated source from
compile, detail and publish responses while retaining the immutable compiled bundle for the platform, and a
member of another project must not use those routes to discover the version or source.
"""

from __future__ import annotations

import pytest

from caliber.db.models import CaliberWorkflowVersion
from tests.egi_scenarios.conftest import (
    EDITOR_A,
    EDITOR_B,
    PROJECT_A,
    PROJECT_B,
    ReferenceApp,
    Workspaces,
)
from tests.egi_scenarios.test_b02_admission import _call
from tests.workflow_helpers import make_manifest


def _create_version(app: ReferenceApp) -> tuple[str, str]:
    created = _call(app, "POST", EDITOR_A, PROJECT_A, "/workflows", {"name": "D3 source boundary"})
    assert created.status_code == 201, created.text
    workflow_id = created.json()["data"]["workflow_id"]
    draft = _call(
        app,
        "POST",
        EDITOR_A,
        PROJECT_A,
        f"/workflows/{workflow_id}/versions",
        {"manifest": make_manifest(workflow_id)},
    )
    assert draft.status_code == 201, draft.text
    return workflow_id, draft.json()["data"]["version_id"]


def test_reference_public_version_routes_redact_source_but_keep_storage(
    reference_app: ReferenceApp,
    two_workspaces: Workspaces,
) -> None:
    if reference_app.profile != "reference":
        pytest.skip("source redaction is a reference-profile contract")
    _workflow_id, version_id = _create_version(reference_app)

    compiled = _call(
        reference_app,
        "POST",
        EDITOR_A,
        PROJECT_A,
        f"/workflow-versions/{version_id}/compile",
    )
    assert compiled.status_code == 200, compiled.text
    assert "generated_python" not in compiled.text
    assert "def run" not in compiled.text

    detail = _call(
        reference_app,
        "GET",
        EDITOR_A,
        PROJECT_A,
        f"/workflow-versions/{version_id}",
    )
    assert detail.status_code == 200, detail.text
    assert "generated_python" not in detail.text
    assert "def run" not in detail.text

    published = _call(
        reference_app,
        "POST",
        EDITOR_A,
        PROJECT_A,
        f"/workflow-versions/{version_id}/publish",
    )
    assert published.status_code == 200, published.text
    assert "generated_python" not in published.text
    assert "def run" not in published.text

    with reference_app.session_factory() as session:
        stored = session.get(CaliberWorkflowVersion, version_id)
        assert stored is not None
        bundle = stored.compiled_bundle or {}
        assert "def run" in bundle.get("generated_python", "")


def test_foreign_project_cannot_discover_version_or_source(
    reference_app: ReferenceApp,
    two_workspaces: Workspaces,
) -> None:
    _workflow_id, version_id = _create_version(reference_app)

    for method, suffix in (
        ("GET", f"/workflow-versions/{version_id}"),
        ("POST", f"/workflow-versions/{version_id}/compile"),
        ("POST", f"/workflow-versions/{version_id}/publish"),
    ):
        response = _call(reference_app, method, EDITOR_B, PROJECT_B, suffix)
        assert response.status_code == 404, response.text
        assert version_id not in response.text
        assert "def run" not in response.text
