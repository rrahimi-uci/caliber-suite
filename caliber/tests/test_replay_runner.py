"""Unit coverage for the bounded, fail-closed historical replay runner."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from caliber.acceptance.completion import CRITERIA_BY_CONNECTOR
from caliber.acceptance.decision import AcceptanceDecision, judge
from caliber.db.models import CaliberWorkflowRun
from caliber.replay import runner
from caliber.replay.observations import (
    ObservationError,
    ObservationIntegrityError,
    ObservationRecord,
    ObservationScopeError,
)
from caliber.run_manifest import ManifestError
from caliber.workflows.run_pins import PinsError


def _record(
    *,
    observation_id: str = "OBS-00000001",
    kind: str = "read",
    content: bytes = b'{"case_id":"MORT-1","document_requests":[]}',
    content_size: int | None = None,
) -> ObservationRecord:
    return ObservationRecord(
        observation_id=observation_id,
        workflow_run_id="WR-1",
        project_id="project-a",
        principal="editor_a",
        node_id="reader",
        occurrence_id=observation_id,
        attempt=1,
        observation_kind=kind,  # type: ignore[arg-type]
        request_digest="a" * 64,
        content_hash="b" * 64,
        content_size=len(content) if content_size is None else content_size,
        storage_backend="local",
        content_ref=f"replay/{observation_id}",
        object_version_id=None,
        provider="fixture",
        provider_version="v1",
        created_at=datetime(2026, 1, 1),
        content=content,
    )


def _run(*, legacy: bool = False) -> CaliberWorkflowRun:
    return CaliberWorkflowRun(
        workflow_run_id="WR-1",
        workflow_id="workflow-1",
        project_id="project-a",
        workflow_version_id="version-1",
        manifest_legacy=legacy,
        summary={
            "acceptance": {
                "decisions": [
                    {
                        "decision_id": "ACC-1",
                        "candidate_id": "prop-1",
                        "outcome": "accepted",
                        "observation_digest": "c" * 64,
                        "failed_criteria": [],
                    }
                ]
            }
        },
    )


def test_id_and_json_validation_is_strict() -> None:
    assert runner._validate_ids(None) is None
    assert runner._validate_ids(["OBS-00000001"]) == ("OBS-00000001",)
    with pytest.raises(runner.ReplayError) as empty:
        runner._validate_ids([])
    assert empty.value.code == runner.OBSERVATION_MALFORMED
    with pytest.raises(runner.ReplayError) as duplicate:
        runner._validate_ids(["OBS-00000001", "OBS-00000001"])
    assert duplicate.value.status_code == 400
    with pytest.raises(runner.ReplayError) as bad_type:
        runner._validate_ids([{"id": "OBS-00000001"}])  # type: ignore[list-item]
    assert bad_type.value.code == runner.OBSERVATION_MALFORMED
    with pytest.raises(runner.ReplayError) as bad_id:
        runner._validate_ids(["obs-00000001"])
    assert bad_id.value.code == runner.OBSERVATION_MALFORMED
    with pytest.raises(runner.ReplayError) as over:
        runner._validate_ids([f"OBS-{index:08x}" for index in range(257)])
    assert over.value.code == runner.OBSERVATION_LIMIT
    with pytest.raises(runner.ReplayError):
        runner._json_payload(_record(content=b'{"same":1,"same":2}'))
    with pytest.raises(runner.ReplayError):
        runner._json_payload(_record(content=b"NaN"))
    with pytest.raises(runner.ReplayError):
        runner._json_payload(_record(content=b"\xff"))
    with pytest.raises(runner.ReplayError):
        runner._json_payload(replace(_record(), content=None))
    with pytest.raises(ValueError):
        runner.ReplayError("unknown", "not a public code")
    with pytest.raises(runner.ReplayError):
        runner._validate_ids("OBS-00000001")  # type: ignore[arg-type]


def test_read_rows_accepts_supported_model_and_case_read(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _record(
        observation_id="OBS-00000001",
        kind="model",
        content=b'{"final_output":"done","structured_output":null,"tool_calls":[],"model":"fake","prompt_version":"v1"}',
    )
    case = _record(observation_id="OBS-00000002")
    monkeypatch.setattr(
        runner,
        "read_observation",
        lambda *_args, observation_id, **_kwargs: (
            model if observation_id == model.observation_id else case
        ),
    )

    records, models = runner._read_rows(
        Mock(), Mock(), [model, case], project_id="project-a", principal="editor_a"
    )

    assert models == {model.observation_id}
    assert records == {"MORT-1": {"case_id": "MORT-1", "document_requests": []}}

    duplicate = _record(observation_id="OBS-00000003")
    monkeypatch.setattr(runner, "read_observation", lambda *_args, **_kwargs: duplicate)
    repeated, _ = runner._read_rows(
        Mock(), Mock(), [duplicate, duplicate], project_id="project-a", principal="editor_a"
    )
    assert repeated == records


@pytest.mark.parametrize(
    ("record", "error"),
    [
        (_record(content_size=0), runner.OBSERVATION_LIMIT),
        (_record(content_size=runner.MAX_OBSERVATION_BYTES + 1), runner.OBSERVATION_LIMIT),
        (_record(kind="other"), runner.OBSERVATION_UNSUPPORTED),
        (_record(content=b"not-json"), runner.OBSERVATION_MALFORMED),
        (_record(content=b"[1,2,3]"), runner.OBSERVATION_UNSUPPORTED),
        (_record(content=b'{"case_id":"MORT-1"}'), runner.OBSERVATION_MALFORMED),
    ],
)
def test_read_rows_refuses_bad_bounds_and_shapes(
    record: ObservationRecord, error: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "read_observation", lambda *_args, **_kwargs: record)
    with pytest.raises(runner.ReplayError) as raised:
        runner._read_rows(Mock(), Mock(), [record], project_id="project-a", principal="editor_a")
    assert raised.value.code == error


def test_read_rows_refuses_integrity_scope_disagreement_and_duplicate_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _record()
    monkeypatch.setattr(
        runner,
        "read_observation",
        Mock(side_effect=ObservationScopeError("foreign")),
    )
    with pytest.raises(runner.ReplayError) as scope:
        runner._read_rows(Mock(), Mock(), [record], project_id="project-a", principal="editor_a")
    assert scope.value.code == runner.OBSERVATION_SCOPE

    monkeypatch.setattr(
        runner,
        "read_observation",
        Mock(side_effect=ObservationIntegrityError("tampered")),
    )
    with pytest.raises(runner.ReplayError) as integrity:
        runner._read_rows(Mock(), Mock(), [record], project_id="project-a", principal="editor_a")
    assert integrity.value.code == runner.OBSERVATION_INTEGRITY

    first = _record(observation_id="OBS-00000001")
    second = _record(
        observation_id="OBS-00000002",
        content=b'{"case_id":"MORT-1","document_requests":[{"request_id":"REQ-1"}]}',
    )
    monkeypatch.setattr(
        runner,
        "read_observation",
        lambda *_args, observation_id, **_kwargs: (
            first if observation_id == first.observation_id else second
        ),
    )
    with pytest.raises(runner.ReplayError) as disagreement:
        runner._read_rows(
            Mock(), Mock(), [first, second], project_id="project-a", principal="editor_a"
        )
    assert disagreement.value.code == runner.OBSERVATION_UNSUPPORTED

    model = _record(
        observation_id="OBS-00000003",
        kind="model",
        content=b'{"final_output":"done","structured_output":null,"tool_calls":[],"model":"fake","prompt_version":"v1"}',
    )
    monkeypatch.setattr(runner, "read_observation", lambda *_args, **_kwargs: model)
    with pytest.raises(runner.ReplayError) as missing:
        runner._read_rows(Mock(), Mock(), [model], project_id="project-a", principal="editor_a")
    assert missing.value.code == runner.OBSERVATIONS_MISSING


def test_read_rows_refuses_generic_storage_and_unsupported_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _record()
    monkeypatch.setattr(runner, "read_observation", Mock(side_effect=ObservationError("down")))
    with pytest.raises(runner.ReplayError) as storage:
        runner._read_rows(Mock(), Mock(), [record], project_id="project-a", principal="editor_a")
    assert storage.value.code == runner.OBSERVATION_INTEGRITY

    model = _record(observation_id="OBS-00000003", kind="model", content=b"{}")
    monkeypatch.setattr(runner, "read_observation", lambda *_args, **_kwargs: model)
    with pytest.raises(runner.ReplayError) as unsupported:
        runner._read_rows(Mock(), Mock(), [model], project_id="project-a", principal="editor_a")
    assert unsupported.value.code == runner.OBSERVATION_UNSUPPORTED

    monkeypatch.setattr(runner, "case_record_from_observation", lambda _value: None)
    with pytest.raises(runner.ReplayError) as no_projection:
        runner._read_rows(Mock(), Mock(), [record], project_id="project-a", principal="editor_a")
    assert no_projection.value.code == runner.OBSERVATION_UNSUPPORTED

    bad_items = _record(content=b'{"case_id":"MORT-1","document_requests":[1]}')
    monkeypatch.setattr(runner, "read_observation", lambda *_args, **_kwargs: bad_items)
    monkeypatch.setattr(
        runner,
        "case_record_from_observation",
        lambda value: {
            "case_id": value["case_id"],
            "document_requests": value["document_requests"],
        },
    )
    with pytest.raises(runner.ReplayError) as bad_list:
        runner._read_rows(Mock(), Mock(), [bad_items], project_id="project-a", principal="editor_a")
    assert bad_list.value.code == runner.OBSERVATION_MALFORMED

    monkeypatch.setattr(
        runner,
        "case_record_from_observation",
        lambda _value: {"case_id": "MORT-1", "document_requests": [{"request_id": object()}]},
    )
    with pytest.raises(runner.ReplayError) as non_json:
        runner._read_rows(Mock(), Mock(), [record], project_id="project-a", principal="editor_a")
    assert non_json.value.code == runner.OBSERVATION_MALFORMED


def test_verified_rows_apply_scope_before_bytes_and_bound_default_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run()
    record = _record()
    listed = Mock(return_value=[record])
    monkeypatch.setattr(runner, "list_observation_rows", listed)
    assert runner._verified_rows(
        Mock(), run=run, project_id="project-a", principal="editor_a", observation_ids=None
    ) == [record]
    assert listed.call_args.kwargs["limit"] == runner.MAX_OBSERVATIONS + 1

    listed.return_value = []
    with pytest.raises(runner.ReplayError) as missing:
        runner._verified_rows(
            Mock(), run=run, project_id="project-a", principal="editor_a", observation_ids=None
        )
    assert missing.value.code == runner.OBSERVATIONS_MISSING

    listed.return_value = [_record(observation_id=f"OBS-{index:08x}") for index in range(257)]
    with pytest.raises(runner.ReplayError) as over:
        runner._verified_rows(
            Mock(), run=run, project_id="project-a", principal="editor_a", observation_ids=None
        )
    assert over.value.code == runner.OBSERVATION_LIMIT

    listed.return_value = []
    with pytest.raises(runner.ReplayError) as foreign:
        runner._verified_rows(
            Mock(),
            run=run,
            project_id="project-a",
            principal="editor_a",
            observation_ids=("OBS-00000001",),
        )
    assert foreign.value.code == runner.OBSERVATION_SCOPE

    listed.return_value = [_record()]
    wrong_scope = replace(record, principal="editor_b")
    listed.return_value = [wrong_scope]
    with pytest.raises(runner.ReplayError) as wrong:
        runner._verified_rows(
            Mock(), run=run, project_id="project-a", principal="editor_a", observation_ids=None
        )
    assert wrong.value.code == runner.OBSERVATION_SCOPE

    listed.return_value = [replace(record, observation_id="not-an-observation-id")]
    with pytest.raises(runner.ReplayError) as malformed:
        runner._verified_rows(
            Mock(), run=run, project_id="project-a", principal="editor_a", observation_ids=None
        )
    assert malformed.value.code == runner.OBSERVATION_MALFORMED


def test_original_decisions_are_named_and_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run()
    decision = SimpleNamespace(
        workflow_run_id="WR-1",
        candidate_id="prop-1",
        decision_id="ACC-1",
        outcome="accepted",
        observation_digest="c" * 64,
        failed_criteria=(),
    )
    monkeypatch.setattr(runner, "read_decision", lambda *_args: decision)
    assert runner._original_decisions(Mock(), run) == {"prop-1": decision}

    run.summary = {
        "acceptance": {
            "decisions": [
                {
                    "decision_id": "ACC-1",
                    "candidate_id": "prop-1",
                    "outcome": "rejected",
                    "observation_digest": "c" * 64,
                    "failed_criteria": [],
                }
            ]
        }
    }
    with pytest.raises(runner.ReplayError) as disagreement:
        runner._original_decisions(Mock(), run)
    assert disagreement.value.code == runner.DECISIONS_CORRUPT

    for summary in (None, {}, {"acceptance": {"decisions": []}}):
        run.summary = summary
        with pytest.raises(runner.ReplayError) as missing:
            runner._original_decisions(Mock(), run)
        assert missing.value.code == runner.DECISIONS_MISSING
    run.summary = {"acceptance": {"decisions": [{"candidate_id": "prop-1"}]}}
    with pytest.raises(runner.ReplayError) as malformed:
        runner._original_decisions(Mock(), run)
    assert malformed.value.code == runner.DECISIONS_CORRUPT
    run.summary = {"acceptance": {"decisions": [dict.fromkeys(runner._SUMMARY_DECISION_KEYS, 1)]}}
    with pytest.raises(runner.ReplayError) as bad_types:
        runner._original_decisions(Mock(), run)
    assert bad_types.value.code == runner.DECISIONS_CORRUPT
    monkeypatch.setattr(runner, "read_decision", Mock(side_effect=KeyError("missing")))
    run.summary = _run().summary
    with pytest.raises(runner.ReplayError) as unreadable:
        runner._original_decisions(Mock(), run)
    assert unreadable.value.code == runner.DECISIONS_CORRUPT
    monkeypatch.setattr(
        runner,
        "read_decision",
        lambda *_args: SimpleNamespace(
            workflow_run_id="WR-1", candidate_id="other", decision_id="ACC-1"
        ),
    )
    with pytest.raises(runner.ReplayError) as foreign:
        runner._original_decisions(Mock(), run)
    assert foreign.value.code == runner.DECISIONS_CORRUPT
    monkeypatch.setattr(runner, "read_decision", lambda *_args: decision)
    run.summary = {
        "acceptance": {
            "decisions": [
                {
                    "decision_id": "ACC-1",
                    "candidate_id": "prop-1",
                    "outcome": "accepted",
                    "observation_digest": "c" * 64,
                    "failed_criteria": [],
                },
                {
                    "decision_id": "ACC-1",
                    "candidate_id": "prop-1",
                    "outcome": "accepted",
                    "observation_digest": "c" * 64,
                    "failed_criteria": [],
                },
            ]
        }
    }
    with pytest.raises(runner.ReplayError) as duplicate:
        runner._original_decisions(Mock(), run)
    assert duplicate.value.code == runner.DECISIONS_CORRUPT


def test_manifest_and_original_decision_contracts_refuse_corruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run()
    loaded = SimpleNamespace(
        record={
            "legacy": False,
            "project_id": "project-a",
            "pins": {"workflow_version": "version-1"},
        },
        manifest_id="MAN-1",
        digest="d" * 64,
    )
    monkeypatch.setattr(runner, "load_run_manifest", lambda *_args: loaded)
    assert runner._manifest(Mock(), run, "project-a") is loaded

    legacy = _run(legacy=True)
    with pytest.raises(runner.ReplayError) as legacy_error:
        runner._manifest(Mock(), legacy, "project-a")
    assert legacy_error.value.code == runner.MANIFEST_UNSUPPORTED

    monkeypatch.setattr(runner, "load_run_manifest", lambda *_args: None)
    with pytest.raises(runner.ReplayError) as missing:
        runner._manifest(Mock(), run, "project-a")
    assert missing.value.code == runner.MANIFEST_MISSING

    monkeypatch.setattr(
        runner,
        "load_run_manifest",
        Mock(side_effect=PinsError("manifest_corrupt", "bad")),
    )
    with pytest.raises(runner.ReplayError) as corrupt:
        runner._manifest(Mock(), run, "project-a")
    assert corrupt.value.code == runner.MANIFEST_CORRUPT

    monkeypatch.setattr(
        runner,
        "load_run_manifest",
        Mock(side_effect=ManifestError("bad_pin", "bad")),
    )
    with pytest.raises(runner.ReplayError) as malformed:
        runner._manifest(Mock(), run, "project-a")
    assert malformed.value.code == runner.MANIFEST_CORRUPT

    monkeypatch.setattr(runner, "load_run_manifest", lambda *_args: loaded)
    with pytest.raises(runner.ReplayError) as foreign:
        runner._manifest(Mock(), run, "project-b")
    assert foreign.value.code == runner.MANIFEST_UNSUPPORTED
    loaded.record["pins"] = {"workflow_version": "version-2"}
    with pytest.raises(runner.ReplayError) as pins:
        runner._manifest(Mock(), run, "project-a")
    assert pins.value.code == runner.PINS_MISMATCH


def test_replay_historical_returns_semantic_report_without_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run()
    criteria = CRITERIA_BY_CONNECTOR["document-request"]
    observations = {
        "proposal": {"case_id": "MORT-1", "document_type": "pay-statement", "expected_version": 1},
        "receipt": {"downstream_request_id": "REQ-1"},
        "case_record": {"case_id": "MORT-1", "document_requests": []},
    }
    verdict = judge(criteria.predicates, observations)
    original = AcceptanceDecision(
        decision_id="ACC-1",
        workflow_run_id="WR-1",
        candidate_id="prop-1",
        criteria_version=verdict.criteria_version,
        observation_digest=verdict.observation_digest,
        outcome=verdict.outcome,
        criteria=verdict.criteria,
        reason_code=verdict.reason_code,
        reason_detail=verdict.reason_detail,
        decided_at=datetime(2026, 1, 1),
    )
    read = _record()
    monkeypatch.setattr(
        runner,
        "_manifest",
        lambda *_args: SimpleNamespace(manifest_id="MAN-1", digest="d" * 64),
    )
    monkeypatch.setattr(runner, "_verified_rows", lambda *_args, **_kwargs: [read])
    monkeypatch.setattr(
        runner,
        "_read_rows",
        lambda *_args, **_kwargs: ({"MORT-1": observations["case_record"]}, set()),
    )
    monkeypatch.setattr(runner, "_original_decisions", lambda *_args: {"prop-1": original})
    monkeypatch.setattr(
        runner,
        "observations_for_proposal",
        lambda _session, _proposal_id, *, case_record=None: {
            **{key: value for key, value in observations.items() if key != "case_record"},
            **({"case_record": case_record} if case_record is not None else {}),
        },
    )
    session = Mock()
    session.execute.return_value.all.return_value = [("prop-1", "document-request")]

    report = runner.replay_historical(
        session,
        Mock(),
        run=run,
        project_id="project-a",
        principal="editor_a",
    )

    assert report.matches_original is True
    assert report.manifest_id == "MAN-1"
    assert report.decisions[0].semantic_match is True
    assert report.decisions[0].observation_match is True


def test_replay_historical_refuses_scope_and_non_run() -> None:
    with pytest.raises(runner.ReplayError) as non_run:
        runner.replay_historical(
            Mock(), Mock(), run=object(), project_id="project-a", principal="editor_a"
        )  # type: ignore[arg-type]
    assert non_run.value.status_code == 400
    run = _run()
    with pytest.raises(runner.ReplayError) as scope:
        runner.replay_historical(
            Mock(), Mock(), run=run, project_id="project-b", principal="editor_a"
        )
    assert scope.value.code == runner.OBSERVATION_SCOPE


def test_replay_historical_refuses_invalid_principal() -> None:
    run = _run()
    with pytest.raises(runner.ReplayError) as invalid:
        runner.replay_historical(Mock(), Mock(), run=run, project_id="project-a", principal=" ")
    assert invalid.value.status_code == 400
