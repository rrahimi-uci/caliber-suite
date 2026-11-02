"""The alpha ships its unsupported areas behind feature flags, off by default.

``caliber.features`` gates four areas by path prefix in one middleware. Aria is the one
exception: it ships on by default (the other three stay off), so a deployment that wants
a clean alpha turns it off rather than on. These tests pin:

* the shipped default (aria on, the other three off) and how the environment turns each
  flag on or off;
* which paths belong to which flag, including the edges (``/releases/live`` is core,
  ``/cookbooks-extra`` is not ``/cookbooks``, ``/projects/{id}/releases:x`` is gated);
* that **every route registered by a gated module is covered by a flag**, so a route
  added to ``mcp_servers.py`` tomorrow cannot ship un-gated by accident;
* the HTTP behavior: a disabled area answers ``404`` with ``reason_code`` and the
  variable to set, a core route beside it still works, and enabling the flag restores it;
* ``/capabilities`` reports every flag, and MCP workflow nodes report themselves
  unavailable while integrations are off.

The Beta section at the end pins per-adapter support (``adapters`` in ``/capabilities``): only the
trusted reference connector and the release evaluation path can be ``supported``, only in a valid
reference deployment whose code still matches the pinned scenario evidence; MCP servers and OpenAPI
integrations never are; a disabled adapter reports the cause, code and text of its 404; and the
flag block older readers parse is unchanged.

The shared ``app_config`` fixture turns every flag on (the rest of the suite exercises
those routes); the tests that care about the shipped default build their own config.
"""

from __future__ import annotations

import ast
import itertools
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

import caliber.eval.evidence as evidence_module
import caliber.features as features_module
import caliber.profile as profile_module
from caliber.config import CaliberConfig
from caliber.features import (
    ADAPTERS,
    FEATURES,
    PREFIX,
    PREVIEW,
    REASON_CODE,
    SUPPORT_EVIDENCE,
    SUPPORT_STATUSES,
    SUPPORTED,
    UNAVAILABLE,
    adapter_capabilities,
    capabilities_block,
    disabled_message,
    feature_enabled,
    feature_for_path,
)
from caliber.profile import reference_profile_violations
from caliber.schemas import AdapterCapabilitySchema, FeatureFlagSchema, PlatformCapabilitiesSchema
from caliber.server import create_app
from caliber.workflows.component_catalog import evaluate_requirements
from tests.conftest import DEFAULT_TEST_USER
from tests.test_profile import SAFE

ALL = {flag.id for flag in FEATURES}
FLAG_ENV = {flag.id: flag.env_var for flag in FEATURES}

#: Route modules whose every route belongs to exactly one flag.
GATED_MODULES = {
    "assistant": "aria",
    "aria_plans": "aria",
    "mcp_servers": "integrations",
    "openapi_integrations": "integrations",
    "cookbooks": "cookbooks",
    "workspace_releases": "release_candidates",
    "workspace_release_operations": "release_candidates",
}


def _off(app_config: CaliberConfig, *, keep: frozenset[str] = frozenset()) -> CaliberConfig:
    return app_config.model_copy(
        update={f"feature_{flag_id}_enabled": flag_id in keep for flag_id in ALL}
    )


@pytest.fixture
def make_client(
    app_config: CaliberConfig, engine: Engine, session_factory: sessionmaker[Session]
) -> Iterator[object]:
    clients: list[TestClient] = []

    def build(config: CaliberConfig) -> TestClient:
        app = create_app(config=config)
        app.state.engine = engine
        app.state.session_factory = session_factory
        test_client = TestClient(app, headers={"X-CALIBER-User": DEFAULT_TEST_USER})
        test_client.__enter__()
        clients.append(test_client)
        return test_client

    yield build
    for test_client in clients:
        test_client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# The shipped default and the environment
# ---------------------------------------------------------------------------


def test_the_shipped_default_turns_on_aria_and_leaves_the_rest_off() -> None:
    config = CaliberConfig.load(environ={})

    assert {flag.id: feature_enabled(config, flag.id) for flag in FEATURES} == {
        "aria": True,
        "integrations": False,
        "cookbooks": False,
        "release_candidates": False,
    }
    assert CaliberConfig().feature_aria_enabled is True


@pytest.mark.parametrize("flag", FEATURES, ids=lambda flag: flag.id)
@pytest.mark.parametrize("raw", ["true", "1", "yes", "on", "TRUE"])
def test_the_environment_turns_one_flag_on_and_only_that_one(flag, raw: str) -> None:
    environ = {flag.env_var: raw}
    if flag.id != "aria":
        # Aria ships on by default, so pin it off here unless it's the flag under test,
        # or it would always show up alongside whichever other flag we just turned on.
        environ["CALIBER_FEATURE_ARIA"] = "false"
    config = CaliberConfig.load(environ=environ)

    assert feature_enabled(config, flag.id) is True
    assert [other.id for other in FEATURES if feature_enabled(config, other.id)] == [flag.id]


@pytest.mark.parametrize("raw", ["", "false", "0", "no", "off", "nope"])
def test_anything_else_leaves_a_flag_off(raw: str) -> None:
    config = CaliberConfig.load(environ={"CALIBER_FEATURE_ARIA": raw})

    assert config.feature_aria_enabled is False


def test_the_registry_names_a_real_config_field_and_variable_for_every_flag() -> None:
    for flag in FEATURES:
        assert flag.config_field in CaliberConfig.model_fields
        assert flag.env_var == f"CALIBER_FEATURE_{flag.id.upper()}"
        assert flag.stability in {"preview", "record_only"}


# ---------------------------------------------------------------------------
# Path ownership
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/assistant/sessions", "aria"),
        ("/assistant/sessions/s1/messages", "aria"),
        ("/aria/plans", "aria"),
        ("/aria/capabilities", "aria"),
        ("/mcp-servers", "integrations"),
        ("/mcp-servers/m1/tools/t/policy", "integrations"),
        ("/openapi-integrations/i1/tool-drafts/d1/publish", "integrations"),
        ("/cookbooks", "cookbooks"),
        ("/cookbooks/01/install", "cookbooks"),
        ("/releases/candidates", "release_candidates"),
        ("/releases/candidates/c1/signoffs", "release_candidates"),
        ("/releases/report-jobs/j1", "release_candidates"),
        ("/projects/p1/releases", "release_candidates"),
        ("/projects/p1/releases/r1/evaluate", "release_candidates"),
        ("/projects/p1/releases/r1/operations/o1:apply", "release_candidates"),
        # Core, never gated.
        ("/releases/live", None),
        ("/releases/timeline", None),
        ("/releases/operations", None),
        ("/releases/operations/reconcile", None),
        ("/projects/p1", None),
        ("/projects/p1/revisions", None),
        ("/projects/p1/change-requests", None),
        ("/prompts", None),
        ("/skills", None),
        ("/tools", None),
        ("/workflows", None),
        ("/capabilities", None),
        # A longer word is not the prefix.
        ("/cookbooks-extra", None),
        ("/assistants", None),
        ("/aria-other", None),
        ("/mcp-servers-archive", None),
    ],
)
def test_path_ownership(path: str, expected: str | None) -> None:
    flag = feature_for_path(PREFIX + path)

    assert (flag.id if flag else None) == expected


def test_paths_outside_the_api_prefix_are_never_gated() -> None:
    for path in ("/caliber/cookbooks", "/cookbooks", "/caliber/aria/plans", "/health"):
        assert feature_for_path(path) is None


def test_every_route_of_a_gated_module_is_covered_by_its_flag(app_config: CaliberConfig) -> None:
    """The guard that makes the prefix list safe to maintain: a new route in a gated module
    that no prefix covers fails here instead of shipping un-gated."""
    app = create_app(config=app_config)
    seen: dict[str, int] = {}
    uncovered: list[str] = []
    for route in app.routes:
        module = getattr(getattr(route, "endpoint", None), "__module__", "").rsplit(".", 1)[-1]
        if module not in GATED_MODULES:
            continue
        seen[module] = seen.get(module, 0) + 1
        flag = feature_for_path(str(getattr(route, "path", "")))
        if flag is None or flag.id != GATED_MODULES[module]:
            uncovered.append(f"{module}: {route.path} -> {flag.id if flag else None}")

    assert not uncovered, "routes not covered by their feature flag:\n" + "\n".join(uncovered)
    assert set(seen) == set(GATED_MODULES), "a gated module registered no routes"


def test_release_candidate_routes_in_releases_py_are_covered(app_config: CaliberConfig) -> None:
    """``releases.py`` mixes core (live board, timeline, operations) and candidate routes,
    so it cannot be a whole-module entry above; check it route by route."""
    app = create_app(config=app_config)
    for route in app.routes:
        module = getattr(getattr(route, "endpoint", None), "__module__", "").rsplit(".", 1)[-1]
        path = str(getattr(route, "path", ""))
        if module != "releases":
            continue
        relative = path.removeprefix(PREFIX)
        is_candidate = relative.startswith(("/releases/candidates", "/releases/report-jobs"))
        flag = feature_for_path(path)
        assert (flag is not None) is is_candidate, path


# ---------------------------------------------------------------------------
# HTTP behavior
# ---------------------------------------------------------------------------


GATED_CALLS = [
    ("aria", "GET", "/assistant/sessions"),
    ("aria", "GET", "/aria/plans"),
    ("aria", "GET", "/aria/capabilities"),
    ("integrations", "GET", "/mcp-servers"),
    ("integrations", "GET", "/openapi-integrations"),
    ("cookbooks", "GET", "/cookbooks"),
    ("release_candidates", "GET", "/releases/candidates"),
    ("release_candidates", "GET", "/projects/anything/releases"),
]


@pytest.mark.parametrize(("flag_id", "method", "path"), GATED_CALLS)
def test_a_disabled_area_answers_404_with_a_reason_and_the_variable(
    make_client, app_config: CaliberConfig, flag_id: str, method: str, path: str
) -> None:
    client = make_client(_off(app_config))

    response = client.request(method, PREFIX + path)

    assert response.status_code == 404
    body = response.json()
    assert body["reason_code"] == REASON_CODE == "feature_disabled"
    assert body["status_code"] == 404
    assert FLAG_ENV[flag_id] + "=true" in body["detail"]
    flag = next(flag for flag in FEATURES if flag.id == flag_id)
    assert body["detail"] == disabled_message(flag)


@pytest.mark.parametrize(("flag_id", "method", "path"), GATED_CALLS)
def test_enabling_the_flag_restores_the_area(
    make_client, app_config: CaliberConfig, flag_id: str, method: str, path: str
) -> None:
    client = make_client(_off(app_config, keep=frozenset({flag_id})))

    response = client.request(method, PREFIX + path)

    assert response.json().get("reason_code") != REASON_CODE, response.text


def test_a_write_to_a_disabled_area_is_refused_before_it_reaches_the_handler(
    make_client, app_config: CaliberConfig
) -> None:
    client = make_client(_off(app_config))

    response = client.post(PREFIX + "/mcp-servers", json={"name": "x"})

    assert response.status_code == 404
    assert response.json()["reason_code"] == "feature_disabled"


def test_an_unauthenticated_call_to_a_disabled_area_gets_the_same_404(
    make_client, app_config: CaliberConfig
) -> None:
    client = make_client(_off(app_config))

    response = client.get(PREFIX + "/cookbooks", headers={"X-CALIBER-User": ""})

    # The gate runs outside auth: it does not reveal anything about the caller.
    assert response.status_code == 404
    assert response.json()["reason_code"] == "feature_disabled"


def test_core_routes_beside_a_disabled_area_still_work(
    make_client, app_config: CaliberConfig
) -> None:
    client = make_client(_off(app_config))

    for path in ("/releases/live", "/releases/timeline", "/prompts", "/skills", "/tools"):
        response = client.get(PREFIX + path)
        assert response.status_code == 200, (path, response.status_code, response.text)


def test_the_gate_reads_the_live_config(make_client, app_config: CaliberConfig) -> None:
    client = make_client(_off(app_config))
    assert client.get(PREFIX + "/cookbooks").status_code == 404

    client.app.state.config = _off(app_config, keep=frozenset({"cookbooks"}))

    assert client.get(PREFIX + "/cookbooks").status_code == 200


def test_the_aria_plan_worker_is_not_built_while_aria_is_off(
    make_client, app_config: CaliberConfig
) -> None:
    off = make_client(_off(app_config))
    on = make_client(_off(app_config, keep=frozenset({"aria"})))

    assert off.app.state.aria_plan_worker is None
    assert on.app.state.aria_plan_worker is not None


# ---------------------------------------------------------------------------
# /capabilities and the workflow palette
# ---------------------------------------------------------------------------


def test_capabilities_report_every_flag_on_or_off(make_client, app_config: CaliberConfig) -> None:
    client = make_client(_off(app_config, keep=frozenset({"cookbooks"})))

    data = client.get(PREFIX + "/capabilities").json()["data"]

    assert set(data["features"]) == ALL
    assert data["features"]["cookbooks"] == {
        "enabled": True,
        "env_var": "CALIBER_FEATURE_COOKBOOKS",
        "label": "Cookbooks",
        "stability": "preview",
    }
    assert data["features"]["aria"]["enabled"] is False
    assert data["features"]["release_candidates"]["stability"] == "record_only"
    assert data["openapi_integrations"]["enabled"] is False


def test_capabilities_show_integrations_enabled_when_the_flag_is_on(
    make_client, app_config: CaliberConfig
) -> None:
    client = make_client(app_config)

    data = client.get(PREFIX + "/capabilities").json()["data"]

    assert data["features"]["integrations"]["enabled"] is True
    assert data["openapi_integrations"]["enabled"] is True


def test_mcp_nodes_are_unavailable_while_integrations_are_off(
    app_config: CaliberConfig,
) -> None:
    off = _off(app_config)
    on = _off(app_config, keep=frozenset({"integrations"}))

    assert evaluate_requirements(off)["mcp_server"] is False
    # With integrations on, a server is registered data, which the config cannot judge.
    assert evaluate_requirements(on)["mcp_server"] is None


# ---------------------------------------------------------------------------
# Per-adapter support (Beta, B15-Support.1, #755)
# ---------------------------------------------------------------------------
#
# ``adapters`` answers a different question than ``features``: not "is the area on" but "is this
# adapter *supported*". Only the trusted reference connector and the release evaluation path can be,
# and only in a valid reference deployment whose live code still matches their scenario evidence.

CALIBER_DIR = Path(__file__).resolve().parents[1]
ADAPTER_IDS = [spec.id for spec in ADAPTERS]
BLOCK_KEYS = {"status", "kind", "label", "reason_code", "disabled_by", "message", "evidence"}


def _reference(**updates: Any) -> CaliberConfig:
    """A configuration that passes the reference profile, with the supported release path switched on."""
    base = {
        **SAFE,
        "feature_release_candidates_enabled": True,
        "workspace_release_evaluator_enabled": True,
        "eval_provider": "mlflow",
        "feature_integrations_enabled": True,
    }
    return CaliberConfig().model_copy(update={**base, **updates})


def _development(**updates: Any) -> CaliberConfig:
    base = {
        "feature_release_candidates_enabled": True,
        "workspace_release_evaluator_enabled": True,
        "eval_provider": "mlflow",
        "feature_integrations_enabled": True,
    }
    return CaliberConfig().model_copy(update={**base, **updates})


def _status(config: CaliberConfig) -> dict[str, tuple[str, str | None]]:
    return {
        adapter_id: (block["status"], block["reason_code"])
        for adapter_id, block in adapter_capabilities(config).items()
    }


def test_supported_reference_visible() -> None:
    """The reference connector and the release evaluation path report supported, with their scenarios."""
    adapters = adapter_capabilities(_reference())

    assert _status(_reference()) == {
        "document_request_connector": (SUPPORTED, None),
        "release_evaluation": (SUPPORTED, None),
        "mcp_servers": (UNAVAILABLE, "mcp_not_in_reference_profile"),
        "openapi_integrations": (PREVIEW, "no_support_evidence"),
    }
    for adapter_id in ("document_request_connector", "release_evaluation"):
        block = adapters[adapter_id]
        assert block["disabled_by"] is None
        assert block["evidence"] == list(SUPPORT_EVIDENCE[adapter_id].scenarios)
        assert block["message"].startswith(block["label"])
    assert adapters["document_request_connector"]["kind"] == "connector"
    assert adapters["release_evaluation"]["kind"] == "release_evaluation"


def test_every_block_has_the_same_keys_and_evidence_only_when_supported() -> None:
    for config in (_reference(), _development(), CaliberConfig()):
        for block in adapter_capabilities(config).values():
            assert set(block) == BLOCK_KEYS
            assert block["status"] in SUPPORT_STATUSES
            assert (block["evidence"] != []) is (block["status"] == SUPPORTED)
            assert (block["reason_code"] is None) is (block["status"] == SUPPORTED)


def test_unsupported_mcp_not_promoted() -> None:
    """MCP servers and OpenAPI integrations are never supported, whatever the flags and profile say."""
    for profile_updates in ({}, SAFE):
        for release, evaluator, integrations, cookbooks in itertools.product(
            [False, True], repeat=4
        ):
            config = CaliberConfig().model_copy(
                update={
                    **profile_updates,
                    "feature_release_candidates_enabled": release,
                    "workspace_release_evaluator_enabled": evaluator,
                    "feature_integrations_enabled": integrations,
                    "feature_cookbooks_enabled": cookbooks,
                    "eval_provider": "mlflow",
                }
            )
            adapters = adapter_capabilities(config)
            for adapter_id in ("mcp_servers", "openapi_integrations"):
                assert adapters[adapter_id]["status"] != SUPPORTED, (profile_updates, adapter_id)
                assert adapters[adapter_id]["evidence"] == []
                # The area flag alone decides unavailable vs not for the OpenAPI adapter.
                if adapter_id == "openapi_integrations":
                    expected = PREVIEW if integrations else UNAVAILABLE
                    assert adapters[adapter_id]["status"] == expected


@pytest.mark.parametrize("adapter_id", ["mcp_servers", "openapi_integrations"])
def test_integrations_on_is_preview_outside_the_reference_profile(adapter_id: str) -> None:
    block = adapter_capabilities(_development())[adapter_id]

    assert block["status"] == PREVIEW
    assert block["reason_code"] == "no_support_evidence"
    assert block["disabled_by"] == "profile"
    assert "no scenario evidence" in block["message"]


def test_mcp_servers_are_unavailable_in_the_reference_profile_while_it_lists_none() -> None:
    block = adapter_capabilities(_reference())["mcp_servers"]

    assert block["status"] == UNAVAILABLE
    assert block["reason_code"] == "mcp_not_in_reference_profile"
    assert block["disabled_by"] == "profile"
    assert "no MCP command or host" in block["message"]


def test_a_profile_that_listed_an_mcp_host_would_still_leave_servers_at_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Listing a host is an ADR change; it does not create scenario evidence, so it promotes nothing."""
    monkeypatch.setattr(features_module, "REFERENCE_MCP_ALLOWLIST", ("mcp.example.invalid",))

    block = adapter_capabilities(_reference())["mcp_servers"]

    assert (block["status"], block["reason_code"]) == (PREVIEW, "no_support_evidence")


def test_unavailable_evaluator_disabled() -> None:
    """The release flag on but the evaluator off: unavailable, naming the variable to set."""
    config = _reference(workspace_release_evaluator_enabled=False)

    block = adapter_capabilities(config)["release_evaluation"]

    assert block["status"] == UNAVAILABLE
    assert block["reason_code"] == "evaluator_disabled"
    assert block["disabled_by"] == "CALIBER_WORKSPACE_RELEASE_EVALUATOR_ENABLED"
    assert block["message"] == (
        "Workspace release evaluation is unavailable in this deployment. "
        "Set CALIBER_WORKSPACE_RELEASE_EVALUATOR_ENABLED=true to enable it."
    )
    assert block["evidence"] == []


def test_a_closed_area_flag_wins_and_keeps_the_404s_code_and_text() -> None:
    flag = next(flag for flag in FEATURES if flag.id == "release_candidates")

    block = adapter_capabilities(_reference(feature_release_candidates_enabled=False))[
        "release_evaluation"
    ]

    assert block["status"] == UNAVAILABLE
    assert block["reason_code"] == REASON_CODE == "feature_disabled"
    assert block["disabled_by"] == flag.env_var
    assert block["message"] == disabled_message(flag)
    # The flag wins even when the evaluator is also off.
    both = _reference(
        feature_release_candidates_enabled=False, workspace_release_evaluator_enabled=False
    )
    assert adapter_capabilities(both)["release_evaluation"]["reason_code"] == "feature_disabled"


@pytest.mark.parametrize("adapter_id", ["mcp_servers", "openapi_integrations"])
def test_integrations_off_makes_both_integration_adapters_unavailable(adapter_id: str) -> None:
    flag = next(flag for flag in FEATURES if flag.id == "integrations")
    for config in (_development(feature_integrations_enabled=False), CaliberConfig()):
        block = adapter_capabilities(config)[adapter_id]

        assert block["status"] == UNAVAILABLE
        assert block["reason_code"] == "feature_disabled"
        assert block["disabled_by"] == "CALIBER_FEATURE_INTEGRATIONS"
        assert block["message"] == disabled_message(flag)


def test_the_connector_needs_no_area_flag_but_does_need_the_reference_profile() -> None:
    development = adapter_capabilities(CaliberConfig())["document_request_connector"]

    assert development["status"] == PREVIEW
    assert development["reason_code"] == "not_reference_profile"
    assert development["disabled_by"] == "CALIBER_PROFILE"
    assert "CALIBER_PROFILE=reference" in development["message"]
    no_flags = _reference(
        feature_integrations_enabled=False, feature_release_candidates_enabled=False
    )
    assert adapter_capabilities(no_flags)["document_request_connector"]["status"] == SUPPORTED


def test_release_evaluation_is_preview_outside_the_reference_profile() -> None:
    block = adapter_capabilities(_development())["release_evaluation"]

    assert (block["status"], block["reason_code"]) == (PREVIEW, "not_reference_profile")
    assert block["disabled_by"] == "CALIBER_PROFILE"


@pytest.mark.parametrize("provider", ["fake", "FAKE", "Fake"])
def test_release_evaluation_with_simulated_scores_is_preview(provider: str) -> None:
    block = adapter_capabilities(_reference(eval_provider=provider))["release_evaluation"]

    assert (block["status"], block["reason_code"]) == (PREVIEW, "simulated_provider")
    assert block["disabled_by"] == "CALIBER_EVAL_PROVIDER"
    assert "scores are simulated" in block["message"]
    assert "CALIBER_EVAL_PROVIDER=mlflow" in block["message"]


@pytest.mark.parametrize(
    "provider", ["bogus", " mlflow ", "mlflow ", "auto", "", "  fake ", "openai"]
)
def test_only_the_exact_mlflow_provider_is_a_real_one(provider: str) -> None:
    """An allowlist spelled the way ``build_provider`` reads it: anything else is never supported."""
    block = adapter_capabilities(_reference(eval_provider=provider))["release_evaluation"]

    assert (block["status"], block["reason_code"]) == (PREVIEW, "eval_provider_unrecognized")
    assert block["disabled_by"] == "CALIBER_EVAL_PROVIDER"
    assert "not a provider this build supports" in block["message"]
    assert block["evidence"] == []


@pytest.mark.parametrize("provider", ["mlflow", "MLFLOW", "MLflow"])
def test_mlflow_is_a_real_provider_however_it_is_cased(provider: str) -> None:
    block = adapter_capabilities(_reference(eval_provider=provider))["release_evaluation"]

    assert block["status"] == SUPPORTED


@pytest.mark.parametrize("adapter_id", ["document_request_connector", "release_evaluation"])
def test_supported_fails_closed_when_the_background_loops_are_off(adapter_id: str) -> None:
    """The cited recovery scenarios pass because the reconciler and evaluator loops run; with them off, not supported."""
    block = adapter_capabilities(_reference(background_tasks_enabled=False))[adapter_id]

    assert (block["status"], block["reason_code"]) == (UNAVAILABLE, "background_tasks_disabled")
    assert block["disabled_by"] == "CALIBER_BACKGROUND_TASKS_ENABLED"
    assert "Set CALIBER_BACKGROUND_TASKS_ENABLED=true" in block["message"]
    assert block["evidence"] == []


def test_the_connector_is_unavailable_while_the_effect_reconciler_is_off() -> None:
    adapters = adapter_capabilities(_reference(effect_reconciler_enabled=False))

    block = adapters["document_request_connector"]
    assert (block["status"], block["reason_code"]) == (UNAVAILABLE, "reconciler_disabled")
    assert block["disabled_by"] == "CALIBER_EFFECT_RECONCILER_ENABLED"
    assert "Set CALIBER_EFFECT_RECONCILER_ENABLED=true" in block["message"]
    assert block["evidence"] == []
    # The release evaluator's evidence does not rely on the effect reconciler.
    assert adapters["release_evaluation"]["status"] == SUPPORTED


def test_the_workflow_run_queue_is_not_a_condition_of_either_claim() -> None:
    """The cited scenarios run with the queue at its default (off), so it must not withdraw support."""
    adapters = adapter_capabilities(_reference(workflow_run_queue_enabled=False))

    assert adapters["document_request_connector"]["status"] == SUPPORTED
    assert adapters["release_evaluation"]["status"] == SUPPORTED


def test_the_evaluator_switch_is_checked_before_the_profile() -> None:
    """Evaluator off is ``unavailable`` even outside the reference profile, never the weaker ``preview``."""
    block = adapter_capabilities(_development(workspace_release_evaluator_enabled=False))[
        "release_evaluation"
    ]

    assert (block["status"], block["reason_code"]) == (UNAVAILABLE, "evaluator_disabled")


def test_the_connector_checks_the_profile_before_the_reconciler() -> None:
    block = adapter_capabilities(_development(effect_reconciler_enabled=False))[
        "document_request_connector"
    ]

    assert (block["status"], block["reason_code"]) == (PREVIEW, "not_reference_profile")


@pytest.mark.parametrize("adapter_id", ["document_request_connector", "release_evaluation"])
def test_a_reference_configuration_that_breaks_the_profile_is_unavailable(adapter_id: str) -> None:
    config = _reference(csrf_enabled=False, mcp_allow_insecure_http=True)

    block = adapter_capabilities(config)[adapter_id]

    assert (block["status"], block["reason_code"]) == (UNAVAILABLE, "reference_profile_violations")
    assert block["disabled_by"] == "profile"
    assert "csrf_disabled, mcp_insecure_http" in block["message"]
    # Codes are listed in the order the profile checks them, which is not alphabetical.
    unsorted_config = _reference(csrf_enabled=False, database_url="sqlite:///x.db")
    unsorted = adapter_capabilities(unsorted_config)[adapter_id]["message"]
    assert "(sqlite_database, csrf_disabled)" in unsorted
    assert [v.code for v in reference_profile_violations(unsorted_config)] == [
        "sqlite_database",
        "csrf_disabled",
    ]
    # The codes are named; no configured value is.
    assert "db.example.invalid" not in block["message"]
    assert "secret" not in block["message"]


def test_a_changed_admission_record_withdraws_the_connector_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changed = tuple(
        replace(record, capability_digest="sha256:" + "0" * 64)
        if record.backend_kind == "document_request"
        else record
        for record in profile_module.REFERENCE_CONNECTORS
    )
    monkeypatch.setattr(profile_module, "REFERENCE_CONNECTORS", changed)

    adapters = adapter_capabilities(_reference())

    block = adapters["document_request_connector"]
    assert (block["status"], block["reason_code"]) == (UNAVAILABLE, "connector_capability_mismatch")
    assert block["disabled_by"] == "profile"
    assert block["evidence"] == []
    # The other adapter is not affected by it.
    assert adapters["release_evaluation"]["status"] == SUPPORTED


def test_an_unlisted_connector_is_not_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(profile_module, "REFERENCE_CONNECTORS", ())

    block = adapter_capabilities(_reference())["document_request_connector"]

    assert (block["status"], block["reason_code"]) == (UNAVAILABLE, "connector_not_admitted")


def test_a_replaced_evaluator_implementation_withdraws_the_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pinned = SUPPORT_EVIDENCE["release_evaluation"]
    monkeypatch.setitem(
        SUPPORT_EVIDENCE,
        "release_evaluation",
        replace(pinned, implementation="caliber.somewhere.else:Evaluator"),
    )

    block = adapter_capabilities(_reference())["release_evaluation"]

    assert (block["status"], block["reason_code"]) == (UNAVAILABLE, "evaluator_evidence_mismatch")
    assert block["disabled_by"] == "profile"
    assert block["evidence"] == []


def test_a_changed_evidence_shape_withdraws_the_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        evidence_module, "EVIDENCE_SCHEMA_VERSION", evidence_module.EVIDENCE_SCHEMA_VERSION + 1
    )

    block = adapter_capabilities(_reference())["release_evaluation"]

    assert (block["status"], block["reason_code"]) == (UNAVAILABLE, "evaluator_evidence_mismatch")


def test_support_is_derived_never_asserted() -> None:
    """No setting marks an adapter supported, and only the two evidenced adapters can be."""
    assert [name for name in CaliberConfig.model_fields if "support" in name] == []
    assert set(SUPPORT_EVIDENCE) == {"document_request_connector", "release_evaluation"}
    assert set(SUPPORT_EVIDENCE) <= set(ADAPTER_IDS)
    assert len(set(ADAPTER_IDS)) == len(ADAPTER_IDS) == 4
    for spec in ADAPTERS:
        assert spec.feature_id is None or spec.feature_id in ALL
    for adapter_id, evidence in SUPPORT_EVIDENCE.items():
        assert evidence.adapter_id == adapter_id


def test_the_cited_scenarios_are_exactly_the_reviewed_ones() -> None:
    """Pin the content of each claim, so swapping a cited test for another is a reviewed change."""
    assert SUPPORT_EVIDENCE["document_request_connector"].scenarios == (
        "tests/egi_scenarios/test_connector_capabilities.py::test_stable_key_mutates_once",
        "tests/egi_scenarios/test_connector_capabilities.py::test_stale_precondition_returns_412_and_no_mutation",
        "tests/egi_scenarios/test_connector_capabilities.py::test_query_by_key_after_lost_response",
        "tests/egi_scenarios/test_b04_dispatcher.py::test_dispatcher_uses_canonical_approval_and_registered_connector",
        "tests/egi_scenarios/test_v07_restart_exactly_once.py::test_kill_after_downstream_commit",
    )
    release = SUPPORT_EVIDENCE["release_evaluation"]
    assert release.scenarios == (
        "tests/egi_scenarios/test_v13_release_evaluation.py::test_evaluator_crash_recovers",
        "tests/egi_scenarios/test_v13_release_evaluation.py::test_worker_records_provider_run_before_completion_crash",
        "tests/egi_scenarios/test_v13_release_evaluation.py::test_recovery_before_provider_result_keeps_attempt_history",
    )
    assert release.implementation == (
        "caliber.orchestrator.workspace_release_evaluator:WorkspaceReleaseEvaluator"
    )
    assert release.evidence_schema_version == 1


def test_the_adapters_are_listed_in_the_documented_order() -> None:
    """``docs/operate/reference-profile.md`` lists them in this order, and so does ``/capabilities``."""
    assert ADAPTER_IDS == [
        "document_request_connector",
        "release_evaluation",
        "mcp_servers",
        "openapi_integrations",
    ]
    assert list(adapter_capabilities(CaliberConfig())) == ADAPTER_IDS


def test_every_support_scenario_names_a_real_test_function() -> None:
    """A claim cannot cite a scenario that does not exist (the evidence is real, not decorative)."""
    for evidence in SUPPORT_EVIDENCE.values():
        assert evidence.scenarios
        assert len(set(evidence.scenarios)) == len(evidence.scenarios)
        for node_id in evidence.scenarios:
            relative, _, name = node_id.partition("::")
            assert relative.startswith("tests/egi_scenarios/test_"), node_id
            source = CALIBER_DIR / relative
            assert source.is_file(), f"{node_id}: {relative} does not exist"
            defined = {
                node.name
                for node in ast.walk(ast.parse(source.read_text()))
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            assert name.startswith("test_"), node_id
            assert name in defined, f"{node_id}: no test function {name!r}"


def test_compatibility_values() -> None:
    """The flag block a pre-Beta reader parses is unchanged; ``adapters`` is purely additive."""
    assert {flag.stability for flag in FEATURES} == {"preview", "record_only"}
    assert set(FeatureFlagSchema.model_fields) == {"enabled", "env_var", "label", "stability"}
    for block in capabilities_block(CaliberConfig()).values():
        assert set(block) == {"enabled", "env_var", "label", "stability"}
        assert block["stability"] in {"preview", "record_only"}
    assert SUPPORT_STATUSES == ("unavailable", "preview", "supported")
    assert set(AdapterCapabilitySchema.model_fields) == BLOCK_KEYS
    # A payload with no ``adapters`` (an older server) still validates, as an empty map.
    assert PlatformCapabilitiesSchema.model_fields["adapters"].default_factory is dict


def test_the_alpha_default_promotes_nothing() -> None:
    """The shipped defaults keep every adapter below supported: preview or unavailable."""
    statuses = {
        adapter_id: block["status"]
        for adapter_id, block in adapter_capabilities(CaliberConfig.load(environ={})).items()
    }

    assert statuses == {
        "document_request_connector": PREVIEW,
        "release_evaluation": UNAVAILABLE,
        "mcp_servers": UNAVAILABLE,
        "openapi_integrations": UNAVAILABLE,
    }


def test_the_capabilities_route_serves_adapters_beside_unchanged_features(
    make_client, app_config: CaliberConfig
) -> None:
    client = make_client(_off(app_config, keep=frozenset({"integrations"})))

    data = client.get(PREFIX + "/capabilities").json()["data"]

    assert set(data["adapters"]) == set(ADAPTER_IDS)
    for block in data["adapters"].values():
        assert set(block) == BLOCK_KEYS
    assert data["adapters"]["mcp_servers"]["status"] == PREVIEW
    assert data["adapters"]["release_evaluation"]["reason_code"] == "feature_disabled"
    assert data["adapters"]["document_request_connector"]["status"] == PREVIEW
    assert set(data["features"]) == ALL
    assert data["features"]["integrations"]["stability"] == "preview"
    assert data["openapi_integrations"]["stability"] == "beta"


def test_the_capabilities_route_reads_the_live_config(
    make_client, app_config: CaliberConfig
) -> None:
    client = make_client(_off(app_config))
    before = client.get(PREFIX + "/capabilities").json()["data"]["adapters"]
    assert before["mcp_servers"]["status"] == UNAVAILABLE

    client.app.state.config = _off(app_config, keep=frozenset({"integrations"}))

    after = client.get(PREFIX + "/capabilities").json()["data"]["adapters"]
    assert after["mcp_servers"]["status"] == PREVIEW


@pytest.mark.parametrize(
    ("adapter_id", "flag_id", "path"),
    [
        ("release_evaluation", "release_candidates", "/releases/candidates"),
        ("mcp_servers", "integrations", "/mcp-servers"),
        ("openapi_integrations", "integrations", "/openapi-integrations"),
    ],
)
def test_an_unavailable_adapter_and_its_404_say_the_same_thing(
    make_client, app_config: CaliberConfig, adapter_id: str, flag_id: str, path: str
) -> None:
    """Disabled paths keep one error: the adapter's message is the refusal's detail, under the same code."""
    client = make_client(_off(app_config))

    block = client.get(PREFIX + "/capabilities").json()["data"]["adapters"][adapter_id]
    refused = client.get(PREFIX + path)

    assert refused.status_code == 404
    assert refused.json()["detail"] == block["message"]
    assert refused.json()["reason_code"] == block["reason_code"] == REASON_CODE
    assert block["disabled_by"] == FLAG_ENV[flag_id]
