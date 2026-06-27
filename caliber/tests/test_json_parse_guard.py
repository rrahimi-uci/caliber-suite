"""Cross-endpoint JSON parse contract.

Deep-review V2 Finding 3: every write endpoint must translate a
malformed JSON body to ``400`` (the JSON envelope) rather than the
plaintext ``500 Internal Server Error`` Starlette emits by default.

The parametrized sweep below hits one representative write endpoint
per route module with a malformed body (and one with a JSON array
instead of an object) and asserts the response is a structured 400.
A future write endpoint that bypasses :func:`parse_json_object` will
trip the wildcard sweep that doesn't pre-seed any state — invalid
JSON should be rejected before any DB lookup runs.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/ajax-api/2.0/mlflow/caliber/agents"),
        ("PATCH", "/ajax-api/2.0/mlflow/caliber/agents/some-agent"),
        ("POST", "/ajax-api/2.0/mlflow/caliber/skills"),
        ("POST", "/ajax-api/2.0/mlflow/caliber/skills/import-package"),
        ("PATCH", "/ajax-api/2.0/mlflow/caliber/skills/SK-ANY"),
        ("POST", "/ajax-api/2.0/mlflow/caliber/eval-datasets"),
        ("PATCH", "/ajax-api/2.0/mlflow/caliber/eval-datasets/ED-ANY"),
        ("POST", "/ajax-api/2.0/mlflow/caliber/eval-datasets/ED-ANY/examples"),
        ("POST", "/ajax-api/2.0/mlflow/caliber/workflow-benchmark-reports"),
        ("PATCH", "/ajax-api/2.0/mlflow/caliber/workflow-benchmark-reports/WFB-ANY"),
        ("POST", "/ajax-api/2.0/mlflow/caliber/agents/some-agent/rollback"),
    ],
)
def test_malformed_json_body_returns_400(client: TestClient, method: str, path: str) -> None:
    """Malformed JSON → structured 400, not plaintext 500."""
    response = client.request(
        method,
        path,
        content=b"not valid json {{",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["status_code"] == 400
    assert "invalid JSON" in body["detail"]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/ajax-api/2.0/mlflow/caliber/agents"),
        ("POST", "/ajax-api/2.0/mlflow/caliber/skills"),
        ("POST", "/ajax-api/2.0/mlflow/caliber/skills/import-package"),
        ("POST", "/ajax-api/2.0/mlflow/caliber/eval-datasets"),
        ("POST", "/ajax-api/2.0/mlflow/caliber/workflow-benchmark-reports"),
    ],
)
def test_non_object_json_body_returns_400(client: TestClient, method: str, path: str) -> None:
    """A JSON array / number / bare null doesn't satisfy the
    ``dict[str, Any]`` shape Pydantic models validate against; the
    helper short-circuits with 400 rather than letting Pydantic emit
    a confusing structural error."""
    response = client.request(
        method,
        path,
        content=b'["not", "an", "object"]',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400, response.text
    assert "JSON object" in response.json()["detail"]


def test_rollback_malformed_json_returns_400(client: TestClient) -> None:
    """Deep-review V2 Finding 4: rollback previously swallowed
    parse failures and silently fell back to ``checkpoint_id=None``
    (= latest checkpoint), which could deploy the wrong rollback.
    A malformed body is now a clean 400."""
    response = client.post(
        "/ajax-api/2.0/mlflow/caliber/agents/some-agent/rollback",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400


def test_rollback_empty_body_still_allowed(client: TestClient) -> None:
    """Rollback body is optional — empty must still resolve cleanly
    (404 because the agent doesn't exist, but not 400)."""
    response = client.post("/ajax-api/2.0/mlflow/caliber/agents/some-agent/rollback")
    assert response.status_code == 404


def test_apply_empty_body_still_allowed(client: TestClient) -> None:
    """The Apply endpoint ignores the request body — a missing body must
    still resolve cleanly (404 because the job doesn't exist, not 400)."""
    response = client.post("/ajax-api/2.0/mlflow/caliber/jobs/RFN-NONE/apply")
    assert response.status_code == 404


def test_invalid_utf8_body_returns_400(client: TestClient) -> None:
    """Backend V2 Finding 2: ``json.loads`` raises
    ``UnicodeDecodeError`` — not ``JSONDecodeError`` — on byte
    sequences that aren't valid UTF-* encodings (e.g. ``\\xff\\xfe\\xfd``).
    The old guard caught only ``JSONDecodeError``, so a hostile or
    corrupted client could bypass the contract and trigger a 500.
    The helper now catches both and returns the same structured 400."""
    response = client.post(
        "/ajax-api/2.0/mlflow/caliber/agents",
        content=b"\xff\xfe\xfd",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400, response.text
    assert "invalid JSON body" in response.json()["detail"]
