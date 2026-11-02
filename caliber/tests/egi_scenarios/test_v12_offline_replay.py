# ruff: noqa: F811 - the world fixture is imported from the governed-worker scenario
"""B09.4 / #805: bounded historical replay is a read-only offline operation."""

from __future__ import annotations

from typing import Any, cast

import pytest
from sqlalchemy import func, select

from caliber.acceptance import completion as completion_module
from caliber.db.models import (
    CaliberAcceptanceDecision,
    CaliberStepObservation,
    CaliberWorkflowRun,
)
from caliber.replay.observations import record_observation
from caliber.replay.runner import (
    OBSERVATION_LIMIT,
    OBSERVATION_MALFORMED,
    ReplayError,
    _validate_ids,
)
from tests.egi_scenarios.conftest import EDITOR_A, PROJECT_A, Downstream, ReferenceApp
from tests.egi_scenarios.test_b02_admission import _call
from tests.egi_scenarios.test_b05_governed_worker_wiring import CASE, world  # noqa: F401
from tests.egi_scenarios.test_v11_false_success import Reading, drive


def _backend(app: ReferenceApp) -> Any:
    return app.client.app.state.working_dir_service.backend  # type: ignore[attr-defined]


def _record_case(
    app: ReferenceApp,
    run_id: str,
    case: object,
    *,
    principal: str = EDITOR_A,
    occurrence_id: str = "case-read:0",
) -> str:
    with app.session_factory() as session:
        record = record_observation(
            session,
            _backend(app),
            workflow_run_id=run_id,
            project_id=PROJECT_A,
            principal=principal,
            node_id="case-reader",
            occurrence_id=occurrence_id,
            attempt=1,
            observation_kind="read",
            request={"method": "GET", "path": f"/cases/{CASE}"},
            content=case,
            provider="harborlight-fixture",
            provider_version="v1",
        )
        session.commit()
        return record.observation_id


def _run_summary(app: ReferenceApp, run_id: str) -> tuple[dict[str, Any] | None, int, int]:
    with app.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        observations = session.scalar(select(func.count()).select_from(CaliberStepObservation))
        decisions = session.scalar(select(func.count()).select_from(CaliberAcceptanceDecision))
        return run.summary, int(observations or 0), int(decisions or 0)


def test_replay_makes_no_live_calls(
    world: tuple[ReferenceApp, Downstream], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid report reads the pinned evidence and never re-enters live completion or dispatch."""

    app, downstream = world
    run_id = drive(app, downstream, monkeypatch, Reading())
    case = downstream.client.get(f"/cases/{CASE}").json()
    observation_id = _record_case(app, run_id, case)
    before = _run_summary(app, run_id)
    mutations = downstream.mutations()

    def no_live_case_read(*_: Any, **__: Any) -> Any:
        raise AssertionError("historical replay called the live downstream reader")

    monkeypatch.setattr(completion_module, "read_case", no_live_case_read)
    response = _call(app, "POST", EDITOR_A, PROJECT_A, f"/workflow-runs/{run_id}/replay", {})

    assert response.status_code == 200, response.text
    report = response.json()["data"]
    assert report["schema_version"] == "v1"
    assert report["observation_ids"] == [observation_id]
    assert report["observation_count"] == 1
    assert report["criteria_version"] == "harborlight-v1"
    assert report["matches_original"] is True
    assert report["decisions"][0]["semantic_match"] is True
    assert report["decisions"][0]["observation_match"] is True
    assert downstream.mutations() == mutations
    assert _run_summary(app, run_id) == before


def test_missing_observations_refuse_without_live_fallback(
    world: tuple[ReferenceApp, Downstream], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, downstream = world
    run_id = drive(app, downstream, monkeypatch, Reading())

    response = _call(app, "POST", EDITOR_A, PROJECT_A, f"/workflow-runs/{run_id}/replay", {})

    assert response.status_code == 409
    assert response.json()["detail"].startswith("observations_missing:")
    assert downstream.mutations() == 1


def test_tampered_observation_refuses_without_fallback(
    world: tuple[ReferenceApp, Downstream], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, downstream = world
    run_id = drive(app, downstream, monkeypatch, Reading())
    observation_id = _record_case(app, run_id, downstream.client.get(f"/cases/{CASE}").json())

    with app.session_factory() as session:
        row = session.scalar(
            select(CaliberStepObservation).where(
                CaliberStepObservation.observation_id == observation_id
            )
        )
        assert row is not None
        content_ref = row.content_ref
    _backend(app).write_bytes(content_ref, b"tampered", overwrite=True)

    response = _call(app, "POST", EDITOR_A, PROJECT_A, f"/workflow-runs/{run_id}/replay", {})

    assert response.status_code == 409
    assert response.json()["detail"].startswith("observation_integrity:")
    assert downstream.mutations() == 1


def test_foreign_observation_is_refused_before_storage_access(
    world: tuple[ReferenceApp, Downstream], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, downstream = world
    run_id = drive(app, downstream, monkeypatch, Reading())
    observation_id = _record_case(
        app,
        run_id,
        downstream.client.get(f"/cases/{CASE}").json(),
        principal="editor_b",
    )
    backend = _backend(app)
    original_read = backend.read_bytes
    reads: list[str] = []

    def count_read(ref: str) -> bytes:
        reads.append(ref)
        return cast(bytes, original_read(ref))

    monkeypatch.setattr(backend, "read_bytes", count_read)
    response = _call(
        app,
        "POST",
        EDITOR_A,
        PROJECT_A,
        f"/workflow-runs/{run_id}/replay",
        {"observation_ids": [observation_id]},
    )

    assert response.status_code == 409
    assert response.json()["detail"].startswith("observation_scope:")
    assert reads == []
    assert downstream.mutations() == 1


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b'{"case_id":', "observation_malformed"),
        (["not", "a", "case"], "observation_unsupported"),
    ],
)
def test_malformed_or_unsupported_observation_refuses_without_fallback(
    content: object,
    code: str,
    world: tuple[ReferenceApp, Downstream],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, downstream = world
    run_id = drive(app, downstream, monkeypatch, Reading())
    _record_case(app, run_id, content)

    response = _call(app, "POST", EDITOR_A, PROJECT_A, f"/workflow-runs/{run_id}/replay", {})

    assert response.status_code == 409
    assert response.json()["detail"].startswith(f"{code}:")
    assert downstream.mutations() == 1


def test_observation_id_contract_is_bounded_and_fail_closed() -> None:
    with pytest.raises(ReplayError) as malformed:
        _validate_ids(["OBS-00000000", ["not-a-string"]])  # type: ignore[list-item]
    assert malformed.value.code == OBSERVATION_MALFORMED
    with pytest.raises(ReplayError) as over_bound:
        _validate_ids([f"OBS-{index:08x}" for index in range(257)])
    assert over_bound.value.code == OBSERVATION_LIMIT
