"""Contract tests for ``GET /caliber/capabilities``."""

from __future__ import annotations

from starlette.testclient import TestClient

PREFIX = "/ajax-api/2.0/mlflow/caliber"


def test_capabilities_default_contract(client) -> None:
    response = client.get(f"{PREFIX}/capabilities")
    assert response.status_code == 200
    data = response.json()["data"]
    runs = data["workflow_runs"]

    assert runs["queue_enabled"] is False
    assert runs["supports_async_submit"] is False
    assert runs["supports_cancel"] is False
    assert runs["supports_retry"] is False
    assert runs["supports_resume"] is False
    assert runs["runtime_approvals_enabled"] is False
    assert runs["checkpointing_enabled"] is False
    assert runs["event_backend"] == "in_process"
    assert runs["approval_readiness"]["status"] == "configuration_required"
    assert runs["approval_readiness"]["allow_self_approval"] is True
    assert "required_role" in runs["approval_readiness"]["decision_scope"]
    assert len(runs["approval_readiness"]["blockers"]) == 3
    assert data["sync_workflow_version_run"] is True
    assert set(data["artifact_families"]) == {
        "prompt",
        "workflow",
        "knowledge_base",
        "skill",
        "tool",
        "test_set",
        "mcp_server",
        "judge",
        "agent",
    }
    assert data["artifact_families"]["prompt"]["rollbackable"] is True
    assert data["artifact_families"]["tool"]["rollbackable"] is False
    assert data["openapi_integrations"] == {
        "enabled": True,
        "stability": "beta",
        "import_sources": ["inline_text"],
        "publication_backend": "tool_registry_openapi_http",
        "runtime_backend": "python_callable_and_openapi_http",
    }


def test_each_artifact_family_declares_the_complete_contract(client) -> None:
    """Every family declares every field, and the endpoint hides none of them.

    The expected set is read from the registry rather than restated here. A
    literal copy would have to be edited alongside the registry, which makes the
    test a second description of the contract instead of a check on it -- and a
    field dropped from both would pass.
    """
    from caliber.artifact_capabilities import CAPABILITY_FIELDS

    families = client.get(f"{PREFIX}/capabilities").json()["data"]["artifact_families"]
    for family, contract in families.items():
        assert set(contract) == set(CAPABILITY_FIELDS), family


def test_rollback_mechanisms_are_disclosed_and_all_differ(client) -> None:
    """The four rollbackable families mean four different things by it.

    This is the operator trap the paper names: the same version-history panel is
    mounted for several families, and a client shown only ``rollbackable: true``
    would infer one guarantee from four distinct semantics. The endpoint has to
    carry the mechanism for the distinction to survive the trip.
    """
    families = client.get(f"{PREFIX}/capabilities").json()["data"]["artifact_families"]

    mechanisms = {
        name: contract["rollback"]
        for name, contract in families.items()
        if contract["rollbackable"]
    }

    assert mechanisms == {
        "prompt": "alias_restore",
        "workflow": "checkpoint_stack_pop",
        "knowledge_base": "derived_from_activation_history",
        "skill": "snapshot_restored_as_new_version",
    }
    assert len(set(mechanisms.values())) == len(mechanisms), (
        "two families share a rollback mechanism; if that is now true the paper's "
        "claim that the five semantics all differ needs revising too"
    )
    assert all(
        contract["rollback"] == "none"
        for contract in families.values()
        if not contract["rollbackable"]
    )


def test_only_runtime_assets_are_deployable(client) -> None:
    """Six are authored runtime assets; the other three are not things one deploys."""
    families = client.get(f"{PREFIX}/capabilities").json()["data"]["artifact_families"]

    kinds = {name: contract["kind"] for name, contract in families.items()}
    assert kinds["test_set"] == "evidence_asset"
    assert kinds["judge"] == "scoring_asset"
    assert kinds["agent"] == "anchor_record"
    assert sum(kind == "runtime_asset" for kind in kinds.values()) == 6

    for name, contract in families.items():
        if contract["kind"] != "runtime_asset":
            assert contract["promotable"] is False, name
            assert contract["rollback"] == "none", name


def test_capabilities_reflect_flag_overrides(client) -> None:
    client.app.state.config = client.app.state.config.model_copy(
        update={
            "workflow_run_queue_enabled": True,
            "workflow_run_runtime_approvals_enabled": True,
            "workflow_run_checkpointing_enabled": True,
            "workflow_run_event_backend": "database",
        }
    )
    response = client.get(f"{PREFIX}/capabilities")
    assert response.status_code == 200
    runs = response.json()["data"]["workflow_runs"]

    assert runs["queue_enabled"] is True
    assert runs["supports_async_submit"] is True
    assert runs["supports_cancel"] is True
    assert runs["supports_retry"] is True
    assert runs["supports_resume"] is True
    assert runs["runtime_approvals_enabled"] is True
    assert runs["checkpointing_enabled"] is True
    assert runs["event_backend"] == "database"
    assert runs["approval_readiness"]["status"] == "ready"
    assert runs["approval_readiness"]["blockers"] == []
    assert runs["approval_readiness"]["settings_path"] == "/settings"


def test_capabilities_keep_resume_disabled_without_checkpointing(client) -> None:
    client.app.state.config = client.app.state.config.model_copy(
        update={
            "workflow_run_queue_enabled": True,
            "workflow_run_runtime_approvals_enabled": True,
            "workflow_run_checkpointing_enabled": False,
            "workflow_run_event_backend": "database",
        }
    )
    response = client.get(f"{PREFIX}/capabilities")
    assert response.status_code == 200
    runs = response.json()["data"]["workflow_runs"]

    assert runs["queue_enabled"] is True
    assert runs["supports_async_submit"] is True
    assert runs["supports_cancel"] is True
    assert runs["supports_retry"] is True
    assert runs["supports_resume"] is False
    assert runs["runtime_approvals_enabled"] is True
    assert runs["checkpointing_enabled"] is False
    assert runs["event_backend"] == "database"


# ---------------------------------------------------------------------------
# Honest disclosure of the executable envelope
# ---------------------------------------------------------------------------


def test_capabilities_endpoint_reports_what_aria_can_actually_execute(
    client: TestClient,
) -> None:
    """The drafting UI is broad; the executable registry is seven built-ins.

    Nothing surfaced that number, so the envelope a user infers from the panel
    was wider than the one the product implements.
    """
    from caliber.assistant.capabilities import registered_capabilities
    from caliber.routes.aria_plans import CAPABILITIES_PATH

    resp = client.get(CAPABILITIES_PATH)

    assert resp.status_code == 200
    body = resp.json()["data"] if "data" in resp.json() else resp.json()
    assert body["count"] == len(registered_capabilities())
    keys = {item["key"] for item in body["capabilities"]}
    assert keys == {capability.key for capability in registered_capabilities()}


def test_each_capability_carries_the_scopes_needed_to_invoke_it(
    client: TestClient,
) -> None:
    """Disclosure without the authorization detail would invite the same wrong inference."""
    from caliber.routes.aria_plans import CAPABILITIES_PATH

    body = client.get(CAPABILITIES_PATH).json()
    body = body["data"] if "data" in body else body

    for item in body["capabilities"]:
        assert "required_scopes" in item
        assert "tier" in item
        assert "input_schema" in item


def test_capabilities_requires_authentication(client: TestClient) -> None:
    from caliber.routes.aria_plans import CAPABILITIES_PATH

    resp = client.get(CAPABILITIES_PATH, headers={"X-CALIBER-User": ""})
    assert resp.status_code in {401, 403}


def test_capabilities_reports_which_code_may_write_production_artifacts(
    client: TestClient,
) -> None:
    """An operator needs the provenance, not just the names.

    Optimizers author the prompt that gets promoted. A list that showed only
    names would answer "what can I pick" while hiding "whose code runs", which
    is the question that matters for a third-party plugin.
    """
    payload = client.get(f"{PREFIX}/capabilities").json()["data"]
    extensibility = payload["extensibility"]

    names = {entry["name"]: entry for entry in extensibility["optimizers"]}
    assert {"MetaPrompt", "SkillMetaPrompt", "GEPA"} <= set(names)
    assert all(entry["source"] == "builtin" for entry in names.values())

    # The scoping the dispatch chain could not express, now visible to the UI.
    assert names["SkillMetaPrompt"]["artifact_types"] == ["skill"]
    assert names["MetaPrompt"]["artifact_types"] == ["prompt"]
    # And the optional dependency, so a control can say why GEPA is unavailable.
    assert names["GEPA"]["requires"] == "gepa"
    # DSPyMIPRO is implemented and never auto-selected; the flag says so.
    assert names["DSPyMIPRO"]["explicit_only"] is True


def test_capabilities_names_the_variable_that_enables_a_plugin(client: TestClient) -> None:
    """So the UI can tell an operator what to set instead of hardcoding it."""
    extensibility = client.get(f"{PREFIX}/capabilities").json()["data"]["extensibility"]
    assert extensibility["allowlist_env_var"] == "CALIBER_PLUGIN_ALLOWLIST"
    # No plugins installed in a test environment, and an empty list is the
    # honest answer rather than a missing key.
    assert extensibility["plugins"] == []
