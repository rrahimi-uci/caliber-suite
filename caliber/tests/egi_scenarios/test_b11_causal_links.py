"""B11.3b (#745) on the public path: a governed effect's causal links, read back through the API.

The reference-profile app (PostgreSQL; the development profile on SQLite). A run is admitted through the public route, an
effect is proposed and approved, and the real dispatcher performs it against the independent downstream. The causal-links
endpoint is then read as the project's editor and answers with the persisted rows' ids, and those ids are the ones the
effect's spans carried. A caller from another project, and a caller who names a run that does not exist, get the same
``404``, and reading changes nothing downstream.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from caliber.db.models import CaliberProjectMember, CaliberRuntimeApprovalRequest
from caliber.effects.dispatcher import dispatch
from caliber.egress import EgressPolicy
from caliber.integrations.openapi.executor import bind_egress_policy
from caliber.observability.mlflow_tracing import Tracer, set_tracer
from caliber.ownership import acquire
from caliber.workflows.approval_policy import approval_valid_at_commit
from tests.egi_scenarios.conftest import (
    APPROVER_A,
    EDITOR_A,
    EDITOR_B,
    PROJECT_A,
    PROJECT_B,
    Downstream,
    ReferenceApp,
    Workspaces,
)
from tests.egi_scenarios.test_b02_admission import _call
from tests.egi_scenarios.test_v04_stale_approval import EVIDENCE, T0, _request
from tests.egi_scenarios.test_v05_revocation_while_waiting import _approved, _waiting_run
from tests.test_mlflow_tracing import FakeMlflow

CAUSAL_KEYS = (
    "caliber.proposal_id",
    "caliber.decision_id",
    "caliber.approval_id",
    "caliber.effect_id",
)
SECRET_CASE = "MORT-2026-000184"
SECTIONS = ("effects", "context_manifests", "policy_decisions")


@dataclass
class Armed:
    """One approved effect that has not been dispatched yet."""

    app: ReferenceApp
    run_id: str
    proposal_id: str
    approval_id: str


def _links(app: ReferenceApp, user: str, project: str, run_id: str, query: str = "") -> Any:
    return _call(app, "GET", user, project, f"/workflow-runs/{run_id}/causal-links{query}")


def _only_effect(app: ReferenceApp, run_id: str) -> dict[str, Any]:
    response = _links(app, EDITOR_A, PROJECT_A, run_id)
    assert response.status_code == 200, response.text
    (effect,) = response.json()["data"]["effects"]
    return effect  # type: ignore[no-any-return]


def _shape(text: str, run_id: str) -> str:
    return re.sub(r"request_id=\w+", "", text).replace(run_id, "RUN")


@pytest.fixture
def armed(reference_app: ReferenceApp, two_workspaces: Workspaces) -> Armed:
    with reference_app.session_factory() as session:
        member = session.execute(
            select(CaliberProjectMember).where(
                CaliberProjectMember.project_id == PROJECT_A,
                CaliberProjectMember.user_id == APPROVER_A,
            )
        ).scalar_one()
        member.role = "reviewer"
        session.commit()
    run_id = _waiting_run(reference_app)
    approval_id, proposal_id = _request(
        reference_app,
        run_id,
        "occ-b11-links",
        connector_version="1",
        resource=f"cases/{SECRET_CASE}/document-requests",
        args={"document_type": "pay_statement"},
        expected_resource_version="1",
    )
    _approved(reference_app, run_id, approval_id)
    return Armed(reference_app, run_id, proposal_id, approval_id)


@pytest.fixture
def spans() -> Iterator[FakeMlflow]:
    recorder = FakeMlflow()
    set_tracer(Tracer(mlflow_module=recorder))
    yield recorder
    set_tracer(None)


def _dispatch(case: Armed, downstream: Downstream) -> Any:
    with case.app.session_factory() as session:
        lease = acquire(session, "egi-b11-links", ttl_seconds=600)
        assert lease is not None
        session.commit()
    bind_egress_policy(EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})))
    try:
        return dispatch(
            case.app.session_factory,
            case.proposal_id,
            approval_id=case.approval_id,
            approver_scopes=frozenset({"caliber.approver", "caliber.viewer"}),
            lease=lease,
            backend_kind="document_request",
            backend_config={"base_url": downstream.base_url},
            current_resource_version="1",
            evidence_hash=EVIDENCE,
            approval_validator=approval_valid_at_commit,
            now=T0 + timedelta(minutes=2),
        )
    finally:
        bind_egress_policy(None)


def test_before_the_dispatch_the_effect_is_named_but_has_no_effect_id(armed: Armed) -> None:
    waiting = _only_effect(armed.app, armed.run_id)
    assert waiting["proposal_id"] == armed.proposal_id and waiting["state"] == "proposed"
    approval = waiting["approval"]
    assert (approval["status"], approval["approval_id"]) == ("pending", armed.approval_id)
    assert approval["request_status"] == "approved" and approval["consumed_at"] is None
    assert (waiting["effect"]["status"], waiting["effect"]["code"]) == ("pending", "not_dispatched")
    assert waiting["effect"]["effect_id"] is None
    assert waiting["missing_links"] == [], "approved work waiting for its dispatch is not a broken chain"
    assert "caliber.effect_id" not in waiting["span_attributes"]


def test_a_dispatched_effect_names_the_rows_its_spans_carried(
    armed: Armed, downstream: Downstream, spans: FakeMlflow
) -> None:
    outcome = _dispatch(armed, downstream)
    assert outcome.state == "committed" and downstream.mutations() == 1

    response = _links(armed.app, EDITOR_A, PROJECT_A, armed.run_id)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    (effect,) = data["effects"]
    assert effect["proposal_id"] == armed.proposal_id and effect["state"] == "committed"
    approval = effect["approval"]
    assert approval["status"] == "linked" and approval["approval_id"] == armed.approval_id
    assert approval["consumed_at"] is not None, "the dispatcher consumed it"
    assert effect["effect"]["status"] == "linked" and effect["effect"]["committed"] is True
    assert effect["decision"]["status"] == "unrecorded", (
        "the commit-time decision is stored per run"
    )
    assert effect["missing_links"] == [], "a committed, receipted, approved effect is not broken"
    assert effect["unrecorded_links"] == ["context_manifest", "decision"]

    # the same ids the spans of that dispatch carried (B11.3a)
    recorded = [s for s in spans.spans if s.name.startswith("effect.")]
    assert {s.name for s in recorded} >= {
        "effect.dispatch",
        "effect.approval.commit_check",
        "effect.receipt.write",
    }
    on_spans: dict[str, set[Any]] = {key: set() for key in CAUSAL_KEYS}
    for span in recorded:
        for key in CAUSAL_KEYS:
            if key in span.attributes:
                on_spans[key].add(span.attributes[key])
    api = effect["span_attributes"]
    assert set(api) == {"caliber.proposal_id", "caliber.approval_id", "caliber.effect_id"}
    for key in api:
        assert on_spans[key] == {api[key]}, key
    assert api["caliber.effect_id"] == effect["effect"]["effect_id"]

    # the decision the spans name is a persisted row of this run, listed by the decision section
    (span_decision,) = on_spans["caliber.decision_id"]
    decisions = _links(
        armed.app, EDITOR_A, PROJECT_A, armed.run_id, "?section=policy_decisions&limit=200"
    )
    assert decisions.status_code == 200, decisions.text
    listed = {d["decision_id"]: d for d in decisions.json()["data"]["policy_decisions"]}
    assert span_decision in listed and listed[span_decision]["result"] == "allow"

    # the run manifest is the one the admission endpoint serves, by id and digest
    admission = _call(
        armed.app, "GET", EDITOR_A, PROJECT_A, f"/workflow-runs/{armed.run_id}/admission-manifest"
    ).json()["data"]
    assert data["run_manifest"]["status"] == "linked"
    assert data["run_manifest"]["manifest_id"] == admission["manifest_id"]
    assert data["run_manifest"]["manifest_digest"] == admission["manifest_digest"]
    assert data["acceptance"]["status"] == "not_available"


def test_no_section_carries_the_case_the_people_or_the_arguments(
    armed: Armed, downstream: Downstream
) -> None:
    _dispatch(armed, downstream)
    blob = " ".join(
        _links(armed.app, EDITOR_A, PROJECT_A, armed.run_id, f"?section={name}&limit=200").text
        for name in SECTIONS
    )
    for secret in (SECRET_CASE, "pay_statement", EDITOR_A, APPROVER_A, "cases/"):
        assert secret not in blob, secret


def test_another_project_and_an_unknown_run_get_the_same_404_and_reading_changes_nothing(
    armed: Armed, downstream: Downstream
) -> None:
    _dispatch(armed, downstream)
    foreign = _links(armed.app, EDITOR_B, PROJECT_B, armed.run_id)
    claiming = _links(armed.app, EDITOR_B, PROJECT_A, armed.run_id)
    unknown = _links(armed.app, EDITOR_B, PROJECT_B, "WFR-does-not-exist")
    assert (foreign.status_code, claiming.status_code, unknown.status_code) == (404, 404, 404)
    shapes = {_shape(r.text, armed.run_id) for r in (foreign, claiming)}
    shapes.add(_shape(unknown.text, "WFR-does-not-exist"))
    assert len(shapes) == 1, shapes
    for name in SECTIONS:
        assert (
            _links(armed.app, EDITOR_A, PROJECT_A, armed.run_id, f"?section={name}").status_code
            == 200
        )
    # reading is read-only: the downstream was not asked again and the request is as it was
    assert downstream.mutations() == 1
    with armed.app.session_factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, armed.approval_id)
        assert row is not None and row.consumed_at is not None
