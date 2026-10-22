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

The shared ``app_config`` fixture turns every flag on (the rest of the suite exercises
those routes); the tests that care about the shipped default build their own config.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from caliber.config import CaliberConfig
from caliber.features import (
    FEATURES,
    PREFIX,
    REASON_CODE,
    disabled_message,
    feature_enabled,
    feature_for_path,
)
from caliber.server import create_app
from caliber.workflows.component_catalog import evaluate_requirements
from tests.conftest import DEFAULT_TEST_USER

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
