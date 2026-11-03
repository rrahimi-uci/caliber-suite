"""HTTP-level request-contract checks for B09.4 before a run is looked up."""

from __future__ import annotations

from starlette.testclient import TestClient

from tests.workflow_helpers import PREFIX


def test_replay_route_rejects_over_bound_request_before_run_lookup(client: TestClient) -> None:
    response = client.post(
        f"{PREFIX}/workflow-runs/WR-missing/replay",
        json={"observation_ids": [f"OBS-{index:08x}" for index in range(257)]},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid historical replay request"
