"""Wave 3b, first slice: agent CRUD (completing the ``agents`` GA family to
100%) plus the prompt/skill release-lifecycle gaps -- rollback, baseline,
bind, and (for skills) the agent-free calibrate front door.

Every test pins the exact path and method, per the same discipline
``test_resources_governance.py`` established for wave 3a: this is precisely
the class of bug ``test_sdk_api_coverage.py`` exists to catch.
"""

from __future__ import annotations

from typing import Any

import httpx

from caliber_sdk import CaliberClient
from caliber_sdk.models import Agent, decode

BASE = "https://caliber.test"


def client_with(handler: Any) -> CaliberClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return CaliberClient(BASE, token="calpat_test", http_client=http)


def envelope(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"data": data})


def _seen_path(request: httpx.Request) -> str:
    return f"{request.method} {request.url.path.rsplit('/caliber', 1)[-1]}"


# --- agents: models ----------------------------------------------------------


def test_agent_decodes_the_governance_fields() -> None:
    agent = decode(
        Agent,
        {
            "agent_id": "AGT-1",
            "experiment_id": "42",
            "name": "support-triage",
            "owner": "ops",
            "enabled": True,
            "required_approvals": 2,
            "optimize_for": "quality",
        },
    )
    assert agent.agent_id == "AGT-1"
    assert agent.enabled is True
    assert agent.required_approvals == 2


# --- agents: CRUD --------------------------------------------------------------


def test_agent_list_get_create_update_delete_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        if request.url.path.endswith("/agents") and request.method == "GET":
            return envelope([{"agent_id": "AGT-1"}])
        if request.method == "DELETE":
            return envelope({"agent_id": "AGT-1", "deleted": True})
        return envelope({"agent_id": "AGT-1", "name": "support-triage"}, status=201)

    with client_with(handler) as caliber:
        agents = caliber.agents.list()
        created = caliber.agents.create("AGT-1", experiment_id="42", name="support-triage")
        fetched = caliber.agents.get("AGT-1")
        updated = caliber.agents.update("AGT-1", enabled=False)
        deleted = caliber.agents.delete("AGT-1")

    assert seen == [
        "GET /agents",
        "POST /agents",
        "GET /agents/AGT-1",
        "PATCH /agents/AGT-1",
        "DELETE /agents/AGT-1",
    ]
    assert [a.agent_id for a in agents] == ["AGT-1"]
    assert created.name == "support-triage"
    assert fetched.agent_id == "AGT-1"
    assert updated.agent_id == "AGT-1"
    assert deleted is True


def test_agent_create_sends_the_required_fields_and_extra_options() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        return envelope({"agent_id": "AGT-2"}, status=201)

    with client_with(handler) as caliber:
        caliber.agents.create("AGT-2", experiment_id="7", name="incident-copilot", enabled=False)

    assert bodies[0] == (
        b'{"agent_id":"AGT-2","experiment_id":"7","name":"incident-copilot","enabled":false}'
    )


def test_agent_delete_reports_false_for_an_unexpected_shape() -> None:
    """The server's own contract always sends ``deleted: true`` on success,
    but a client should not crash on a response shape it doesn't recognise."""

    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({"agent_id": "AGT-1"})  # no "deleted" key

    with client_with(handler) as caliber:
        assert caliber.agents.delete("AGT-1") is False


def test_agent_skills_and_experiment_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        if request.url.path.endswith("/skills"):
            return envelope({"skills": [], "missing": ["renamed-skill"]})
        return envelope({"configured_experiment_id": "42", "status": "reachable"})

    with client_with(handler) as caliber:
        skills = caliber.agents.skills("AGT-1")
        experiment = caliber.agents.experiment("AGT-1")

    assert seen == ["GET /agents/AGT-1/skills", "GET /agents/AGT-1/experiment"]
    assert skills["missing"] == ["renamed-skill"]
    assert experiment["status"] == "reachable"


# --- prompts: release lifecycle ------------------------------------------------


def test_prompt_release_lifecycle_methods_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({})

    with client_with(handler) as caliber:
        caliber.prompts.rollback("support-triage")
        caliber.prompts.set_baseline("support-triage", test_run_id="PTR-1")
        caliber.prompts.bind("support-triage", kind="agent", agent_id="AGT-1")
        caliber.prompts.delete("support-triage")

    assert seen == [
        "POST /prompts/support-triage/rollback",
        "POST /prompts/support-triage/baseline",
        "POST /prompts/support-triage/bind",
        "DELETE /prompts/support-triage",
    ]


def test_prompt_rollback_defaults_to_the_prod_alias() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        return envelope({})

    with client_with(handler) as caliber:
        caliber.prompts.rollback("support-triage")

    assert bodies[0] == b'{"alias":"prod"}'


def test_prompt_bind_sends_kind_and_extra_params() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        return envelope({})

    with client_with(handler) as caliber:
        caliber.prompts.bind(
            "support-triage", kind="workflow_node", workflow_id="WF-1", node_id="n1"
        )

    assert bodies[0] == b'{"kind":"workflow_node","workflow_id":"WF-1","node_id":"n1"}'


# --- skills: release lifecycle --------------------------------------------------


def test_skill_release_lifecycle_methods_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_seen_path(request))
        return envelope({})

    with client_with(handler) as caliber:
        caliber.skills.rollback("SKL-1")
        caliber.skills.set_baseline("SKL-1", test_run_id="STR-1")
        caliber.skills.bind("SKL-1", kind="agent", agent_id="AGT-1")
        caliber.skills.calibrate("SKL-1", optimizer_type="meta_prompt")

    assert seen == [
        "POST /skills/SKL-1/rollback",
        "POST /skills/SKL-1/baseline",
        "POST /skills/SKL-1/bind",
        "POST /skills/SKL-1/calibrate",
    ]


def test_skill_calibrate_sends_options_as_the_body() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        return envelope({})

    with client_with(handler) as caliber:
        caliber.skills.calibrate("SKL-1", optimizer_type="meta_prompt", notes="manual run")

    assert bodies[0] == b'{"optimizer_type":"meta_prompt","notes":"manual run"}'
