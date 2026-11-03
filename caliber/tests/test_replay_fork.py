"""B09.5 counterfactual forks are separately identified, bounded, and isolated."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from caliber.acceptance.completion import CRITERIA_BY_CONNECTOR
from caliber.acceptance.decision import record_decision
from caliber.db.models import (
    CaliberProject,
    CaliberRunManifest,
    CaliberWorkflow,
    CaliberWorkflowRun,
)
from caliber.effects.records import Intent, record_proposal
from caliber.replay import runner
from caliber.replay.observations import record_observation
from caliber.replay.runner import (
    FORK_ADAPTER,
    FORK_AUTHORIZATION,
    FORK_BUDGET,
    FORK_PINS,
    FORK_SOURCE,
    CounterfactualBudget,
    CounterfactualError,
    ScriptedCandidateAdapter,
    _adapter_info,
    _candidate_case,
    _fork_budget,
    _fork_manifest,
    _fork_pins,
    fork_counterfactual,
)
from caliber.run_manifest import canonical_bytes, digest_of
from caliber.storage.local import LocalStorageBackend

PROJECT = "PRJ-fork"
RUN_ID = "WR-source"
PRINCIPAL = "editor_a"
CASE_ID = "MORT-1"
PROPOSAL_ID = "prop-source"


def _manifest(run_id: str) -> CaliberRunManifest:
    record: dict[str, Any] = {
        "schema_version": "v1",
        "manifest_id": f"mf-{run_id}",
        "workflow_run_id": run_id,
        "tenant_id": "local",
        "region": "local",
        "project_id": PROJECT,
        "pins": {
            "workflow_version": "version-1",
            "bundle_digest": "bundle-1",
            "prompt_versions": [],
            "tool_versions": [],
            "skill_versions": [],
            "knowledge_base_versions": [],
            "model_routes": ["fake/model"],
            "policy_version": "policy-1",
        },
        "subject": {"user_id": PRINCIPAL, "auth_method": "test"},
        "workload": {
            "workload_id": "workload-1",
            "name": "fork-test",
            "workflow_version_digest": "bundle-1",
        },
        "purpose": "workflow",
        "legacy": False,
        "created_at": "2026-11-03T00:00:00Z",
        "grant_id": None,
        "request_digest": None,
    }
    payload = canonical_bytes(record)
    return CaliberRunManifest(
        manifest_id=str(record["manifest_id"]),
        workflow_run_id=run_id,
        schema_version="v1",
        manifest_bytes=payload,
        manifest_digest=digest_of(payload),
    )


def _case(version: int) -> dict[str, Any]:
    return {
        "case_id": CASE_ID,
        "document_requests": [
            {
                "request_id": "REQ-1",
                "document_type": "pay_statement",
                "case_version": version,
            }
        ],
    }


def _source_case_observations(*, case: dict[str, Any]) -> dict[str, Any]:
    return {
        "proposal": {
            "case_id": CASE_ID,
            "document_type": "pay_statement",
            "expected_version": 1,
        },
        "receipt": {"downstream_request_id": "REQ-1"},
        "case_record": case,
    }


def test_fork_leaves_source_untouched(
    db_session: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fork writes only new-run evidence and never dispatches or edits its source."""

    # Import the registry only here so the fixture can stay independent of
    # connector implementation details while recording the real source row.
    from caliber.acceptance.completion import CRITERIA_BY_CONNECTOR

    backend = LocalStorageBackend(f"file://{tmp_path / 'objects'}")
    source = CaliberWorkflowRun(
        workflow_run_id=RUN_ID,
        workflow_id="workflow-fork",
        project_id=PROJECT,
        workflow_version_id="version-1",
        status="completed",
        source="manual",
        created_by=PRINCIPAL,
        manifest_legacy=False,
        summary=None,
    )
    db_session.add_all(
        [
            CaliberProject(project_id=PROJECT, name="Fork", owner="@test"),
            CaliberWorkflow(
                workflow_id="workflow-fork", project_id=PROJECT, name="Fork", owner="@test"
            ),
            source,
        ]
    )
    db_session.flush()
    db_session.add(_manifest(RUN_ID))
    proposal = record_proposal(
        db_session,
        workflow_run_id=RUN_ID,
        node_id="request",
        occurrence_id="1",
        intent=Intent(
            connector="document-request",
            connector_version="1.0.0",
            effect_class="irreversible_write",
            resource=f"cases/{CASE_ID}/document-requests",
            args={"case_id": CASE_ID, "document_type": "pay_statement"},
            expected_resource_version="1",
        ),
    )
    record_observation(
        db_session,
        backend,
        workflow_run_id=RUN_ID,
        project_id=PROJECT,
        principal=PRINCIPAL,
        node_id="reader",
        occurrence_id="case-read",
        attempt=1,
        observation_kind="read",
        request={"method": "GET", "path": f"/cases/{CASE_ID}"},
        content=_case(1),
        provider="fixture",
        provider_version="v1",
    )
    source_observations = _source_case_observations(case=_case(1))
    original = record_decision(
        db_session,
        workflow_run_id=RUN_ID,
        candidate_id=proposal.proposal_id,
        predicates=CRITERIA_BY_CONNECTOR["document-request"].predicates,
        observations=source_observations,
    ).decision
    source.summary = {
        "acceptance": {
            "decisions": [
                {
                    "decision_id": original.decision_id,
                    "candidate_id": proposal.proposal_id,
                    "outcome": original.outcome,
                    "observation_digest": original.observation_digest,
                    "failed_criteria": list(original.failed_criteria),
                }
            ]
        }
    }
    db_session.commit()

    monkeypatch.setattr(
        "caliber.replay.runner.observations_for_proposal",
        lambda _session, _proposal_id, *, case_record=None: {
            **source_observations,
            "case_record": source_observations["case_record"]
            if case_record is None
            else case_record,
        },
    )
    source_before = {
        "summary": copy.deepcopy(source.summary),
        "status": source.status,
        "source_run_id": source.source_run_id,
        "counterfactual_spec": source.counterfactual_spec,
    }
    report = fork_counterfactual(
        db_session,
        backend,
        run=source,
        project_id=PROJECT,
        principal="operator_a",
        changed_pins={"model_routes": ["fake/counterfactual"]},
        candidate_adapter=ScriptedCandidateAdapter(
            {CASE_ID: {"case_id": CASE_ID, "document_requests": []}}
        ),
        source_principal=PRINCIPAL,
        now=datetime(2026, 11, 3, tzinfo=timezone.utc),
    )
    db_session.commit()
    db_session.expire_all()
    stored_source = db_session.get(CaliberWorkflowRun, RUN_ID)
    assert stored_source is not None
    assert {
        "summary": stored_source.summary,
        "status": stored_source.status,
        "source_run_id": stored_source.source_run_id,
        "counterfactual_spec": stored_source.counterfactual_spec,
    } == source_before
    fork = db_session.get(CaliberWorkflowRun, report.workflow_run_id)
    assert fork is not None
    assert fork.source_run_id == RUN_ID
    assert fork.counterfactual_spec["changed_pins"] == {"model_routes": ["fake/counterfactual"]}
    assert fork.counterfactual_spec["protected_writes"] == "disabled"
    assert report.protected_writes is False
    assert report.workflow_run_id != RUN_ID
    assert report.decisions[0].source_candidate_id == proposal.proposal_id
    assert report.decisions[0].outcome == "rejected"


def test_fork_refuses_unchanged_pins_and_over_budget() -> None:
    with pytest.raises(CounterfactualError) as unchanged:
        # Exercise the pure validation path without a database dependency.
        from caliber.replay.runner import _fork_pins

        _fork_pins({"model_routes": ["fake/model"]}, {"model_routes": ["fake/model"]})
    assert unchanged.value.code == FORK_PINS

    with pytest.raises(CounterfactualError) as over:
        from caliber.replay.runner import _fork_budget

        _fork_budget({"max_cases": 257, "max_adapter_calls": 1})
    assert over.value.code == FORK_BUDGET


def test_live_adapter_requires_explicit_authorization() -> None:
    class LiveAdapter:
        name = "live"
        version = "v1"
        requires_live_authorization = True

        def evaluate(self, **_: Any) -> dict[str, Any]:
            return _case(1)

    from caliber.replay.runner import _adapter_info

    with pytest.raises(CounterfactualError) as refused:
        _adapter_info(LiveAdapter(), authorize_live_evaluation=False)
    assert refused.value.code == FORK_AUTHORIZATION


@pytest.mark.parametrize(
    "value",
    [
        {"max_cases": 0, "max_adapter_calls": 1},
        {"max_cases": 1, "max_adapter_calls": 257},
        {"max_cases": True, "max_adapter_calls": 1},
        {"max_cases": 1},
        1,
    ],
)
def test_budget_validation_is_bounded(value: object) -> None:
    with pytest.raises(CounterfactualError) as refused:
        _fork_budget(value)  # type: ignore[arg-type]
    assert refused.value.code == FORK_BUDGET


def test_budget_accepts_dataclass_and_mapping() -> None:
    assert _fork_budget(None) == CounterfactualBudget()
    assert _fork_budget(CounterfactualBudget(2, 3)).as_json() == {
        "max_cases": 2,
        "max_adapter_calls": 3,
    }
    assert _fork_budget({"max_cases": 2, "max_adapter_calls": 3}).max_cases == 2


@pytest.mark.parametrize("value", [None, {}, {"": "x"}, {"model_routes": {1, 2}}])
def test_pin_validation_refuses_malformed_values(value: object) -> None:
    with pytest.raises(CounterfactualError) as refused:
        _fork_pins({"model_routes": ["fake/model"]}, value)  # type: ignore[arg-type]
    assert refused.value.code == FORK_PINS


def test_pin_validation_refuses_nonfinite_result_and_accepts_change() -> None:
    with pytest.raises(CounterfactualError) as nonfinite:
        _fork_pins({"model_routes": ["fake/model"]}, {"model_routes": [float("nan")]})
    assert nonfinite.value.code == FORK_PINS
    merged, changed = _fork_pins({"model_routes": ["fake/model"]}, {"model_routes": ["fake/other"]})
    assert merged["model_routes"] == ["fake/other"]
    assert changed == {"model_routes": ["fake/other"]}


def test_adapter_identity_and_result_validation_fail_closed() -> None:
    class BadIdentity:
        name = ""
        version = "v1"

        def evaluate(self, **_: Any) -> dict[str, Any]:
            return _case(1)

    class NoCallable:
        name = "no-call"
        version = "v1"

    with pytest.raises(CounterfactualError) as identity:
        _adapter_info(BadIdentity(), authorize_live_evaluation=True)
    assert identity.value.code == FORK_ADAPTER
    with pytest.raises(CounterfactualError) as callable_error:
        _adapter_info(NoCallable(), authorize_live_evaluation=True)
    assert callable_error.value.code == FORK_ADAPTER
    with pytest.raises(CounterfactualError) as unauthorized:
        _adapter_info(lambda **_: _case(1), authorize_live_evaluation=False)
    assert unauthorized.value.code == FORK_AUTHORIZATION
    evaluate, name, version, live = _adapter_info(
        lambda **_: _case(1), authorize_live_evaluation=True
    )
    assert callable(evaluate) and name == "function" and version == "v1" and live


@pytest.mark.parametrize(
    "adapter,code",
    [
        (lambda **_: (_ for _ in ()).throw(RuntimeError("boom")), FORK_ADAPTER),
        (lambda **_: ["not-a-case"], FORK_ADAPTER),
        (lambda **_: {"case_id": "other", "document_requests": []}, FORK_ADAPTER),
        (lambda **_: {"case_id": CASE_ID, "document_requests": ["bad"]}, FORK_ADAPTER),
    ],
)
def test_candidate_adapter_failures_are_explicit(adapter: Any, code: str) -> None:
    with pytest.raises(CounterfactualError) as refused:
        _candidate_case(
            adapter,
            case_id=CASE_ID,
            source_case=_case(1),
            pins={"model_routes": ["fake/model"]},
        )
    assert refused.value.code == code


def test_candidate_adapter_preserves_explicit_refusal() -> None:
    def refuse(**_: Any) -> dict[str, Any]:
        raise CounterfactualError(FORK_ADAPTER, "script refused")

    with pytest.raises(CounterfactualError) as refused:
        _candidate_case(
            refuse,
            case_id=CASE_ID,
            source_case=_case(1),
            pins={"model_routes": ["fake/model"]},
        )
    assert refused.value.code == FORK_ADAPTER


def test_fork_manifest_rejects_invalid_pin_shape() -> None:
    loaded = SimpleNamespace(record=_manifest(RUN_ID).manifest_bytes)
    loaded.record = {
        "schema_version": "v1",
        "manifest_id": "mf-source",
        "workflow_run_id": RUN_ID,
        "tenant_id": "local",
        "region": "local",
        "project_id": PROJECT,
        "pins": {
            "workflow_version": "version-1",
            "bundle_digest": "bundle-1",
            "prompt_versions": [],
            "tool_versions": [],
            "skill_versions": [],
            "knowledge_base_versions": [],
            "model_routes": ["fake/model"],
            "policy_version": "policy-1",
        },
        "subject": {"user_id": PRINCIPAL, "auth_method": "test"},
        "workload": {
            "workload_id": "workload-1",
            "name": "fork-test",
            "workflow_version_digest": "bundle-1",
        },
        "purpose": "workflow",
        "created_at": "2026-11-03T00:00:00Z",
    }
    with pytest.raises(CounterfactualError) as refused:
        _fork_manifest(
            loaded,
            workflow_run_id="WR-fork",
            principal="operator_a",
            pins={"unsupported": True},
            moment=datetime(2026, 11, 3, tzinfo=timezone.utc),
        )
    assert refused.value.code == FORK_PINS


def test_pin_validation_checks_source_json_too() -> None:
    with pytest.raises(CounterfactualError) as refused:
        _fork_pins({"model_routes": {1, 2}}, {"policy_version": "policy-2"})
    assert refused.value.code == FORK_PINS


def _minimal_fork(
    monkeypatch: pytest.MonkeyPatch,
    *,
    proposal_rows: list[tuple[str, str]],
    originals: dict[str, Any],
    case_records: dict[str, dict[str, Any]],
    proposal_cases: dict[str, str],
) -> tuple[Any, Mock]:
    run = CaliberWorkflowRun(
        workflow_run_id=RUN_ID,
        workflow_id="workflow-fork",
        project_id=PROJECT,
        workflow_version_id="version-1",
        manifest_legacy=False,
    )
    loaded = SimpleNamespace(
        pins={"model_routes": ["fake/model"]},
        record={},
    )
    monkeypatch.setattr(runner, "_manifest", lambda *_args: loaded)
    monkeypatch.setattr(runner, "_verified_rows", lambda *_args, **_kwargs: [object()])
    monkeypatch.setattr(
        runner,
        "_read_rows",
        lambda *_args, **_kwargs: (case_records, set()),
    )
    monkeypatch.setattr(runner, "_original_decisions", lambda *_args: originals)
    monkeypatch.setattr(
        runner,
        "observations_for_proposal",
        lambda _session, proposal_id, *, case_record=None: {
            "proposal": {"case_id": proposal_cases[proposal_id]},
            "receipt": {"downstream_request_id": "REQ-1"},
            "case_record": case_record,
        },
    )
    session = Mock()
    session.execute.return_value.all.return_value = proposal_rows
    return run, session


def test_fork_source_refusals_cover_effect_identity_and_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(CounterfactualError) as no_effect:
        run, session = _minimal_fork(
            monkeypatch,
            proposal_rows=[],
            originals={},
            case_records={CASE_ID: _case(1)},
            proposal_cases={},
        )
        fork_counterfactual(
            session,
            Mock(),
            run=run,
            project_id=PROJECT,
            principal=PRINCIPAL,
            changed_pins={"model_routes": ["fake/other"]},
        )
    assert no_effect.value.code == FORK_SOURCE

    with pytest.raises(CounterfactualError) as mismatch:
        run, session = _minimal_fork(
            monkeypatch,
            proposal_rows=[("prop-1", "document-request")],
            originals={},
            case_records={CASE_ID: _case(1)},
            proposal_cases={"prop-1": CASE_ID},
        )
        fork_counterfactual(
            session,
            Mock(),
            run=run,
            project_id=PROJECT,
            principal=PRINCIPAL,
            changed_pins={"model_routes": ["fake/other"]},
        )
    assert mismatch.value.code == FORK_SOURCE

    with pytest.raises(CounterfactualError) as too_many_cases:
        rows = [("prop-1", "document-request"), ("prop-2", "document-request")]
        originals = {key: SimpleNamespace(criteria_version="harborlight-v1") for key, _ in rows}
        run, session = _minimal_fork(
            monkeypatch,
            proposal_rows=rows,
            originals=originals,
            case_records={
                "case-1": {"case_id": "case-1", "document_requests": []},
                "case-2": {"case_id": "case-2", "document_requests": []},
            },
            proposal_cases={"prop-1": "case-1", "prop-2": "case-2"},
        )
        fork_counterfactual(
            session,
            Mock(),
            run=run,
            project_id=PROJECT,
            principal=PRINCIPAL,
            changed_pins={"model_routes": ["fake/other"]},
            budget={"max_cases": 1, "max_adapter_calls": 2},
        )
    assert too_many_cases.value.code == FORK_BUDGET


def test_fork_source_refuses_unsupported_criteria_and_missing_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [("prop-1", "other")]
    run, session = _minimal_fork(
        monkeypatch,
        proposal_rows=rows,
        originals={"prop-1": SimpleNamespace(criteria_version="harborlight-v1")},
        case_records={CASE_ID: _case(1)},
        proposal_cases={"prop-1": CASE_ID},
    )
    monkeypatch.setattr(runner, "CRITERIA_BY_CONNECTOR", {"document-request": object()})
    with pytest.raises(CounterfactualError) as unsupported:
        fork_counterfactual(
            session,
            Mock(),
            run=run,
            project_id=PROJECT,
            principal=PRINCIPAL,
            changed_pins={"model_routes": ["fake/other"]},
        )
    assert unsupported.value.code == FORK_SOURCE

    rows = [("prop-1", "document-request")]
    run, session = _minimal_fork(
        monkeypatch,
        proposal_rows=rows,
        originals={"prop-1": SimpleNamespace(criteria_version="wrong")},
        case_records={CASE_ID: _case(1)},
        proposal_cases={"prop-1": CASE_ID},
    )
    monkeypatch.setattr(runner, "CRITERIA_BY_CONNECTOR", CRITERIA_BY_CONNECTOR)
    with pytest.raises(CounterfactualError) as wrong_version:
        fork_counterfactual(
            session,
            Mock(),
            run=run,
            project_id=PROJECT,
            principal=PRINCIPAL,
            changed_pins={"model_routes": ["fake/other"]},
        )
    assert wrong_version.value.code == FORK_SOURCE

    run, session = _minimal_fork(
        monkeypatch,
        proposal_rows=rows,
        originals={"prop-1": SimpleNamespace(criteria_version="harborlight-v1")},
        case_records={},
        proposal_cases={"prop-1": "missing"},
    )
    with pytest.raises(CounterfactualError) as missing:
        fork_counterfactual(
            session,
            Mock(),
            run=run,
            project_id=PROJECT,
            principal=PRINCIPAL,
            changed_pins={"model_routes": ["fake/other"]},
        )
    assert missing.value.code == FORK_SOURCE


def test_fork_budget_and_clock_refusals(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [("prop-1", "document-request"), ("prop-2", "document-request")]
    originals = {key: SimpleNamespace(criteria_version="harborlight-v1") for key, _ in rows}
    run, session = _minimal_fork(
        monkeypatch,
        proposal_rows=rows,
        originals=originals,
        case_records={
            "case-1": {"case_id": "case-1", "document_requests": []},
            "case-2": {"case_id": "case-2", "document_requests": []},
        },
        proposal_cases={"prop-1": "case-1", "prop-2": "case-2"},
    )
    with pytest.raises(CounterfactualError) as calls:
        fork_counterfactual(
            session,
            Mock(),
            run=run,
            project_id=PROJECT,
            principal=PRINCIPAL,
            changed_pins={"model_routes": ["fake/other"]},
            budget={"max_cases": 2, "max_adapter_calls": 1},
        )
    assert calls.value.code == FORK_BUDGET

    rows = [("prop-1", "document-request")]
    run, session = _minimal_fork(
        monkeypatch,
        proposal_rows=rows,
        originals={"prop-1": SimpleNamespace(criteria_version="harborlight-v1")},
        case_records={CASE_ID: _case(1)},
        proposal_cases={"prop-1": CASE_ID},
    )
    with pytest.raises(CounterfactualError) as bad_clock:
        fork_counterfactual(
            session,
            Mock(),
            run=run,
            project_id=PROJECT,
            principal=PRINCIPAL,
            changed_pins={"model_routes": ["fake/other"]},
            now=datetime(2026, 11, 3),
        )
    assert bad_clock.value.code == FORK_SOURCE


@pytest.mark.parametrize(
    "run,project_id,principal,source_principal,code",
    [
        (object(), PROJECT, PRINCIPAL, None, FORK_SOURCE),
        (
            CaliberWorkflowRun(workflow_run_id=RUN_ID, workflow_id="wf", project_id=PROJECT),
            "other",
            PRINCIPAL,
            None,
            FORK_SOURCE,
        ),
        (
            CaliberWorkflowRun(workflow_run_id=RUN_ID, workflow_id="wf", project_id=PROJECT),
            PROJECT,
            " ",
            None,
            FORK_AUTHORIZATION,
        ),
        (
            CaliberWorkflowRun(workflow_run_id=RUN_ID, workflow_id="wf", project_id=PROJECT),
            PROJECT,
            PRINCIPAL,
            " ",
            FORK_SOURCE,
        ),
    ],
)
def test_fork_scope_and_authority_refusals(
    run: object,
    project_id: str,
    principal: str,
    source_principal: str | None,
    code: str,
) -> None:
    with pytest.raises(CounterfactualError) as refused:
        fork_counterfactual(
            Mock(),
            Mock(),
            run=run,  # type: ignore[arg-type]
            project_id=project_id,
            principal=principal,
            source_principal=source_principal,
            changed_pins={"model_routes": ["fake/other"]},
        )
    assert refused.value.code == code
