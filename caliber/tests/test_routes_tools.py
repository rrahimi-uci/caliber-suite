"""Integration tests for the tool registry routes (plan §19.9)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberToolRegistry,
    CaliberToolTestRun,
    CaliberWorkflow,
    CaliberWorkflowVersion,
)
from tests.workflow_helpers import (
    PREFIX,
    create_and_publish,
    deploy_prod,
    make_support_manifest,
    make_tool_payload,
    seed_eval_dataset,
)


def test_list_includes_builtin_tools_on_fresh_registry(client: TestClient) -> None:
    r = client.get(f"{PREFIX}/tools")
    assert r.status_code == 200
    names = {tool["name"] for tool in r.json()["data"]}
    assert {
        "read_text_file",
        "list_folder_files",
        "grep_files",
        "regex_search",
        "grok_parse",
        "sandbox_python",
    } <= names


def test_register_tool(client: TestClient) -> None:
    r = client.post(f"{PREFIX}/tools", json=make_tool_payload("lookup_policy"))
    assert r.status_code == 201
    data = r.json()["data"]
    assert data["tool_id"].startswith("TL-")
    assert data["name"] == "lookup_policy"


def test_register_tool_viewer_forbidden(client: TestClient) -> None:
    r = client.post(
        f"{PREFIX}/tools", json=make_tool_payload("x"), headers={"X-CALIBER-User": "@viewer"}
    )
    assert r.status_code == 403


def test_duplicate_name_version_409(client: TestClient) -> None:
    client.post(f"{PREFIX}/tools", json=make_tool_payload("dup", version="1.0"))
    r = client.post(f"{PREFIX}/tools", json=make_tool_payload("dup", version="1.0"))
    assert r.status_code == 409


def test_get_tool(client: TestClient) -> None:
    tid = client.post(f"{PREFIX}/tools", json=make_tool_payload("gettable")).json()["data"][
        "tool_id"
    ]
    r = client.get(f"{PREFIX}/tools/{tid}")
    assert r.status_code == 200
    assert r.json()["data"]["name"] == "gettable"


def test_list_tool_versions_returns_family_newest_first(client: TestClient) -> None:
    """All versions sharing a tool's name, newest version first."""
    tid1 = client.post(f"{PREFIX}/tools", json=make_tool_payload("fam", version="1.0")).json()[
        "data"
    ]["tool_id"]
    client.post(f"{PREFIX}/tools", json=make_tool_payload("fam", version="2.0"))
    # An unrelated family must not leak in.
    client.post(f"{PREFIX}/tools", json=make_tool_payload("other", version="1.0"))

    r = client.get(f"{PREFIX}/tools/{tid1}/versions")
    assert r.status_code == 200
    rows = r.json()["data"]
    assert [row["version"] for row in rows] == ["2.0", "1.0"]
    assert {row["name"] for row in rows} == {"fam"}


def test_list_tool_versions_404_for_missing_tool(client: TestClient) -> None:
    assert client.get(f"{PREFIX}/tools/TOOL-nope/versions").status_code == 404


def test_list_tool_versions_sorts_naturally_not_lexically(client: TestClient) -> None:
    """Versions order by numeric value, not string order.

    ``version`` is a free-form string column, so a plain ``ORDER BY version``
    puts ``"9"`` ahead of ``"10"`` and ``"1.10"`` ahead of ``"1.9"``. The
    version-aware key must sort newest-first as an operator expects.
    """
    tid = client.post(f"{PREFIX}/tools", json=make_tool_payload("nat", version="9")).json()["data"][
        "tool_id"
    ]
    for version in ("10", "1.9", "1.10", "2.0"):
        assert (
            client.post(
                f"{PREFIX}/tools", json=make_tool_payload("nat", version=version)
            ).status_code
            == 201
        )

    rows = client.get(f"{PREFIX}/tools/{tid}/versions").json()["data"]
    # Natural order, newest first (lexicographic desc would wrongly lead with "9").
    assert [row["version"] for row in rows] == ["10", "9", "2.0", "1.10", "1.9"]


def test_list_tool_versions_does_not_leak_other_scopes(client: TestClient) -> None:
    """A project-scoped tool family must not leak its versions to outside identities."""
    tid = client.post(
        f"{PREFIX}/tools",
        json=make_tool_payload("scoped", version="1.0"),
        headers={"X-CALIBER-Project": "PRJ-1"},
    ).json()["data"]["tool_id"]
    client.post(
        f"{PREFIX}/tools",
        json=make_tool_payload("scoped", version="2.0"),
        headers={"X-CALIBER-Project": "PRJ-1"},
    )

    # A non-admin viewer with no active project cannot see the parent at all. Returning an
    # empty 200 would distinguish "real but hidden family" from a missing tool id.
    resp = client.get(f"{PREFIX}/tools/{tid}/versions", headers={"X-CALIBER-User": "@viewer"})
    assert resp.status_code == 404


def test_get_tool_source_available(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # lookup_policy is a real callable in caliber.workflows.demo_tools.
    tid = client.post(f"{PREFIX}/tools", json=make_tool_payload("lookup_policy")).json()["data"][
        "tool_id"
    ]

    # Import is execution. If the route still reflects in the API process, this fails;
    # the child interpreter does not inherit the monkeypatch and can inspect normally.
    import importlib

    def _parent_import_forbidden(_name: str, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("source inspection imported registered Python in the API process")

    monkeypatch.setattr(importlib, "import_module", _parent_import_forbidden)
    r = client.get(f"{PREFIX}/tools/{tid}/source")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["available"] is True
    assert "def lookup_policy" in data["source"]
    assert data["signature"].startswith("lookup_policy(")
    assert data["error"] is None


def test_get_tool_source_wait_runs_outside_the_async_event_loop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = client.post(f"{PREFIX}/tools", json=make_tool_payload("source_offload")).json()["data"][
        "tool_id"
    ]

    def _blocking_inspection(*_args: object, **_kwargs: object) -> dict[str, Any]:
        # A synchronous sandbox wait called directly by the async route has a running
        # event loop here. The worker-thread path deliberately has none.
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        return {
            "module_path": "caliber.workflows.demo_tools",
            "callable_name": "source_offload",
            "available": False,
            "signature": "source_offload",
            "doc": "",
            "source": "",
            "error": "source unavailable in probe",
        }

    monkeypatch.setattr("caliber.routes.tools._resolve_tool_source", _blocking_inspection)
    response = client.get(f"{PREFIX}/tools/{tid}/source")

    assert response.status_code == 200, response.text
    assert response.json()["data"]["error"] == "source unavailable in probe"


def test_get_tool_source_unavailable_for_missing_callable(client: TestClient) -> None:
    # 'gettable' is not a real callable in demo_tools → reflection fails gracefully.
    tid = client.post(f"{PREFIX}/tools", json=make_tool_payload("gettable")).json()["data"][
        "tool_id"
    ]
    r = client.get(f"{PREFIX}/tools/{tid}/source")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["available"] is False
    assert data["error"]


def test_get_tool_source_404(client: TestClient) -> None:
    assert client.get(f"{PREFIX}/tools/TL-nope/source").status_code == 404


def test_deprecate_tool_sets_timestamp(client: TestClient) -> None:
    tid = client.post(f"{PREFIX}/tools", json=make_tool_payload("dep")).json()["data"]["tool_id"]
    r = client.patch(f"{PREFIX}/tools/{tid}", json={"status": "deprecated"})
    assert r.status_code == 200
    assert r.json()["data"]["deprecated_at"] is not None


def test_archive_unreferenced(client: TestClient) -> None:
    tid = client.post(f"{PREFIX}/tools", json=make_tool_payload("free")).json()["data"]["tool_id"]
    r = client.post(f"{PREFIX}/tools/{tid}/archive")
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "archived"


def test_archive_blocked_by_prod_workflow(client: TestClient, db_session: Session) -> None:
    seed_eval_dataset(db_session)
    manifest = make_support_manifest(
        "tool_block_wf",
        deploy_gates={
            "support_eval": {
                "type": "deploy_gate",
                "dataset_ref": "support_eval",
                "required_for_aliases": ["prod"],
                # Completion, not quality — see test_deploy_gate_evidence.py.
                "thresholds": {"min_completion_rate": 1.0},
            }
        },
    )
    wid, vid = create_and_publish(client, workflow_name="ToolBlock", manifest=manifest)
    deploy_prod(client, wid, vid)
    # lookup_policy is referenced by the prod-deployed workflow.
    tools = client.get(f"{PREFIX}/tools").json()["data"]
    lookup = next(t for t in tools if t["name"] == "lookup_policy")
    r = client.post(f"{PREFIX}/tools/{lookup['tool_id']}/archive")
    assert r.status_code == 409


def test_archive_blocked_by_dev_deployment(client: TestClient) -> None:
    # ext E1: archive is blocked by ANY active deployment, not just prod.
    wid, vid = create_and_publish(client, workflow_name="DevBlock")
    r = client.post(f"{PREFIX}/workflows/{wid}/deployments/dev/promote", json={"version_id": vid})
    assert r.status_code == 200
    tools = client.get(f"{PREFIX}/tools").json()["data"]
    lookup = next(t for t in tools if t["name"] == "lookup_policy")
    r = client.post(f"{PREFIX}/tools/{lookup['tool_id']}/archive")
    assert r.status_code == 409
    assert "dev@" in r.json()["detail"]


def test_tool_usage_lists_referencing_workflows(client: TestClient) -> None:
    wid, _vid = create_and_publish(client, workflow_name="UsageWf")
    tools = client.get(f"{PREFIX}/tools").json()["data"]
    lookup = next(t for t in tools if t["name"] == "lookup_policy")
    r = client.get(f"{PREFIX}/tools/{lookup['tool_id']}/usage")
    assert r.status_code == 200
    usage = r.json()["data"]["usage"]
    assert any(u["workflow_id"] == wid for u in usage)


def test_tool_usage_empty_for_unreferenced_tool(client: TestClient) -> None:
    """A tool no workflow binds to returns empty usage.

    Guards the SQL ``contains`` prefilter added to ``tool_usage``: a tool whose
    name appears in no manifest is correctly filtered out (no false positive),
    and the prefilter returning zero candidate rows still yields a clean 200.
    """
    create_and_publish(client, workflow_name="UnreferencedWf")
    tid = _register_tool(client, "totally_unreferenced_tool")
    r = client.get(f"{PREFIX}/tools/{tid}/usage")
    assert r.status_code == 200
    assert r.json()["data"]["usage"] == []


def test_public_tool_usage_does_not_leak_foreign_private_workflows(
    client: TestClient, db_session: Session
) -> None:
    """A shared tool's usage is still scoped by each referencing workflow.

    Scoping only the parent tool is insufficient: public tools are deliberately visible
    across projects, while the workflow/version ids that reference one may be private.
    Include one owned row as a positive control so an implementation that simply returns
    no usage cannot satisfy the test.
    """
    tool_id = "TL-PUBLIC-USAGE"
    tool_name = "public_usage_probe"
    db_session.add(
        CaliberToolRegistry(
            tool_id=tool_id,
            name=tool_name,
            version="1.0",
            module_path="caliber.workflows.demo_tools",
            callable_name="lookup_policy",
            owner="@system",
            visibility="public",
        )
    )
    for workflow_id, owner, project_id in (
        ("WF-USAGE-MINE", "@operator", "PRJ-MINE"),
        ("WF-USAGE-SECRET", "@secret", "PRJ-SECRET"),
    ):
        db_session.add(
            CaliberWorkflow(
                workflow_id=workflow_id,
                name=workflow_id,
                owner=owner,
                project_id=project_id,
                visibility="project",
            )
        )
        db_session.add(
            CaliberWorkflowVersion(
                version_id=f"WFV-{workflow_id}",
                workflow_id=workflow_id,
                version_number=1,
                status="published",
                manifest=make_support_manifest(
                    workflow_id,
                    tools={
                        tool_name: {
                            "registry_ref": f"tool.{tool_name}.v1",
                            "version_constraint": ">=1.0,<2.0",
                        }
                    },
                ),
                manifest_hash=f"hash-{workflow_id}",
            )
        )
    db_session.commit()

    client.app.state.config = client.app.state.config.model_copy(
        update={"admin_users": "@test", "operator_users": "@operator"}
    )
    response = client.get(
        f"{PREFIX}/tools/{tool_id}/usage",
        headers={"X-CALIBER-User": "@operator", "X-CALIBER-Project": "PRJ-MINE"},
    )

    assert response.status_code == 200, response.text
    usage = response.json()["data"]["usage"]
    assert {item["workflow_id"] for item in usage} == {"WF-USAGE-MINE"}


def test_list_tools_by_status(client: TestClient) -> None:
    client.post(f"{PREFIX}/tools", json=make_tool_payload("active_tool"))
    tid = client.post(f"{PREFIX}/tools", json=make_tool_payload("dep_tool")).json()["data"][
        "tool_id"
    ]
    client.patch(f"{PREFIX}/tools/{tid}", json={"status": "deprecated"})
    r = client.get(f"{PREFIX}/tools?status=all")
    assert r.status_code == 200
    names = {tool["name"] for tool in r.json()["data"]}
    assert {"active_tool", "dep_tool"} <= names
    r = client.get(f"{PREFIX}/tools?status=deprecated")
    assert r.status_code == 200
    assert [tool["name"] for tool in r.json()["data"]] == ["dep_tool"]


def test_builtin_tool_seeding_is_idempotent(client: TestClient) -> None:
    first = client.get(f"{PREFIX}/tools")
    second = client.get(f"{PREFIX}/tools")
    assert first.status_code == 200
    assert second.status_code == 200
    names = [tool["name"] for tool in second.json()["data"]]
    assert names.count("grep_files") == 1
    assert names.count("sandbox_python") == 1


def test_list_tools_invalid_status_400(client: TestClient) -> None:
    r = client.get(f"{PREFIX}/tools?status=invalid")
    assert r.status_code == 400


def test_get_tool_missing_404(client: TestClient) -> None:
    r = client.get(f"{PREFIX}/tools/TL-nonexistent")
    assert r.status_code == 404


def test_update_tool_missing_404(client: TestClient) -> None:
    r = client.patch(f"{PREFIX}/tools/TL-nonexistent", json={"description": "x"})
    assert r.status_code == 404


def test_update_tool_empty_body_400(client: TestClient) -> None:
    tid = client.post(f"{PREFIX}/tools", json=make_tool_payload("empty_update")).json()["data"][
        "tool_id"
    ]
    r = client.patch(f"{PREFIX}/tools/{tid}", json={})
    assert r.status_code == 400


def test_update_tool_no_actual_change(client: TestClient) -> None:
    """When submitted values match current values, return 200 without audit."""
    payload = make_tool_payload("no_change_tool")
    tid = client.post(f"{PREFIX}/tools", json=payload).json()["data"]["tool_id"]
    r = client.patch(f"{PREFIX}/tools/{tid}", json={"description": payload["description"]})
    assert r.status_code == 200


def test_update_tool_metadata_fields(client: TestClient) -> None:
    tid = client.post(f"{PREFIX}/tools", json=make_tool_payload("metadata_tool")).json()["data"][
        "tool_id"
    ]
    r = client.patch(
        f"{PREFIX}/tools/{tid}",
        json={
            "description": "Updated metadata",
            "side_effect_level": "write",
            "requires_approval": True,
            "allow_in_preview": False,
            "owner": "@ops",
            "status": "deprecated",
            "successor_tool_id": "TL-next",
        },
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["description"] == "Updated metadata"
    assert data["side_effect_level"] == "write"
    assert data["requires_approval"] is True
    assert data["allow_in_preview"] is False
    assert data["owner"] == "@ops"
    assert data["status"] == "deprecated"
    assert data["successor_tool_id"] == "TL-next"
    assert data["deprecated_at"] is not None


def test_test_run_read_tool_executes_when_preview_allowed(client: TestClient) -> None:
    tid = client.post(
        f"{PREFIX}/tools",
        json=make_tool_payload(
            "run_lookup_policy",
            callable_name="lookup_policy",
            allow_in_preview=True,
        ),
    ).json()["data"]["tool_id"]
    r = client.post(f"{PREFIX}/tools/{tid}/test-run", json={"input": {"query": "refund"}})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["mocked"] is False
    assert data["error"] is None
    assert data["output"]["query"] == "refund"
    assert "policy" in data["output"]


def test_test_run_side_effect_tool_is_mocked_without_importing_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = client.post(
        f"{PREFIX}/tools",
        json=make_tool_payload(
            "run_escalate",
            callable_name="escalate",
            side_effect_level="external_action",
            allow_in_preview=True,
        ),
    ).json()["data"]["tool_id"]

    # Module top-level code is execution. The old path imported first and decided to mock
    # second, so "always mocked" still gave unsafe tools a control-plane effect surface.
    import importlib

    def _import_must_not_happen(_name: str, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("a mocked tool was imported in the API process")

    monkeypatch.setattr(importlib, "import_module", _import_must_not_happen)
    r = client.post(f"{PREFIX}/tools/{tid}/test-run", json={"input": {"reason": "help"}})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["mocked"] is True
    assert data["isolation"] == "mocked"
    assert data["output"]["_preview_mock"] is True


def test_test_and_calibration_waits_run_outside_the_async_event_loop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _calibratable_tool(client)
    calls: list[str] = []

    def _blocking_invocation(
        _tool: object,
        tool_input: dict[str, Any],
        *,
        config: object | None = None,
    ) -> dict[str, Any]:
        del config
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        calls.append(str(tool_input.get("query", "")))
        return {
            "output": {"query": tool_input.get("query")},
            "mocked": False,
            "duration_ms": 0.1,
            "error": None,
            "isolation": "subprocess",
        }

    monkeypatch.setattr(
        "caliber.routes.tools._invoke_tool_under_preview_policy", _blocking_invocation
    )

    tested = client.post(f"{PREFIX}/tools/{tid}/test-run", json={"input": {"query": "one"}})
    assert tested.status_code == 200, tested.text
    saved = client.put(
        f"{PREFIX}/tools/{tid}/test-cases",
        json={"test_cases": [{"name": "two", "input": {"query": "two"}}]},
    )
    assert saved.status_code == 200, saved.text
    calibrated = client.post(f"{PREFIX}/tools/{tid}/calibrate")

    assert calibrated.status_code == 200, calibrated.text
    assert calls == ["one", "two"]


def test_test_run_requires_object_input(client: TestClient) -> None:
    tid = client.post(f"{PREFIX}/tools", json=make_tool_payload("bad_run_input")).json()["data"][
        "tool_id"
    ]
    r = client.post(f"{PREFIX}/tools/{tid}/test-run", json={"input": []})
    assert r.status_code == 400


def test_archive_tool_missing_404(client: TestClient) -> None:
    r = client.post(f"{PREFIX}/tools/TL-nonexistent/archive")
    assert r.status_code == 404


def test_tool_usage_missing_404(client: TestClient) -> None:
    r = client.get(f"{PREFIX}/tools/TL-nonexistent/usage")
    assert r.status_code == 404


def test_update_tool_viewer_forbidden(client: TestClient) -> None:
    tid = client.post(f"{PREFIX}/tools", json=make_tool_payload("rbac_tool")).json()["data"][
        "tool_id"
    ]
    r = client.patch(
        f"{PREFIX}/tools/{tid}",
        json={"description": "new"},
        headers={"X-CALIBER-User": "@viewer"},
    )
    assert r.status_code == 403


# ── Calibration ────────────────────────────────────────────────────────────


def _calibratable_tool(client: TestClient) -> str:
    """Register a read tool that runs live in preview (lookup_policy)."""
    return client.post(
        f"{PREFIX}/tools",
        json=make_tool_payload(
            "calib_lookup",
            callable_name="lookup_policy",
            allow_in_preview=True,
        ),
    ).json()["data"]["tool_id"]


def test_save_tool_test_cases(client: TestClient) -> None:
    tid = _calibratable_tool(client)
    r = client.put(
        f"{PREFIX}/tools/{tid}/test-cases",
        json={
            "test_cases": [
                {"name": "basic", "input": {"query": "refund"}},
                {
                    "name": "contains",
                    "input": {"query": "refund"},
                    "assertion": {"type": "output_contains", "value": "refund"},
                },
            ]
        },
    )
    assert r.status_code == 200
    assert len(r.json()["data"]["test_cases"]) == 2
    # Persisted on the tool row.
    tool = client.get(f"{PREFIX}/tools/{tid}").json()["data"]
    assert len(tool["test_cases"]) == 2


def test_save_tool_test_cases_missing_value_400(client: TestClient) -> None:
    tid = _calibratable_tool(client)
    r = client.put(
        f"{PREFIX}/tools/{tid}/test-cases",
        json={"test_cases": [{"name": "x", "assertion": {"type": "equals"}}]},
    )
    # Pydantic ValidationError is rendered as a structured 400 in this codebase.
    assert r.status_code == 400


def test_save_tool_test_cases_viewer_forbidden(client: TestClient) -> None:
    tid = _calibratable_tool(client)
    r = client.put(
        f"{PREFIX}/tools/{tid}/test-cases",
        json={"test_cases": []},
        headers={"X-CALIBER-User": "@viewer"},
    )
    assert r.status_code == 403


def test_calibrate_tool_scores_and_persists(client: TestClient) -> None:
    tid = _calibratable_tool(client)
    client.put(
        f"{PREFIX}/tools/{tid}/test-cases",
        json={
            "test_cases": [
                {"name": "no_error", "input": {"query": "refund"}},
                {
                    "name": "contains_pass",
                    "input": {"query": "refund"},
                    "assertion": {"type": "output_contains", "value": "refund"},
                },
                {
                    "name": "contains_fail",
                    "input": {"query": "refund"},
                    "assertion": {"type": "output_contains", "value": "definitely-not-there"},
                },
            ]
        },
    )
    r = client.post(f"{PREFIX}/tools/{tid}/calibrate")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["total"] == 3
    assert data["passed"] == 2
    assert data["pass_rate"] == round(2 / 3, 4)
    by_name = {c["name"]: c for c in data["cases"]}
    assert by_name["no_error"]["passed"] is True
    assert by_name["contains_pass"]["passed"] is True
    assert by_name["contains_fail"]["passed"] is False
    # last_calibration persisted on the tool row.
    tool = client.get(f"{PREFIX}/tools/{tid}").json()["data"]
    assert tool["last_calibration"]["passed"] == 2
    assert tool["last_calibration"]["ran_at"]


def test_calibrate_tool_equals_assertion(client: TestClient) -> None:
    tid = _calibratable_tool(client)
    # lookup_policy returns {"policy": "...", "query": <query>} — equals must
    # match the full stringified output, so a partial value fails.
    client.put(
        f"{PREFIX}/tools/{tid}/test-cases",
        json={
            "test_cases": [
                {
                    "name": "equals_fail",
                    "input": {"query": "refund"},
                    "assertion": {"type": "equals", "value": "refund"},
                },
            ]
        },
    )
    r = client.post(f"{PREFIX}/tools/{tid}/calibrate")
    assert r.status_code == 200
    assert r.json()["data"]["passed"] == 0


def test_calibrate_tool_refuses_to_persist_against_changed_cases(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _calibratable_tool(client)
    original = {"name": "original", "input": {"query": "one"}}
    changed = {"name": "changed", "input": {"query": "two"}}
    saved = client.put(f"{PREFIX}/tools/{tid}/test-cases", json={"test_cases": [original]})
    assert saved.status_code == 200, saved.text

    def _score_after_concurrent_change(
        _tool: object,
        _cases: list[dict[str, Any]],
        *,
        config: object | None,
    ) -> list[dict[str, Any]]:
        del config
        # The scoring phase runs after the route's read session has closed. Simulate a
        # second operator replacing fixtures before this calibration can persist.
        with client.app.state.session_factory() as session:
            row = session.get(CaliberToolRegistry, tid)
            assert row is not None
            row.test_cases = [changed]
            session.commit()
        return [
            {
                "name": "original",
                "passed": True,
                "output": {"query": "one"},
                "error": None,
                "duration_ms": 0.1,
            }
        ]

    monkeypatch.setattr("caliber.routes.tools._score_tool_cases", _score_after_concurrent_change)
    response = client.post(f"{PREFIX}/tools/{tid}/calibrate")

    assert response.status_code == 409, response.text
    assert "test cases changed" in response.json()["detail"]
    tool = client.get(f"{PREFIX}/tools/{tid}").json()["data"]
    assert tool["test_cases"] == [changed]
    assert tool["last_calibration"] is None


def test_calibrate_tool_no_cases_400(client: TestClient) -> None:
    tid = _calibratable_tool(client)
    r = client.post(f"{PREFIX}/tools/{tid}/calibrate")
    assert r.status_code == 400
    assert "no saved test cases" in r.json()["detail"]


def test_calibrate_tool_missing_404(client: TestClient) -> None:
    assert client.post(f"{PREFIX}/tools/TL-nope/calibrate").status_code == 404


def test_calibrate_tool_viewer_forbidden(client: TestClient) -> None:
    tid = _calibratable_tool(client)
    r = client.post(f"{PREFIX}/tools/{tid}/calibrate", headers={"X-CALIBER-User": "@viewer"})
    assert r.status_code == 403


# ── Durable tool-test runs (history) ─────────────────────────────────────────


def _register_tool(client: TestClient, name: str = "ttr_tool") -> str:
    """Register a plain tool and return its id."""
    return client.post(f"{PREFIX}/tools", json=make_tool_payload(name)).json()["data"]["tool_id"]


def _tool_run_body(tool_id: str, **overrides: Any) -> dict[str, Any]:
    """A minimal POST body for ``/tools/test-runs`` with two pass + one fail."""
    body: dict[str, Any] = {
        "tool_id": tool_id,
        "kind": "suite",
        "tool_version": "1.0",
        "results": [
            {
                "name": "case-1",
                "input": {"query": "refund"},
                "output": {"policy": "p"},
                "verdict": "pass",
                "score": 1.0,
                "duration_ms": 1.2,
                "reasoning": "ok",
            },
            {
                "name": "case-2",
                "input": {"query": "hello"},
                "output": {"policy": "q"},
                "verdict": "pass",
                "score": 0.8,
                "reasoning": "ok",
            },
            {
                "name": "case-3",
                "input": {"query": "boom"},
                "output": None,
                "error": "ValueError: boom",
                "verdict": "fail",
                "score": 0.0,
                "reasoning": "errored",
            },
        ],
    }
    body.update(overrides)
    return body


def test_create_list_and_get_tool_test_run_roundtrip(
    client: TestClient,
    db_session: Session,
) -> None:
    """POST a run → list shows summary (no results) → detail returns per-case data."""
    tid = _register_tool(client)
    create = client.post(f"{PREFIX}/tools/test-runs", json=_tool_run_body(tid))
    assert create.status_code == 201
    saved = create.json()["data"]
    test_run_id = saved["test_run_id"]
    assert test_run_id.startswith("TTR-")
    # Server-recomputed aggregates (2 pass + 1 fail, mean of 1.0/0.8/0.0 = 0.6).
    assert saved["test_set_size"] == 3
    assert saved["passed_count"] == 2
    assert saved["failed_count"] == 1
    assert saved["partial_count"] == 0
    assert saved["overall_score"] == 0.6
    assert saved["kind"] == "suite"
    assert saved["tool_version"] == "1.0"
    # The summary must NOT carry the heavy per-case array.
    assert "results" not in saved
    # The default ``client`` fixture authenticates as ``@test``.
    assert saved["created_by"] == "@test"

    # The row was persisted with the per-case payload.
    row = db_session.get(CaliberToolTestRun, test_run_id)
    assert row is not None
    assert len(row.results) == 3
    assert row.completed_at is not None

    # List returns the summary, newest-first, without ``results``.
    listing = client.get(f"{PREFIX}/tools/test-runs", params={"tool_id": tid})
    assert listing.status_code == 200
    rows = listing.json()["data"]
    assert len(rows) == 1
    assert rows[0]["test_run_id"] == test_run_id
    assert "results" not in rows[0]

    # Detail returns the full per-case array.
    detail = client.get(f"{PREFIX}/tools/test-runs/{test_run_id}")
    assert detail.status_code == 200
    detail_data = detail.json()["data"]
    assert len(detail_data["results"]) == 3
    assert detail_data["results"][0]["name"] == "case-1"
    assert detail_data["results"][2]["verdict"] == "fail"
    assert detail_data["results"][2]["error"] == "ValueError: boom"


def test_foreign_tool_and_test_run_families_are_scoped(client: TestClient) -> None:
    """Every child route must enforce the visibility used by the tool list.

    Before the shared parent lookup, a foreign operator could not list this tool but could
    fetch its source, execute it, replace its fixtures, calibrate it, inspect its workspace
    and run history, or pin a baseline by supplying the id directly.
    """
    created = client.post(
        f"{PREFIX}/tools",
        json=make_tool_payload("foreign_tool", allow_in_preview=True),
        headers={"X-CALIBER-Project": "PRJ-SECRET"},
    )
    assert created.status_code == 201, created.text
    tool_id = created.json()["data"]["tool_id"]
    run = client.post(f"{PREFIX}/tools/test-runs", json=_tool_run_body(tool_id))
    assert run.status_code == 201, run.text
    test_run_id = run.json()["data"]["test_run_id"]

    client.app.state.config = client.app.state.config.model_copy(
        update={"admin_users": "@test", "operator_users": "@operator"}
    )
    foreign = {
        "X-CALIBER-User": "@operator",
        "X-CALIBER-Project": "PRJ-OTHER",
    }

    listed = client.get(f"{PREFIX}/tools", headers=foreign)
    assert listed.status_code == 200
    assert tool_id not in {item["tool_id"] for item in listed.json()["data"]}

    reads = [
        f"{PREFIX}/tools/{tool_id}",
        f"{PREFIX}/tools/{tool_id}/versions",
        f"{PREFIX}/tools/{tool_id}/source",
        f"{PREFIX}/tools/{tool_id}/usage",
        f"{PREFIX}/tools/{tool_id}/workspace",
    ]
    for path in reads:
        response = client.get(path, headers=foreign)
        assert response.status_code == 404, f"GET {path} -> {response.text}"

    mutations = [
        ("post", f"{PREFIX}/tools/{tool_id}/test-run", {"input": {"query": "refund"}}),
        ("put", f"{PREFIX}/tools/{tool_id}/test-cases", {"test_cases": []}),
        ("post", f"{PREFIX}/tools/{tool_id}/calibrate", None),
        (
            "post",
            f"{PREFIX}/tools/{tool_id}/baseline",
            {"test_run_id": test_run_id},
        ),
    ]
    for method, path, body in mutations:
        response = getattr(client, method)(path, json=body, headers=foreign)
        assert response.status_code == 404, f"{method.upper()} {path} -> {response.text}"

    create_run = client.post(
        f"{PREFIX}/tools/test-runs",
        json=_tool_run_body(tool_id),
        headers=foreign,
    )
    assert create_run.status_code == 404, create_run.text

    filtered_runs = client.get(
        f"{PREFIX}/tools/test-runs", params={"tool_id": tool_id}, headers=foreign
    )
    assert filtered_runs.status_code == 404, filtered_runs.text
    all_runs = client.get(f"{PREFIX}/tools/test-runs", headers=foreign)
    assert all_runs.status_code == 200
    assert test_run_id not in {item["test_run_id"] for item in all_runs.json()["data"]}
    detail = client.get(f"{PREFIX}/tools/test-runs/{test_run_id}", headers=foreign)
    assert detail.status_code == 404, detail.text
    assert tool_id not in detail.text


def test_create_tool_test_run_recomputes_and_ignores_client_aggregates(
    client: TestClient,
) -> None:
    """The server computes counts/score from ``results`` and rejects stray aggregates."""
    tid = _register_tool(client, "ttr_recompute")
    # Stray client aggregate is rejected outright (schema forbids extras).
    rejected = client.post(
        f"{PREFIX}/tools/test-runs",
        json=_tool_run_body(tid, passed_count=999, overall_score=0.99),
    )
    assert rejected.status_code == 400

    # A run with a partial verdict; assert the server-side recompute.
    body = _tool_run_body(
        tid,
        results=[
            {"name": "a", "input": {}, "verdict": "partial", "score": 0.5},
            {"name": "b", "input": {}, "verdict": "pass", "score": 1.0},
        ],
    )
    saved = client.post(f"{PREFIX}/tools/test-runs", json=body).json()["data"]
    assert saved["test_set_size"] == 2
    assert saved["passed_count"] == 1
    assert saved["partial_count"] == 1
    assert saved["failed_count"] == 0
    assert saved["overall_score"] == 0.75


def test_create_tool_test_run_rejects_empty_results(client: TestClient) -> None:
    tid = _register_tool(client, "ttr_empty")
    r = client.post(f"{PREFIX}/tools/test-runs", json=_tool_run_body(tid, results=[]))
    assert r.status_code == 400


def test_create_tool_test_run_rejects_invalid_verdict_and_score(client: TestClient) -> None:
    tid = _register_tool(client, "ttr_bad")
    bad_verdict = client.post(
        f"{PREFIX}/tools/test-runs",
        json=_tool_run_body(
            tid, results=[{"name": "x", "input": {}, "verdict": "maybe", "score": 1.0}]
        ),
    )
    assert bad_verdict.status_code == 400
    bad_score = client.post(
        f"{PREFIX}/tools/test-runs",
        json=_tool_run_body(
            tid, results=[{"name": "x", "input": {}, "verdict": "pass", "score": 1.5}]
        ),
    )
    assert bad_score.status_code == 400


def test_create_tool_test_run_unknown_tool_404(client: TestClient) -> None:
    r = client.post(f"{PREFIX}/tools/test-runs", json=_tool_run_body("TL-nonexistent"))
    assert r.status_code == 404


def test_create_tool_test_run_requires_operator_scope(client: TestClient) -> None:
    tid = _register_tool(client, "ttr_rbac")
    r = client.post(
        f"{PREFIX}/tools/test-runs",
        json=_tool_run_body(tid),
        headers={"X-CALIBER-User": "@viewer"},
    )
    assert r.status_code == 403


def test_get_tool_test_run_unknown_id_404(client: TestClient) -> None:
    assert client.get(f"{PREFIX}/tools/test-runs/TTR-nope").status_code == 404


def test_list_tool_test_runs_filter_kind_order_and_limit(client: TestClient) -> None:
    tid = _register_tool(client, "ttr_filter")
    other = _register_tool(client, "ttr_other")
    # Two suite runs for tid, one sandbox run for tid, one run for `other`.
    client.post(f"{PREFIX}/tools/test-runs", json=_tool_run_body(tid, kind="suite"))
    client.post(f"{PREFIX}/tools/test-runs", json=_tool_run_body(tid, kind="suite"))
    sandbox = client.post(
        f"{PREFIX}/tools/test-runs", json=_tool_run_body(tid, kind="sandbox")
    ).json()["data"]
    client.post(f"{PREFIX}/tools/test-runs", json=_tool_run_body(other, kind="suite"))

    # tool_id filter scopes to one tool.
    tid_rows = client.get(f"{PREFIX}/tools/test-runs", params={"tool_id": tid}).json()["data"]
    assert len(tid_rows) == 3
    assert {r["tool_id"] for r in tid_rows} == {tid}
    # Newest-first ordering: the sandbox run was created last.
    assert tid_rows[0]["test_run_id"] == sandbox["test_run_id"]

    # kind filter.
    sandbox_rows = client.get(
        f"{PREFIX}/tools/test-runs", params={"tool_id": tid, "kind": "sandbox"}
    ).json()["data"]
    assert len(sandbox_rows) == 1
    assert sandbox_rows[0]["kind"] == "sandbox"

    # limit caps the page.
    limited = client.get(f"{PREFIX}/tools/test-runs", params={"tool_id": tid, "limit": 1}).json()[
        "data"
    ]
    assert len(limited) == 1

    # Over-cap limit is clamped (no error) and bad limit is a 400.
    assert client.get(f"{PREFIX}/tools/test-runs", params={"limit": 9999}).status_code == 200
    assert client.get(f"{PREFIX}/tools/test-runs", params={"limit": "abc"}).status_code == 400


# ── Tool workspace + baseline ────────────────────────────────────────────────


def test_tool_workspace_lifecycle_transitions(
    client: TestClient,
    db_session: Session,
) -> None:
    """Draft → Has fixtures → Tested → Hardened → Published as signals accrue."""
    # A read tool that runs live in preview so calibrate produces a real score.
    tid = client.post(
        f"{PREFIX}/tools",
        json=make_tool_payload(
            "ws_lifecycle", callable_name="lookup_policy", allow_in_preview=True
        ),
    ).json()["data"]["tool_id"]

    # Draft: fresh registered tool (active, but no runs/fixtures/calibration).
    # Demote to "deprecated" so an active tool with no run doesn't read as a
    # higher pill — Draft must be observable on its own.
    client.patch(f"{PREFIX}/tools/{tid}", json={"status": "deprecated"})
    ws = client.get(f"{PREFIX}/tools/{tid}/workspace").json()["data"]
    assert ws["lifecycle"] == "Draft"
    assert ws["has_fixtures"] is False
    assert ws["last_run"] is None
    assert ws["version"] == "1.0"
    assert ws["side_effect_level"] == "read"

    # Has fixtures: save test cases.
    client.put(
        f"{PREFIX}/tools/{tid}/test-cases",
        json={"test_cases": [{"name": "c", "input": {"query": "refund"}}]},
    )
    ws = client.get(f"{PREFIX}/tools/{tid}/workspace").json()["data"]
    assert ws["lifecycle"] == "Has fixtures"
    assert ws["has_fixtures"] is True

    # Tested: a durable run exists (tool still deprecated, so not Published).
    run = client.post(f"{PREFIX}/tools/test-runs", json=_tool_run_body(tid)).json()["data"]
    ws = client.get(f"{PREFIX}/tools/{tid}/workspace").json()["data"]
    assert ws["lifecycle"] == "Tested"
    assert ws["last_run"] is not None
    assert ws["last_run"]["test_run_id"] == run["test_run_id"]

    # Hardened: a calibration result is present (outranks Tested).
    client.post(f"{PREFIX}/tools/{tid}/calibrate")
    ws = client.get(f"{PREFIX}/tools/{tid}/workspace").json()["data"]
    assert ws["lifecycle"] == "Hardened"
    assert ws["last_calibration_score"] is not None

    # Published: re-activate the tool — active + ≥1 run wins over Hardened.
    client.patch(f"{PREFIX}/tools/{tid}", json={"status": "active"})
    ws = client.get(f"{PREFIX}/tools/{tid}/workspace").json()["data"]
    assert ws["lifecycle"] == "Published"
    assert ws["status"] == "active"


def test_tool_workspace_missing_404(client: TestClient) -> None:
    assert client.get(f"{PREFIX}/tools/TL-nope/workspace").status_code == 404


def test_tool_set_baseline_reflected_in_workspace(
    client: TestClient,
    db_session: Session,
) -> None:
    """Pinning a run as baseline records it and the workspace surfaces it."""
    tid = _register_tool(client, "ws_baseline")
    run = client.post(f"{PREFIX}/tools/test-runs", json=_tool_run_body(tid)).json()["data"]
    test_run_id = run["test_run_id"]

    # No baseline yet.
    ws = client.get(f"{PREFIX}/tools/{tid}/workspace").json()["data"]
    assert ws["baseline_run_id"] is None
    assert ws["baseline_run"] is None

    set_baseline = client.post(f"{PREFIX}/tools/{tid}/baseline", json={"test_run_id": test_run_id})
    assert set_baseline.status_code == 200
    assert set_baseline.json()["data"]["baseline_run_id"] == test_run_id

    # Persisted on the tool row.
    db_session.expire_all()
    tool = db_session.get(CaliberToolRegistry, tid)
    assert tool is not None
    assert tool.baseline_run_id == test_run_id

    # The workspace reflects the baseline plus a cheap summary.
    ws_after = client.get(f"{PREFIX}/tools/{tid}/workspace").json()["data"]
    assert ws_after["baseline_run_id"] == test_run_id
    assert ws_after["baseline_run"] is not None
    assert ws_after["baseline_run"]["test_run_id"] == test_run_id
    assert ws_after["baseline_run"]["test_set_size"] == 3
    assert ws_after["baseline_run"]["passed_count"] == 2


def test_tool_set_baseline_wrong_tool_returns_400(client: TestClient) -> None:
    """A run that belongs to a different tool cannot be this tool's baseline (400)."""
    owner = _register_tool(client, "ws_owner")
    other = _register_tool(client, "ws_other")
    run = client.post(f"{PREFIX}/tools/test-runs", json=_tool_run_body(owner)).json()["data"]
    r = client.post(f"{PREFIX}/tools/{other}/baseline", json={"test_run_id": run["test_run_id"]})
    assert r.status_code == 400


def test_tool_set_baseline_does_not_reveal_a_hidden_run(
    client: TestClient, db_session: Session
) -> None:
    hidden_tool = client.post(
        f"{PREFIX}/tools",
        json=make_tool_payload("hidden_baseline_tool"),
        headers={"X-CALIBER-Project": "PRJ-SECRET"},
    ).json()["data"]["tool_id"]
    hidden_run = client.post(f"{PREFIX}/tools/test-runs", json=_tool_run_body(hidden_tool)).json()[
        "data"
    ]["test_run_id"]

    visible_tool = "TL-VISIBLE-BASELINE"
    db_session.add(
        CaliberToolRegistry(
            tool_id=visible_tool,
            name="visible_baseline_tool",
            version="1.0",
            module_path="caliber.workflows.demo_tools",
            callable_name="lookup_policy",
            owner="@system",
            visibility="public",
        )
    )
    db_session.commit()

    client.app.state.config = client.app.state.config.model_copy(
        update={"admin_users": "@test", "operator_users": "@operator"}
    )
    operator = {"X-CALIBER-User": "@operator", "X-CALIBER-Project": "PRJ-OTHER"}

    hidden = client.post(
        f"{PREFIX}/tools/{visible_tool}/baseline",
        json={"test_run_id": hidden_run},
        headers=operator,
    )
    missing = client.post(
        f"{PREFIX}/tools/{visible_tool}/baseline",
        json={"test_run_id": "TTR-DOES-NOT-EXIST"},
        headers=operator,
    )

    assert hidden.status_code == missing.status_code == 404
    assert hidden.text.replace(hidden_run, "X") == missing.text.replace("TTR-DOES-NOT-EXIST", "X")


def test_tool_set_baseline_missing_run_returns_404(client: TestClient) -> None:
    tid = _register_tool(client, "ws_missing_run")
    r = client.post(f"{PREFIX}/tools/{tid}/baseline", json={"test_run_id": "TTR-nope"})
    assert r.status_code == 404


def test_tool_set_baseline_missing_tool_returns_404(client: TestClient) -> None:
    r = client.post(f"{PREFIX}/tools/TL-nope/baseline", json={"test_run_id": "TTR-x"})
    assert r.status_code == 404


def test_tool_set_baseline_requires_operator_scope(client: TestClient) -> None:
    tid = _register_tool(client, "ws_baseline_rbac")
    run = client.post(f"{PREFIX}/tools/test-runs", json=_tool_run_body(tid)).json()["data"]
    r = client.post(
        f"{PREFIX}/tools/{tid}/baseline",
        json={"test_run_id": run["test_run_id"]},
        headers={"X-CALIBER-User": "@viewer"},
    )
    assert r.status_code == 403
