"""B15-Support.1 (#755): a real reference deployment reports which adapters it supports, and promotes nothing else.

``GET /capabilities`` answers per adapter ``supported``, ``preview`` or ``unavailable``. These scenarios read it from real
CALIBER apps on the migrated database of ``reference_app``:

* PostgreSQL (the "EGI scenarios (PostgreSQL)" job) runs ``CALIBER_PROFILE=reference``: the trusted document-request
  connector is ``supported`` there, and the release evaluation path is ``supported`` once the operator turns it on with a
  real evaluation provider, while MCP servers (the profile lists none) and OpenAPI integrations are never promoted, even
  with their area flag on and their routes answering;
* SQLite runs the development profile with the same safe settings, where nothing is ``supported``.

Reading capabilities is a pure read: it records no effect proposal and sends nothing downstream.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select
from starlette.testclient import TestClient

from caliber.config import CaliberConfig
from caliber.db.models import CaliberEffectProposal
from caliber.features import SUPPORT_EVIDENCE
from caliber.server import create_app
from tests.egi_scenarios.conftest import EDITOR_A, ReferenceApp

pytestmark = pytest.mark.xdist_group("egi-scenario-processes")

API = "/ajax-api/2.0/mlflow/caliber"

#: What an operator adds to turn the supported release path on: the feature, its evaluator and a real provider.
RELEASE_PATH_ON = {
    "CALIBER_FEATURE_RELEASE_CANDIDATES": "true",
    "CALIBER_WORKSPACE_RELEASE_EVALUATOR_ENABLED": "true",
    "CALIBER_EVAL_PROVIDER": "mlflow",
    "CALIBER_FEATURE_INTEGRATIONS": "true",
}


def _adapters(client: TestClient, reference_app: ReferenceApp) -> dict[str, Any]:
    response = client.get(f"{API}/capabilities", headers=reference_app.headers(EDITOR_A))
    assert response.status_code == 200, response.text
    return response.json()["data"]["adapters"]


def _proposals(reference_app: ReferenceApp) -> int:
    with reference_app.session_factory() as session:
        return session.execute(select(func.count()).select_from(CaliberEffectProposal)).scalar_one()


def test_a_default_reference_deployment_supports_the_connector_and_promotes_nothing_else(
    reference_app: ReferenceApp,
) -> None:
    before = _proposals(reference_app)

    adapters = _adapters(reference_app.client, reference_app)

    reference = reference_app.profile == "reference"
    connector = adapters["document_request_connector"]
    assert connector["status"] == ("supported" if reference else "preview")
    if reference:
        assert connector["reason_code"] is None
        assert connector["evidence"] == list(
            SUPPORT_EVIDENCE["document_request_connector"].scenarios
        )
    else:
        assert connector["reason_code"] == "not_reference_profile"
        assert connector["disabled_by"] == "CALIBER_PROFILE"
        assert connector["evidence"] == []
    # The area flags ship off, so the other three are unavailable with the same cause as their 404.
    for adapter_id, env_var in (
        ("release_evaluation", "CALIBER_FEATURE_RELEASE_CANDIDATES"),
        ("mcp_servers", "CALIBER_FEATURE_INTEGRATIONS"),
        ("openapi_integrations", "CALIBER_FEATURE_INTEGRATIONS"),
    ):
        assert adapters[adapter_id]["status"] == "unavailable"
        assert adapters[adapter_id]["reason_code"] == "feature_disabled"
        assert adapters[adapter_id]["disabled_by"] == env_var
        assert adapters[adapter_id]["evidence"] == []
    # Reading the answer wrote nothing.
    assert _proposals(reference_app) == before


def test_the_release_path_is_supported_only_with_a_real_provider_and_no_flag_promotes_mcp(
    reference_app: ReferenceApp,
) -> None:
    reference = reference_app.profile == "reference"
    configured = CaliberConfig.load(environ={**reference_app.env, **RELEASE_PATH_ON})
    with TestClient(create_app(configured)) as client:
        adapters = _adapters(client, reference_app)
        mcp_routes = client.get(f"{API}/mcp-servers", headers=reference_app.headers(EDITOR_A))
        release_routes = client.get(
            f"{API}/releases/candidates", headers=reference_app.headers(EDITOR_A)
        )

    # The routes answer (the flags are on): being reachable is not being supported.
    assert mcp_routes.json().get("reason_code") != "feature_disabled", mcp_routes.text
    assert release_routes.json().get("reason_code") != "feature_disabled", release_routes.text
    if reference:
        assert adapters["release_evaluation"]["status"] == "supported"
        assert adapters["release_evaluation"]["evidence"] == list(
            SUPPORT_EVIDENCE["release_evaluation"].scenarios
        )
        assert adapters["document_request_connector"]["status"] == "supported"
        assert adapters["mcp_servers"]["status"] == "unavailable"
        assert adapters["mcp_servers"]["reason_code"] == "mcp_not_in_reference_profile"
    else:
        assert adapters["release_evaluation"]["status"] == "preview"
        assert adapters["release_evaluation"]["reason_code"] == "not_reference_profile"
        assert adapters["mcp_servers"]["status"] == "preview"
    assert adapters["openapi_integrations"]["status"] == "preview"
    assert adapters["openapi_integrations"]["reason_code"] == "no_support_evidence"
    assert [a for a in adapters.values() if a["status"] == "supported" and not a["evidence"]] == []


def test_simulated_release_scores_and_a_disabled_evaluator_are_never_supported(
    reference_app: ReferenceApp,
) -> None:
    simulated = CaliberConfig.load(
        environ={**reference_app.env, **RELEASE_PATH_ON, "CALIBER_EVAL_PROVIDER": "fake"}
    )
    off = CaliberConfig.load(
        environ={
            **reference_app.env,
            **RELEASE_PATH_ON,
            "CALIBER_WORKSPACE_RELEASE_EVALUATOR_ENABLED": "false",
        }
    )

    with TestClient(create_app(simulated)) as client:
        fake = _adapters(client, reference_app)["release_evaluation"]
    with TestClient(create_app(off)) as client:
        disabled = _adapters(client, reference_app)["release_evaluation"]

    expected_fake = (
        "simulated_provider" if reference_app.profile == "reference" else "not_reference_profile"
    )
    assert (fake["status"], fake["reason_code"]) == ("preview", expected_fake)
    assert (disabled["status"], disabled["reason_code"]) == ("unavailable", "evaluator_disabled")
    assert disabled["disabled_by"] == "CALIBER_WORKSPACE_RELEASE_EVALUATOR_ENABLED"
    assert fake["evidence"] == disabled["evidence"] == []
