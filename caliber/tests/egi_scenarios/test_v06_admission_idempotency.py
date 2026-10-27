"""V06 public path: an idempotency key names one request, and only that request (B02.3, #687).

The reference-profile app on PostgreSQL (the same logic on SQLite in the development profile with admission switched
on). A project editor starts a run through the public API with an idempotency key. The manifest stores the SHA-256 of
the canonical request (workflow, the selector as named, input, purpose, server-derived scope). The same request again
returns the original run and manifest even after the version the selector resolves to has moved; a changed request
under the key is a 409 and stores nothing; and requests racing on one key, whether alike or different, admit exactly
one run.
"""

from __future__ import annotations

import threading
from typing import Any

from sqlalchemy import func, select

from caliber.db.models import CaliberRunManifest, CaliberWorkflowRun
from caliber.run_manifest import read_manifest
from caliber.workflows.admission import request_digest_of
from tests.egi_scenarios.conftest import (
    EDITOR_A,
    PROJECT_A,
    ReferenceApp,
    Workspaces,
    seed_release,
)
from tests.egi_scenarios.test_b02_admission import _call, _published_workflow
from tests.workflow_helpers import make_manifest

KEY = "egi-v06"


def _start(app: ReferenceApp, workflow_id: str, **body: Any) -> Any:
    return _call(
        app,
        "POST",
        EDITOR_A,
        PROJECT_A,
        "/workflow-runs",
        {
            "workflow_id": workflow_id,
            "alias": "manual",
            "idempotency_key": KEY,
            **seed_release(app, PROJECT_A),
            **body,
        },
    )


def _stored(app: ReferenceApp) -> tuple[int, int]:
    with app.session_factory() as session:
        runs = session.execute(select(func.count()).select_from(CaliberWorkflowRun)).scalar_one()
        manifests = session.execute(
            select(func.count()).select_from(CaliberRunManifest)
        ).scalar_one()
    return int(runs), int(manifests)


def test_the_manifest_stores_the_request_digest_and_a_moved_selector_returns_the_original(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    workflow_id = _published_workflow(reference_app)
    first = _start(reference_app, workflow_id, input="refund?")
    assert first.status_code == 202, first.text
    run_id = first.json()["data"]["workflow_run_id"]
    with reference_app.session_factory() as session:
        row = session.execute(select(CaliberRunManifest)).scalar_one()
        record = read_manifest(bytes(row.manifest_bytes), row.manifest_digest)
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        intent = run.summary["admission_intent"]
        original_version = record["pins"]["workflow_version"]
        original_digest = row.manifest_digest
    assert record["request_digest"] == request_digest_of(intent)
    assert intent["purpose"] == record["purpose"] == "workflow_run:api"
    assert intent["scope"]["project_id"] == record["project_id"] == PROJECT_A
    assert intent["release_selector"] == "manual", "the selector as named, not what it resolved to"

    # The selector now resolves to a newer version.
    draft = _call(
        reference_app,
        "POST",
        EDITOR_A,
        PROJECT_A,
        f"/workflows/{workflow_id}/versions",
        {"manifest": make_manifest(workflow_id)},
    )
    assert draft.status_code == 201, draft.text
    newer = draft.json()["data"]["version_id"]
    assert newer != original_version
    published = _call(
        reference_app, "POST", EDITOR_A, PROJECT_A, f"/workflow-versions/{newer}/publish"
    )
    assert published.status_code == 200, published.text

    again = _start(reference_app, workflow_id, input="refund?")
    assert again.status_code == 202 and again.json()["data"]["workflow_run_id"] == run_id
    assert _stored(reference_app) == (1, 1), "no second run and no second manifest"
    with reference_app.session_factory() as session:
        assert session.execute(select(CaliberRunManifest)).scalar_one().manifest_digest == (
            original_digest
        )

    for changed in (
        {"input": "something else"},
        {
            "input": "refund?",
            "workflow_version_id": newer,
        },  # a different selector is a different request
    ):
        refused = _start(reference_app, workflow_id, **changed)
        assert refused.status_code == 409, refused.text
        assert "different request" in refused.json()["detail"]
    assert _stored(reference_app) == (1, 1), "a changed request stores nothing"


def test_requests_racing_on_one_key_admit_exactly_one_run(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    workflow_id = _published_workflow(reference_app)
    barrier = threading.Barrier(6)
    results: list[tuple[str, int, str]] = []
    lock = threading.Lock()

    def send(text: str) -> None:
        barrier.wait(10)
        response = _start(reference_app, workflow_id, input=text)
        run_id = response.json()["data"]["workflow_run_id"] if response.status_code == 202 else ""
        with lock:
            results.append((text, response.status_code, run_id))

    threads = [
        threading.Thread(target=send, args=(text,), daemon=True)
        for text in ("alpha", "alpha", "alpha", "beta", "beta", "beta")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert len(results) == 6, results
    assert {status for _, status, _ in results} <= {202, 409}, results
    winners = {text for text, status, _ in results if status == 202}
    assert len(winners) == 1, "only the request that created the run is answered with it"
    assert len({run_id for _, status, run_id in results if status == 202}) == 1
    assert {text for text, status, _ in results if status == 409} == ({"alpha", "beta"} - winners)
    assert _stored(reference_app) == (1, 1)
