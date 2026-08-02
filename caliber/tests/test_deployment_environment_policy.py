"""Regression tests for environment-class keying and universal alias preflight.

The review recorded three related defects, all about a safety requirement being
attached to the wrong thing:

* the production isolation requirement was keyed to the literal alias string
  ``prod``, matched case-sensitively, so ``production`` / ``prod-eu`` / ``PROD``
  promoted with local containment and *no blocker*;
* MCP deployment preflight ran only on forward promotion and promotion approval,
  so rollback and refinement-candidate rotation moved the live alias with no
  dependency check at all; and
* MCP dependency inspection stopped at the root manifest, so a parent that
  declares no MCP tool passed preflight while the subworkflow it invokes at
  runtime used a blocked server.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberMcpServer,
    CaliberWorkflow,
    CaliberWorkflowDeployment,
    CaliberWorkflowVersion,
)
from caliber.deployment_environments import (
    DEVELOPMENT,
    PRODUCTION,
    STAGING,
    environment_class,
    requires_external_isolation,
)
from caliber.mcp_policy import (
    _MAX_SUBWORKFLOW_DEPTH,
    deployment_blockers,
    extract_dependencies,
)
from caliber.workflows import promoter as promoter_module
from caliber.workflows.promoter import AliasPreflightError, rollback
from tests.workflow_helpers import make_manifest

# ---------------------------------------------------------------------------
# Environment classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        # Every spelling the old literal 'prod' match let through.
        ("prod", PRODUCTION),
        ("PROD", PRODUCTION),
        ("production", PRODUCTION),
        ("Production", PRODUCTION),
        ("prd", PRODUCTION),
        ("live", PRODUCTION),
        ("prod-eu", PRODUCTION),
        ("prod_eu", PRODUCTION),
        ("  prod  ", PRODUCTION),
        ("PRODUCTION-US-EAST", PRODUCTION),
        # Staging must not be swallowed by the 'prod' substring in 'pre-prod'.
        ("staging", STAGING),
        ("stage", STAGING),
        ("preprod", STAGING),
        ("pre-prod", STAGING),
        ("uat", STAGING),
        ("qa", STAGING),
        ("dev", DEVELOPMENT),
        ("development", DEVELOPMENT),
        ("local", DEVELOPMENT),
        ("sandbox", DEVELOPMENT),
        # CALIBER's own non-deployment sentinels: a direct run of a version is not
        # a production deployment and must not demand production boundaries.
        ("manual", DEVELOPMENT),
        ("preview", DEVELOPMENT),
    ],
)
def test_environment_class_is_insensitive_to_spelling(alias: str, expected: str) -> None:
    assert environment_class(alias, CaliberConfig()) == expected


def test_unrecognised_alias_fails_closed_into_production() -> None:
    """A house-style alias nobody classified must inherit the *strictest*
    requirements, not escape them."""
    assert environment_class("canary", CaliberConfig()) == PRODUCTION
    assert environment_class("blue", CaliberConfig()) == PRODUCTION


def test_operator_mapping_overrides_the_patterns() -> None:
    config = CaliberConfig(
        deployment_environment_classes="blue=production,green=production,demo=development"
    )
    assert environment_class("blue", config) == PRODUCTION
    assert environment_class("demo", config) == DEVELOPMENT
    # A typo in the class name must not silently downgrade the requirement.
    bad = CaliberConfig(deployment_environment_classes="blue=prodction")
    assert environment_class("blue", bad) == PRODUCTION


def test_default_class_is_configurable_for_a_dev_only_installation() -> None:
    config = CaliberConfig(deployment_default_environment_class="development")
    assert environment_class("canary", config) == DEVELOPMENT
    assert environment_class("prod", config) == PRODUCTION  # patterns still win


@pytest.mark.parametrize("alias", ["prod", "PROD", "production", "prod-eu", "canary"])
def test_isolation_is_required_for_every_production_spelling(alias: str) -> None:
    """The concrete regression: the old alias-list match returned False for all
    of these except the exact literal ``prod``."""
    assert requires_external_isolation(alias, CaliberConfig()) is True


@pytest.mark.parametrize("alias", ["dev", "sandbox", "manual", "preview"])
def test_isolation_is_not_required_for_development(alias: str) -> None:
    assert requires_external_isolation(alias, CaliberConfig()) is False


def test_legacy_alias_list_still_adds_an_opt_in() -> None:
    """An existing deployment's configuration keeps working: the legacy list can
    still *add* a requirement, it just can no longer be the only thing enforcing
    one."""
    config = CaliberConfig(
        mcp_require_external_isolation_for_aliases="dev",
        mcp_require_external_isolation_for_environment_classes="production",
    )
    assert requires_external_isolation("dev", config) is True
    assert requires_external_isolation("sandbox", config) is False


def test_staging_can_be_required_too() -> None:
    config = CaliberConfig(
        mcp_require_external_isolation_for_environment_classes="production,staging"
    )
    assert requires_external_isolation("staging", config) is True
    assert requires_external_isolation("dev", config) is False


def test_an_empty_or_bogus_class_list_falls_back_to_production() -> None:
    for raw in ("", "   ", "nonsense"):
        config = CaliberConfig(mcp_require_external_isolation_for_environment_classes=raw)
        assert requires_external_isolation("prod", config) is True
        assert requires_external_isolation("dev", config) is False


# ---------------------------------------------------------------------------
# Transitive subworkflow dependency inspection
# ---------------------------------------------------------------------------


def _blocked_server(server_id: str = "MCP-BLOCKED") -> CaliberMcpServer:
    """A server that fails readiness: disabled status is enough to block."""
    return CaliberMcpServer(
        server_id=server_id,
        name="Blocked server",
        transport="stdio",
        command="${PYTHON}",
        args=["-m", "caliber.mcp_servers.db", "--mode", "relational"],
        status="disabled",
        discovered_tools=[{"name": "search_docs"}],
        tool_policies={
            "search_docs": {
                "allowed": True,
                "side_effect_level": "read",
                "requires_approval": False,
            }
        },
        last_connected_at=datetime.now(timezone.utc),
    )


def _manifest_with_mcp_node(workflow_id: str, server_id: str) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    nodes = manifest["nodes"]
    assert isinstance(nodes, dict)
    nodes["mcp_lookup"] = {
        "id": "mcp_lookup",
        "type": "mcp_resource",
        "server_id": server_id,
        "tool_name": "search_docs",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"output": {"type": "string"}},
    }
    return manifest


def _manifest_with_subworkflow(
    workflow_id: str, child_workflow_id: str, alias: str
) -> dict[str, object]:
    manifest = make_manifest(workflow_id)
    nodes = manifest["nodes"]
    assert isinstance(nodes, dict)
    nodes["child"] = {
        "id": "child",
        "type": "subworkflow",
        "workflow_id": child_workflow_id,
        "alias": alias,
        "inputs": {"input": {"type": "string"}},
        "outputs": {"output": {"type": "string"}, "result": {"type": "structured"}},
    }
    return manifest


def _deploy(session: Session, workflow_id: str, alias: str, manifest: dict[str, object]) -> str:
    """Create workflow + published version + active deployment; return version id."""
    version_id = f"wfv-{workflow_id}-{alias}"
    session.add(CaliberWorkflow(workflow_id=workflow_id, name=workflow_id, owner="@test"))
    session.add(
        CaliberWorkflowVersion(
            version_id=version_id,
            workflow_id=workflow_id,
            version_number=1,
            status="published",
            manifest=manifest,
            manifest_hash=f"hash-{version_id}",
        )
    )
    session.add(
        CaliberWorkflowDeployment(
            deployment_id=f"wfd-{workflow_id}-{alias}",
            workflow_id=workflow_id,
            alias=alias,
            version_id=version_id,
            status="active",
            deployed_by="@test",
            deployed_at=datetime.now(timezone.utc),
            rollback_checkpoint=[],
        )
    )
    session.flush()
    return version_id


def test_extract_dependencies_walks_into_a_deployed_subworkflow(db_session: Session) -> None:
    """Without the session the walk is root-only (the old behaviour); with it the
    child's MCP dependency is found and labelled by the path that reached it."""
    child_manifest = _manifest_with_mcp_node("child-wf", "MCP-CHILD")
    _deploy(db_session, "child-wf", "prod", child_manifest)
    parent_manifest = _manifest_with_subworkflow("parent-wf", "child-wf", "prod")

    root_only = extract_dependencies(parent_manifest)
    assert root_only == []

    transitive = extract_dependencies(parent_manifest, session=db_session)
    assert [d.server_id for d in transitive] == ["MCP-CHILD"]
    assert transitive[0].label.startswith("via subworkflow 'child' → ")


def test_deployment_preflight_blocks_a_parent_whose_child_uses_a_blocked_server(
    db_session: Session,
) -> None:
    """The concrete gap: the parent declares no MCP tool of its own, so the old
    root-only preflight passed it while the child it invokes was unusable."""
    db_session.add(_blocked_server("MCP-CHILD"))
    _deploy(db_session, "child-wf", "prod", _manifest_with_mcp_node("child-wf", "MCP-CHILD"))
    parent_manifest = _manifest_with_subworkflow("parent-wf", "child-wf", "prod")

    # Root-only inspection — what preflight used to do — sees nothing to block.
    assert extract_dependencies(parent_manifest) == []
    blockers = deployment_blockers(db_session, parent_manifest, alias="dev", config=CaliberConfig())
    # The child's disabled status must surface, attributed to the child.
    assert any("via subworkflow 'child'" in b and "disabled" in b for b in blockers)


def test_transitive_walk_terminates_on_a_subworkflow_cycle(db_session: Session) -> None:
    """A stored manifest can predate the compiler's cycle check; the walk must
    still terminate rather than recurse forever."""
    db_session.add(CaliberWorkflow(workflow_id="a-wf", name="a", owner="@test"))
    a_manifest = _manifest_with_subworkflow("a-wf", "b-wf", "prod")
    db_session.add(
        CaliberWorkflowVersion(
            version_id="wfv-a",
            workflow_id="a-wf",
            version_number=1,
            status="published",
            manifest=a_manifest,
            manifest_hash="hash-a",
        )
    )
    db_session.add(
        CaliberWorkflowDeployment(
            deployment_id="wfd-a",
            workflow_id="a-wf",
            alias="prod",
            version_id="wfv-a",
            status="active",
            deployed_by="@test",
            deployed_at=datetime.now(timezone.utc),
            rollback_checkpoint=[],
        )
    )
    _deploy(db_session, "b-wf", "prod", _manifest_with_subworkflow("b-wf", "a-wf", "prod"))

    assert extract_dependencies(a_manifest, session=db_session) == []


def test_an_unresolvable_child_blocks_an_alias_rotation_but_not_a_run(
    db_session: Session,
) -> None:
    """An alias rotation is a promise the whole graph is deployable, so a child
    with no active deployment is a blocker. A run submission keeps its existing
    contract, where the runtime reports the failure precisely on the run record."""
    parent_manifest = _manifest_with_subworkflow("parent-wf", "absent-wf", "prod")

    rotation = deployment_blockers(
        db_session, parent_manifest, alias="dev", require_resolvable_subworkflows=True
    )
    assert any("absent-wf" in b and "no active deployment" in b for b in rotation)
    assert deployment_blockers(db_session, parent_manifest, alias="dev") == []


# ---------------------------------------------------------------------------
# Universal alias preflight (rollback)
# ---------------------------------------------------------------------------


def test_rollback_runs_mcp_preflight_and_leaves_the_alias_untouched_on_failure(
    db_session: Session,
) -> None:
    """Regression: rollback rotated the live alias with no preflight, so it could
    restore a version whose MCP server had since been disabled."""
    db_session.add(_blocked_server("MCP-OLD"))
    db_session.add(CaliberWorkflow(workflow_id="wf-rb", name="Rollback", owner="@test"))
    old = CaliberWorkflowVersion(
        version_id="wfv-old",
        workflow_id="wf-rb",
        version_number=1,
        status="published",
        manifest=_manifest_with_mcp_node("wf-rb", "MCP-OLD"),
        manifest_hash="hash-old",
    )
    new = CaliberWorkflowVersion(
        version_id="wfv-new",
        workflow_id="wf-rb",
        version_number=2,
        status="published",
        manifest=make_manifest("wf-rb"),
        manifest_hash="hash-new",
    )
    deployment = CaliberWorkflowDeployment(
        deployment_id="wfd-rb",
        workflow_id="wf-rb",
        alias="prod",
        version_id="wfv-new",
        status="active",
        deployed_by="@test",
        deployed_at=datetime.now(timezone.utc),
        rollback_checkpoint=[
            {"version_id": "wfv-old", "deployed_at": None, "deployed_by": "@test"}
        ],
    )
    db_session.add_all([old, new, deployment])
    db_session.flush()

    with pytest.raises(AliasPreflightError) as excinfo:
        rollback(db_session, "wf-rb", "prod", actor="@test", config=CaliberConfig())

    assert "MCP-OLD" in str(excinfo.value) or excinfo.value.blockers
    # The alias and its checkpoint stack must be intact so the operator can fix
    # the dependency and retry.
    db_session.refresh(deployment)
    assert deployment.version_id == "wfv-new"
    assert [entry["version_id"] for entry in deployment.rollback_checkpoint] == ["wfv-old"]


def test_rollback_succeeds_and_records_the_environment_class(db_session: Session) -> None:
    """``environment`` was a dormant column: nothing populated it. It must now
    agree with the resolver that keys the isolation requirement."""
    db_session.add(CaliberWorkflow(workflow_id="wf-ok", name="Rollback ok", owner="@test"))
    for number, version_id in ((1, "wfv-a"), (2, "wfv-b")):
        db_session.add(
            CaliberWorkflowVersion(
                version_id=version_id,
                workflow_id="wf-ok",
                version_number=number,
                status="published",
                manifest=make_manifest("wf-ok"),
                manifest_hash=f"hash-{version_id}",
            )
        )
    deployment = CaliberWorkflowDeployment(
        deployment_id="wfd-ok",
        workflow_id="wf-ok",
        alias="prod-eu",
        version_id="wfv-b",
        status="active",
        deployed_by="@test",
        deployed_at=datetime.now(timezone.utc),
        rollback_checkpoint=[{"version_id": "wfv-a", "deployed_at": None, "deployed_by": "@test"}],
    )
    db_session.add(deployment)
    db_session.flush()

    result = rollback(db_session, "wf-ok", "prod-eu", actor="@ops", config=CaliberConfig())
    assert result.version_id == "wfv-a"
    assert result.rollback_checkpoint == []
    assert result.environment == PRODUCTION
    assert result.deployed_by == "@ops"


def test_rollback_refuses_a_checkpoint_whose_version_no_longer_exists(
    db_session: Session,
) -> None:
    db_session.add(CaliberWorkflow(workflow_id="wf-gone", name="Gone", owner="@test"))
    db_session.add(
        CaliberWorkflowVersion(
            version_id="wfv-current",
            workflow_id="wf-gone",
            version_number=2,
            status="published",
            manifest=make_manifest("wf-gone"),
            manifest_hash="hash-current",
        )
    )
    db_session.add(
        CaliberWorkflowDeployment(
            deployment_id="wfd-gone",
            workflow_id="wf-gone",
            alias="dev",
            version_id="wfv-current",
            status="active",
            deployed_by="@test",
            deployed_at=datetime.now(timezone.utc),
            rollback_checkpoint=[{"version_id": "wfv-deleted"}],
        )
    )
    db_session.flush()

    with pytest.raises(AliasPreflightError, match="wfv-deleted"):
        rollback(db_session, "wf-gone", "dev", actor="@test", config=CaliberConfig())


# ---------------------------------------------------------------------------
# Managed-file time-of-check/time-of-use at rotation
# ---------------------------------------------------------------------------


def test_alias_rotation_refuses_a_version_whose_pinned_object_disappeared(
    db_session: Session, tmp_path: object
) -> None:
    """Regression: the deploy gate verified pinned objects when it evaluated, but
    approval (potentially much later) did not re-read them, so deleting an object
    between evaluation and approval could rotate the alias onto an unrunnable
    version. Rollback had the same hole with no check at all.
    """
    from pathlib import Path

    from caliber.config import WorkflowStorageConfig
    from caliber.db.models import CaliberProject
    from caliber.storage import LocalStorageBackend, WorkingDirectoryService
    from caliber.workflows.promoter import require_alias_target_ready

    assert isinstance(tmp_path, Path)
    storage_config = WorkflowStorageConfig(base_uri=f"file://{tmp_path}/files")
    service = WorkingDirectoryService(LocalStorageBackend(storage_config.base_uri), storage_config)
    project = CaliberProject(project_id="PRJ-toctou", tenant_id="t", name="TOCTOU", owner="@test")
    workflow = CaliberWorkflow(
        workflow_id="wf-toctou", project_id=project.project_id, name="TOCTOU", owner="@test"
    )
    db_session.add_all([project, workflow])
    record = service.register_project_file(
        db_session,
        project_id=project.project_id,
        tenant_id=project.tenant_id,
        kind="input",
        filename="source.md",
        data=b"pinned content",
        media_type="text/markdown",
        actor="@test",
    )
    manifest = make_manifest("wf-toctou")
    nodes = manifest["nodes"]
    assert isinstance(nodes, dict)
    nodes["managed_source"] = {
        "id": "managed_source",
        "type": "file_input",
        "file_ref": record.to_api()["immutable_ref"],
    }
    manifest["edges"] = [
        {"id": "e0", "from": "start", "to": "managed_source", "map": {"msg": "path"}},
        {"id": "e1", "from": "managed_source", "to": "agent", "map": {"text": "input"}},
        {"id": "e2", "from": "agent", "to": "final", "map": {"final_output": "response"}},
    ]
    version = CaliberWorkflowVersion(
        version_id="wfv-toctou",
        workflow_id="wf-toctou",
        version_number=1,
        status="published",
        manifest=manifest,
        manifest_hash="hash-toctou",
    )
    db_session.add(version)
    db_session.flush()

    config = CaliberConfig(workflow_storage=storage_config)
    # Verified while the object is present.
    require_alias_target_ready(db_session, "prod", "wfv-toctou", config=config)

    # Now the object physically disappears between evaluation and rotation.
    for path in (tmp_path / "files").rglob("*"):
        if path.is_file():
            path.unlink()

    with pytest.raises(AliasPreflightError) as excinfo:
        require_alias_target_ready(db_session, "prod", "wfv-toctou", config=config)
    assert any("managed file preflight failed" in blocker for blocker in excinfo.value.blockers)


# ---------------------------------------------------------------------------
# L5 — the traversal bound must block, not silently stop
# ---------------------------------------------------------------------------


def _deep_subworkflow_chain(
    session: Session, length: int, *, leaf_server: str
) -> dict[str, object]:
    """Deploy ``wf-0 → wf-1 → … → wf-{length}``, the leaf using ``leaf_server``.

    Returns the root manifest, which is *not* deployed: preflight is called on it
    directly, exactly as a promotion would.
    """
    leaf_id = f"deep-wf-{length}"
    _deploy(session, leaf_id, "prod", _manifest_with_mcp_node(leaf_id, leaf_server))
    # Build upward from the leaf so each parent's child is already deployed.
    for index in range(length - 1, 0, -1):
        _deploy(
            session,
            f"deep-wf-{index}",
            "prod",
            _manifest_with_subworkflow(f"deep-wf-{index}", f"deep-wf-{index + 1}", "prod"),
        )
    return _manifest_with_subworkflow("deep-wf-0", "deep-wf-1", "prod")


def test_a_chain_within_the_bound_is_still_fully_inspected(db_session: Session) -> None:
    """The bound must not be so eager that a legitimately nested graph is refused."""
    root = _deep_subworkflow_chain(db_session, 5, leaf_server="MCP-DEEP-OK")

    dependencies = extract_dependencies(root, session=db_session)

    assert [d.server_id for d in dependencies] == ["MCP-DEEP-OK"]
    assert not any(d.depth_exhausted for d in dependencies)


def test_exceeding_the_depth_bound_is_an_explicit_blocker(db_session: Session) -> None:
    """L5: the walk recursed only ``while _depth < 16`` and then returned normally,
    so a deeper chain hid the leaf's MCP dependency and preflight passed on a partial
    inspection reported as a complete one."""
    root = _deep_subworkflow_chain(db_session, _MAX_SUBWORKFLOW_DEPTH + 3, leaf_server="MCP-HIDDEN")

    dependencies = extract_dependencies(root, session=db_session)

    # The leaf is genuinely out of reach — that is what the bound means — so the
    # requirement is that the walk *says so* rather than returning a clean result.
    assert not any(d.server_id == "MCP-HIDDEN" for d in dependencies)
    exhausted = [d for d in dependencies if d.depth_exhausted]
    assert exhausted, "reaching the bound must be reported, not returned as success"
    assert str(_MAX_SUBWORKFLOW_DEPTH) in exhausted[0].label

    blockers = deployment_blockers(db_session, root, alias="dev", config=CaliberConfig())
    assert any("inspection bound" in blocker for blocker in blockers), blockers


def test_depth_exhaustion_blocks_even_where_unresolved_children_are_tolerated(
    db_session: Session,
) -> None:
    """The two conditions must not share a switch.

    ``require_resolvable_subworkflows=False`` exists for import preflight on a graph
    whose children are not deployed yet — a state an operator can reason about.
    "We stopped inspecting" is an unknown, and an unknown has to fail closed
    regardless of that tolerance.
    """
    root = _deep_subworkflow_chain(
        db_session, _MAX_SUBWORKFLOW_DEPTH + 2, leaf_server="MCP-HIDDEN2"
    )

    blockers = deployment_blockers(
        db_session,
        root,
        alias="dev",
        config=CaliberConfig(),
        require_resolvable_subworkflows=False,
    )
    assert any("inspection bound" in blocker for blocker in blockers), blockers


def test_an_uninspectable_chain_blocks_deleting_an_mcp_server(db_session: Session) -> None:
    """The second half of L5: server deletion asked "does any deployed version
    reference this?" and a depth-exhausted walk answered "no".

    Deleting on the strength of an inspection that stopped early is how a
    checkpointed rollback silently breaks, so an unprovable answer must block.
    """
    from caliber.routes.mcp_servers import _version_reference_reason

    root = _deep_subworkflow_chain(db_session, _MAX_SUBWORKFLOW_DEPTH + 2, leaf_server="MCP-DEL")
    root_version_id = _deploy(db_session, "deep-wf-0", "prod", root)

    reason = _version_reference_reason(db_session, root_version_id, "MCP-DEL")

    assert reason is not None, "an incomplete inspection must not read as 'no reference'"
    assert "depth bound" in reason


def test_a_shallow_version_that_does_not_reference_the_server_is_still_deletable(
    db_session: Session,
) -> None:
    """The guard must stay narrow: a fully inspected version with no reference must
    not be turned into a phantom blocker."""
    from caliber.routes.mcp_servers import _version_reference_reason

    root = _deep_subworkflow_chain(db_session, 3, leaf_server="MCP-OTHER")
    root_version_id = _deploy(db_session, "deep-wf-0", "prod", root)

    assert _version_reference_reason(db_session, root_version_id, "MCP-UNRELATED") is None
    # And it correctly still reports the one it does reference.
    assert _version_reference_reason(db_session, root_version_id, "MCP-OTHER") == ""


# ---------------------------------------------------------------------------
# Unmanaged host-filesystem nodes at the promotion boundary
# ---------------------------------------------------------------------------


def _host_path_version(db_session: Session, version_id: str) -> None:
    """Publish a version carrying all three unmanaged host-filesystem node kinds."""
    workflow = CaliberWorkflow(
        workflow_id=f"wf-{version_id}", project_id=None, name="Host paths", owner="@test"
    )
    db_session.add(workflow)
    manifest = make_manifest(f"wf-{version_id}")
    nodes = manifest["nodes"]
    assert isinstance(nodes, dict)
    nodes["legacy_read"] = {"id": "legacy_read", "type": "file_input", "path": "/etc/passwd"}
    nodes["legacy_dir"] = {"id": "legacy_dir", "type": "folder_input", "path": "/etc/ssl"}
    nodes["legacy_write"] = {"id": "legacy_write", "type": "output_folder", "path": "/tmp/escaped"}
    db_session.add(
        CaliberWorkflowVersion(
            version_id=version_id,
            workflow_id=f"wf-{version_id}",
            version_number=1,
            status="published",
            manifest=manifest,
            manifest_hash=f"hash-{version_id}",
        )
    )
    db_session.flush()


def test_production_alias_refuses_unmanaged_host_filesystem_nodes(db_session: Session) -> None:
    """A plain operator must not be able to put arbitrary host reads/writes live.

    Authoring, publishing, and promoting are all ``operator`` scope, and these
    nodes resolve paths with no root confinement, so before this gate an operator
    could point a production alias at a workflow that reads ``/etc/passwd`` and
    writes a file of its choosing into a directory of its choosing.
    """
    from caliber.workflows.promoter import require_alias_target_ready

    _host_path_version(db_session, "wfv-hostpath-prod")

    with pytest.raises(AliasPreflightError) as excinfo:
        require_alias_target_ready(db_session, "prod", "wfv-hostpath-prod", config=CaliberConfig())

    blob = "; ".join(excinfo.value.blockers)
    # Every offending node is named, so the operator knows what to change.
    assert "legacy_read (file_input without a managed file)" in blob
    assert "legacy_dir (folder_input)" in blob
    assert "legacy_write (output_folder)" in blob
    assert "production-class" in blob


def test_development_alias_still_allows_host_filesystem_nodes(db_session: Session) -> None:
    """The same version stays deployable where the affordance is intended."""
    from caliber.workflows.promoter import require_alias_target_ready

    _host_path_version(db_session, "wfv-hostpath-dev")

    require_alias_target_ready(db_session, "dev", "wfv-hostpath-dev", config=CaliberConfig())


def test_host_path_refusal_is_widenable_by_configuration(db_session: Session) -> None:
    """A deployment whose authors are as trusted as its operators can opt in."""
    from caliber.workflows.promoter import require_alias_target_ready

    _host_path_version(db_session, "wfv-hostpath-optin")
    config = CaliberConfig(
        workflow_host_path_nodes_allowed_environment_classes="development,production"
    )

    require_alias_target_ready(db_session, "prod", "wfv-hostpath-optin", config=config)


def test_host_path_allowlist_falls_back_to_development_on_a_typo(db_session: Session) -> None:
    """An unrecognised class must not silently open production."""
    from caliber.workflows.promoter import require_alias_target_ready

    _host_path_version(db_session, "wfv-hostpath-typo")
    config = CaliberConfig(workflow_host_path_nodes_allowed_environment_classes="prodcution")

    with pytest.raises(AliasPreflightError):
        require_alias_target_ready(db_session, "prod", "wfv-hostpath-typo", config=config)


def test_managed_file_input_is_not_refused_by_the_host_path_gate(db_session: Session) -> None:
    """The gate matches Preview's predicate, which exempts a pinned managed object.

    Refusing a managed ``file_ref`` here would break the supported path this
    feature exists to steer authors toward.
    """
    workflow = CaliberWorkflow(
        workflow_id="wf-managed-ok", project_id=None, name="Managed", owner="@test"
    )
    db_session.add(workflow)
    manifest = make_manifest("wf-managed-ok")
    nodes = manifest["nodes"]
    assert isinstance(nodes, dict)
    # ``file_ref`` present -> managed; content is verified separately by
    # ``_managed_file_blockers``, which is not what this test is about.
    nodes["managed_read"] = {
        "id": "managed_read",
        "type": "file_input",
        "file_ref": {
            "file_id": "F1",
            "file_ref": "caliber://project/PRJ/file/F1@1",
            "sha256": "0" * 64,
            "name": "source.md",
            "size_bytes": 14,
            "media_type": "text/markdown",
            "object_version_id": "1",
        },
    }
    db_session.add(
        CaliberWorkflowVersion(
            version_id="wfv-managed-ok",
            workflow_id="wf-managed-ok",
            version_number=1,
            status="published",
            manifest=manifest,
            manifest_hash="hash-managed-ok",
        )
    )
    db_session.flush()

    blockers = promoter_module._host_path_blockers(
        db_session.get(CaliberWorkflowVersion, "wfv-managed-ok"),
        alias="prod",
        config=CaliberConfig(),
    )
    assert blockers == []


# ---------------------------------------------------------------------------
# Registered tools meet the same isolation bar as MCP servers
# ---------------------------------------------------------------------------


def _tool_version(db_session: Session, version_id: str) -> None:
    workflow = CaliberWorkflow(
        workflow_id=f"wf-{version_id}", project_id=None, name="Tools", owner="@test"
    )
    db_session.add(workflow)
    manifest = make_manifest(f"wf-{version_id}")
    manifest["tools"] = {"lookup": {"registry_ref": "tool.lookup.v1", "version_constraint": ">=1"}}
    db_session.add(
        CaliberWorkflowVersion(
            version_id=version_id,
            workflow_id=f"wf-{version_id}",
            version_number=1,
            status="published",
            manifest=manifest,
            manifest_hash=f"hash-{version_id}",
        )
    )
    db_session.flush()


def test_production_refuses_registered_tools_without_an_isolating_backend(
    db_session: Session,
) -> None:
    """The same version was refused for its MCP server and accepted for its tool.

    ``LocalSubprocessToolSandbox`` is a process boundary — its own docstring says
    the child keeps ambient filesystem and network authority — so gating MCP on
    external isolation while letting tool code through was an inconsistency
    rather than a policy.
    """
    from caliber.workflows.promoter import require_alias_target_ready

    _tool_version(db_session, "wfv-tools-prod")

    config = CaliberConfig(
        tool_sandbox_require_external_isolation_for_environment_classes="production"
    )
    with pytest.raises(AliasPreflightError) as excinfo:
        require_alias_target_ready(db_session, "prod", "wfv-tools-prod", config=config)

    assert any("OS-enforced isolation" in blocker for blocker in excinfo.value.blockers)


def test_an_operator_supplied_backend_satisfies_the_gate(db_session: Session) -> None:
    """Pointing at an isolating backend is how the gate is meant to be cleared."""
    from caliber.workflows.promoter import require_alias_target_ready

    _tool_version(db_session, "wfv-tools-backend")
    config = CaliberConfig(
        tool_sandbox_backend="acme.sandboxes:docker_factory",
        tool_sandbox_require_external_isolation_for_environment_classes="production",
    )

    require_alias_target_ready(db_session, "prod", "wfv-tools-backend", config=config)


def test_development_still_deploys_registered_tools(db_session: Session) -> None:
    """The shipped sandbox remains the right boundary for trusted authors."""
    from caliber.workflows.promoter import require_alias_target_ready

    _tool_version(db_session, "wfv-tools-dev")
    config = CaliberConfig(
        tool_sandbox_require_external_isolation_for_environment_classes="production"
    )

    require_alias_target_ready(db_session, "dev", "wfv-tools-dev", config=config)


def test_a_version_with_no_tools_is_unaffected(db_session: Session) -> None:
    """The gate must only fire for versions that actually execute tool code."""
    from caliber.workflows.promoter import require_alias_target_ready

    workflow = CaliberWorkflow(
        workflow_id="wf-no-tools", project_id=None, name="None", owner="@test"
    )
    db_session.add(workflow)
    db_session.add(
        CaliberWorkflowVersion(
            version_id="wfv-no-tools",
            workflow_id="wf-no-tools",
            version_number=1,
            status="published",
            manifest=make_manifest("wf-no-tools"),
            manifest_hash="hash-no-tools",
        )
    )
    db_session.flush()

    require_alias_target_ready(db_session, "prod", "wfv-no-tools", config=CaliberConfig())


def test_the_sandbox_gate_is_off_by_default(db_session: Session) -> None:
    """Shipped behaviour must be unchanged: this is hardening, not a migration.

    Requiring an isolating backend by default refused every existing prod
    promotion that uses a registered tool. MCP can be strict because it is a
    deliberate integration; tools are ubiquitous.
    """
    from caliber.workflows.promoter import require_alias_target_ready

    _tool_version(db_session, "wfv-tools-default")

    require_alias_target_ready(db_session, "prod", "wfv-tools-default", config=CaliberConfig())
