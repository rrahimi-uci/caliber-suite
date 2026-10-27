"""``/readiness`` says whether this process runs the background loops (B13.3, #673)."""

from __future__ import annotations

from starlette.testclient import TestClient

from caliber.observability.readiness import ReadinessReport

READINESS = "/ajax-api/2.0/mlflow/caliber/readiness"


def test_a_process_that_runs_the_loops_reports_owner(worker_client: TestClient) -> None:
    body = worker_client.get(READINESS).json()["data"]
    assert body["execution_role"] == "owner"
    assert body["profile"] == "development"


def test_a_process_that_only_serves_requests_reports_secondary(client: TestClient) -> None:
    """Background tasks are off in this fixture: no loop runs, so this process is not the owner."""
    assert client.get(READINESS).json()["data"]["execution_role"] == "secondary"


def test_before_the_lifespan_has_run_no_loop_is_running_either(app_config) -> None:  # type: ignore[no-untyped-def]
    from caliber.server import create_app

    app = create_app(app_config)  # a fresh app, never started: nothing has published a controller
    assert not hasattr(app.state, "execution_role")
    unstarted = TestClient(
        app, headers={"X-CALIBER-User": "@test"}
    )  # no context manager: no lifespan
    assert unstarted.get(READINESS).json()["data"]["execution_role"] == "secondary"


def test_the_report_carries_the_role_and_defaults_to_owner() -> None:
    assert ReadinessReport(checks=[]).to_dict()["execution_role"] == "owner"
    assert (
        ReadinessReport(checks=[], execution_role="secondary").to_dict()["execution_role"]
        == "secondary"
    )


def test_the_lifespan_publishes_the_controller(worker_client: TestClient) -> None:
    controller = worker_client.app.state.execution_role  # type: ignore[attr-defined]
    assert controller.role == "owner" and controller.lease is None, (
        "no lease outside the reference profile"
    )
