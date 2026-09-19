"""`P4-B`/`P4-C`: the second real Workspace resource adapter -- a CALIBER prompt.

Mirrors ``test_workspace_release_workflow_adapter.py``'s pattern (proving the
:class:`~caliber.workspace_release_adapters.WorkspaceResourceAdapter`
Protocol against a genuine resource type) and ``test_routes_prompts.py``'s
mlflow-stub convention (a bare ``types.ModuleType("mlflow")`` installed via
``monkeypatch.setitem(sys.modules, ...)`` -- offline, no real MLflow Prompt
Registry network I/O).
"""

from __future__ import annotations

import hashlib
import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session

from caliber.db.models import (
    CaliberAgentConfig,
    CaliberProject,
    CaliberWorkspaceEnvironment,
    CaliberWorkspaceRevisionResource,
)
from caliber.workspace_release_adapters import (
    PreparedAction,
    SnapshotPin,
    WorkspaceReleaseAdapterError,
)
from caliber.workspace_release_prompt_adapter import (
    PROMPT_ADAPTER_VERSION,
    PromptWorkspaceResourceAdapter,
    ResolvedPromptVersion,
)

PROJECT_ID = "PRJ-prompt-adapter"
OTHER_PROJECT_ID = "PRJ-prompt-adapter-other"
ADAPTER = PromptWorkspaceResourceAdapter()


def _install_mlflow(
    monkeypatch: pytest.MonkeyPatch,
    *,
    load_refs: dict[str, Any] | None = None,
    set_alias_impl: Any | None = None,
    include_load: bool = True,
    include_set_alias: bool = True,
) -> dict[str, list[Any]]:
    load_refs = load_refs or {}
    alias_calls: list[Any] = []

    def load_prompt(ref: str, allow_missing: bool = False) -> object | None:
        value = load_refs.get(ref)
        if isinstance(value, Exception):
            raise value
        return value

    def set_prompt_alias(name: str, alias: str, version: int) -> None:
        alias_calls.append((name, alias, version))
        if set_alias_impl is not None:
            set_alias_impl(name, alias, version)

    genai_attrs: dict[str, object] = {}
    if include_load:
        genai_attrs["load_prompt"] = load_prompt
    if include_set_alias:
        genai_attrs["set_prompt_alias"] = set_prompt_alias

    mlflow_mod = types.ModuleType("mlflow")
    mlflow_mod.genai = SimpleNamespace(**genai_attrs)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlflow", mlflow_mod)
    return {"alias_calls": alias_calls}


def _seed_project(session: Session, *, project_id: str = PROJECT_ID) -> None:
    session.add(
        CaliberProject(project_id=project_id, name=f"Prompt adapter {project_id}", owner="admin")
    )
    session.flush()


def _seed_target(session: Session, *, name: str, project_id: str | None) -> CaliberAgentConfig:
    target = CaliberAgentConfig(
        agent_id=name,
        experiment_id=f"exp-{name}",
        name=name,
        owner="@test",
        project_id=project_id,
        visibility="project" if project_id else "user",
        artifact_types=["prompt"],
        eval_thresholds={},
        optimizer_config={},
        approval_policy={},
    )
    session.add(target)
    session.flush()
    return target


# -- resolve --------------------------------------------------------------


def test_resolve_loads_the_exact_version_and_computes_no_alias(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_project(db_session)
    _install_mlflow(
        monkeypatch,
        load_refs={
            "prompts:/support-agent/3": SimpleNamespace(
                name="support-agent", version=3, template="Hi {{n}}", tags={"k": "v"}
            )
        },
    )
    project = db_session.get(CaliberProject, PROJECT_ID)
    resolved = ADAPTER.resolve(
        db_session, project, {"resource_id": "support-agent", "version_ref": "3"}
    )
    assert isinstance(resolved, ResolvedPromptVersion)
    assert resolved.name == "support-agent"
    assert resolved.version == 3
    assert resolved.template == "Hi {{n}}"
    assert resolved.tags == {"k": "v"}
    assert resolved.provider_ref == "prompts:/support-agent/3"


def test_resolve_requires_resource_id_and_version_ref(db_session: Session) -> None:
    with pytest.raises(WorkspaceReleaseAdapterError, match="requires"):
        ADAPTER.resolve(db_session, None, {"resource_id": "x"})
    with pytest.raises(WorkspaceReleaseAdapterError, match="requires"):
        ADAPTER.resolve(db_session, None, {})
    with pytest.raises(WorkspaceReleaseAdapterError):
        ADAPTER.resolve(db_session, None, "not-a-mapping")  # type: ignore[arg-type]


def test_resolve_rejects_a_non_integer_version(db_session: Session) -> None:
    with pytest.raises(WorkspaceReleaseAdapterError, match="integer"):
        ADAPTER.resolve(db_session, None, {"resource_id": "x", "version_ref": "not-a-number"})


def test_resolve_fails_closed_when_mlflow_is_unavailable(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "mlflow", None)
    with pytest.raises(WorkspaceReleaseAdapterError, match="mlflow is not installed"):
        ADAPTER.resolve(db_session, None, {"resource_id": "x", "version_ref": "1"})


def test_resolve_raises_when_the_prompt_version_does_not_exist(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_mlflow(monkeypatch, load_refs={})
    with pytest.raises(WorkspaceReleaseAdapterError, match="not found"):
        ADAPTER.resolve(db_session, None, {"resource_id": "ghost", "version_ref": "9"})


def test_resolve_refuses_a_prompt_bound_to_a_different_project(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_project(db_session)
    _seed_project(db_session, project_id=OTHER_PROJECT_ID)
    _seed_target(db_session, name="owned-elsewhere", project_id=OTHER_PROJECT_ID)
    _install_mlflow(
        monkeypatch,
        load_refs={
            "prompts:/owned-elsewhere/1": SimpleNamespace(
                name="owned-elsewhere", version=1, template="x", tags={}
            )
        },
    )
    project = db_session.get(CaliberProject, PROJECT_ID)
    with pytest.raises(WorkspaceReleaseAdapterError, match="registered to project"):
        ADAPTER.resolve(db_session, project, {"resource_id": "owned-elsewhere", "version_ref": "1"})


def test_resolve_allows_a_prompt_with_no_project_bound_target(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare provider-only prompt (no hidden target row at all) is not
    scoped to any project -- see ``prompt_targets.py``'s own "no target = no
    project to gate against" carve-out, reused here."""
    _seed_project(db_session)
    _install_mlflow(
        monkeypatch,
        load_refs={
            "prompts:/unregistered/1": SimpleNamespace(
                name="unregistered", version=1, template="x", tags={}
            )
        },
    )
    project = db_session.get(CaliberProject, PROJECT_ID)
    resolved = ADAPTER.resolve(
        db_session, project, {"resource_id": "unregistered", "version_ref": "1"}
    )
    assert resolved.name == "unregistered"


# -- snapshot ---------------------------------------------------------------


def test_snapshot_computes_a_genuine_content_digest() -> None:
    resolved = ResolvedPromptVersion(
        name="support-agent",
        version=3,
        template="Hello world",
        tags={},
        provider_ref="prompts:/support-agent/3",
    )
    pin = ADAPTER.snapshot(None, resolved)
    assert isinstance(pin, SnapshotPin)
    assert pin.resource_id == "support-agent"
    assert pin.version_ref == "3"
    assert pin.content_sha256 == hashlib.sha256(b"Hello world").hexdigest()
    assert pin.provider_ref == "prompts:/support-agent/3"
    assert pin.resolution["strategy"] == "live_resource_adapter"
    assert pin.resolution["adapter_version"] == PROMPT_ADAPTER_VERSION
    assert pin.resolution["template_length"] == len("Hello world")


def test_snapshot_distinguishes_different_templates() -> None:
    a = ADAPTER.snapshot(None, ResolvedPromptVersion("p", 1, "template A", {}, "prompts:/p/1"))
    b = ADAPTER.snapshot(None, ResolvedPromptVersion("p", 1, "template B", {}, "prompts:/p/1"))
    assert a.content_sha256 != b.content_sha256


def test_snapshot_rejects_a_non_resolved_input() -> None:
    with pytest.raises(WorkspaceReleaseAdapterError, match="resolved prompt version"):
        ADAPTER.snapshot(None, {"not": "resolved"})


# -- validate -----------------------------------------------------------


def test_validate_accepts_an_unscoped_pin(db_session: Session) -> None:
    pin = CaliberWorkspaceRevisionResource(
        resource_pin_id="WSRR-validate-1",
        revision_id="WSR-validate-1",
        resource_type="prompt",
        logical_name="support-agent",
        resource_id="support-agent",
        version_ref="3",
        content_sha256="a" * 64,
        provider_ref="prompts:/support-agent/3",
        purpose="runtime",
        resolution={},
    )
    result = ADAPTER.validate(db_session, pin)
    assert result == {
        "valid": True,
        "resource_pin_id": "WSRR-validate-1",
        "prompt_name": "support-agent",
        "version": 3,
    }


def test_validate_refuses_a_prompt_outside_the_environments_project(
    db_session: Session,
) -> None:
    _seed_project(db_session)
    _seed_project(db_session, project_id=OTHER_PROJECT_ID)
    _seed_target(db_session, name="owned-elsewhere", project_id=OTHER_PROJECT_ID)
    environment = CaliberWorkspaceEnvironment(
        environment_id="WSE-validate",
        project_id=PROJECT_ID,
        name="prod",
        environment_class="production",
        promotion_order=40,
        status="active",
        created_by="admin",
    )
    pin = CaliberWorkspaceRevisionResource(
        resource_pin_id="WSRR-validate-2",
        revision_id="WSR-validate-2",
        resource_type="prompt",
        logical_name="owned-elsewhere",
        resource_id="owned-elsewhere",
        version_ref="1",
        content_sha256="a" * 64,
        provider_ref="prompts:/owned-elsewhere/1",
        purpose="runtime",
        resolution={},
    )
    with pytest.raises(WorkspaceReleaseAdapterError, match="project"):
        ADAPTER.validate(db_session, pin, environment)


def test_validate_rejects_a_non_integer_version(db_session: Session) -> None:
    pin = CaliberWorkspaceRevisionResource(
        resource_pin_id="WSRR-validate-3",
        revision_id="WSR-validate-3",
        resource_type="prompt",
        logical_name="support-agent",
        resource_id="support-agent",
        version_ref="not-a-number",
        content_sha256="a" * 64,
        purpose="runtime",
        resolution={},
    )
    with pytest.raises(WorkspaceReleaseAdapterError, match="non-integer"):
        ADAPTER.validate(db_session, pin)


# -- prepare_release ------------------------------------------------------


def test_prepare_release_builds_a_promote_action() -> None:
    pin = CaliberWorkspaceRevisionResource(
        resource_pin_id="WSRR-prep-1",
        revision_id="WSR-prep-1",
        resource_type="prompt",
        logical_name="support-agent",
        resource_id="support-agent",
        version_ref="3",
        content_sha256="a" * 64,
        purpose="runtime",
        resolution={},
    )
    environment = CaliberWorkspaceEnvironment(
        environment_id="WSE-prep-1",
        project_id=PROJECT_ID,
        name="prod",
        environment_class="production",
        promotion_order=40,
        status="active",
        created_by="admin",
    )
    prepared = ADAPTER.prepare_release(pin, environment, before_ref="2")
    assert prepared.action == "promote"
    assert prepared.target_ref == "prompt:support-agent@prod"
    assert prepared.before_ref == "2"
    assert prepared.after_ref == "3"
    assert prepared.metadata["prompt_name"] == "support-agent"
    assert prepared.metadata["alias"] == "prod"


def test_prepare_release_is_a_no_op_when_before_equals_after() -> None:
    pin = CaliberWorkspaceRevisionResource(
        resource_pin_id="WSRR-prep-2",
        revision_id="WSR-prep-2",
        resource_type="prompt",
        logical_name="support-agent",
        resource_id="support-agent",
        version_ref="3",
        content_sha256="a" * 64,
        purpose="runtime",
        resolution={},
    )
    environment = CaliberWorkspaceEnvironment(
        environment_id="WSE-prep-2",
        project_id=PROJECT_ID,
        name="prod",
        environment_class="production",
        promotion_order=40,
        status="active",
        created_by="admin",
    )
    prepared = ADAPTER.prepare_release(pin, environment, before_ref="3")
    assert prepared.action == "no_op"


# -- apply / rollback / observe -------------------------------------------


def _prepared_action(*, action: str = "promote", after_ref: str = "3") -> PreparedAction:
    return PreparedAction(
        resource_pin_id="WSRR-apply-1",
        resource_type="prompt",
        action=action,
        target_ref="prompt:support-agent@prod",
        before_ref="2",
        after_ref=after_ref,
        metadata={"prompt_name": "support-agent", "alias": "prod"},
    )


def test_apply_release_sets_the_alias_via_mlflow(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_mlflow(monkeypatch)
    outcome = ADAPTER.apply_release(None, _prepared_action())
    assert outcome.status == "applied"
    assert outcome.provider_result["version"] == 3
    assert calls["alias_calls"] == [("support-agent", "prod", 3)]


def test_apply_release_no_op_does_not_touch_mlflow(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_mlflow(monkeypatch)
    outcome = ADAPTER.apply_release(None, _prepared_action(action="no_op"))
    assert outcome.status == "applied"
    assert calls["alias_calls"] == []


def test_apply_release_fails_closed_when_mlflow_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "mlflow", None)
    outcome = ADAPTER.apply_release(None, _prepared_action())
    assert outcome.status == "failed"
    assert outcome.error_code == "mlflow_unavailable"


def test_apply_release_reports_an_invalid_version(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_mlflow(monkeypatch)
    outcome = ADAPTER.apply_release(None, _prepared_action(after_ref="not-a-number"))
    assert outcome.status == "failed"
    assert outcome.error_code == "invalid_prompt_version"


def test_apply_release_reports_a_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(name: str, alias: str, version: int) -> None:
        raise RuntimeError("registry unavailable")

    _install_mlflow(monkeypatch, set_alias_impl=_boom)
    outcome = ADAPTER.apply_release(None, _prepared_action())
    assert outcome.status == "failed"
    assert outcome.error_code == "prompt_alias_set_failed"


def test_rollback_release_sets_the_alias_the_same_way(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_mlflow(monkeypatch)
    outcome = ADAPTER.rollback_release(None, _prepared_action(after_ref="2"))
    assert outcome.status == "applied"
    assert calls["alias_calls"] == [("support-agent", "prod", 2)]


def test_observe_release_reads_the_real_alias_state(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_mlflow(
        monkeypatch,
        load_refs={
            "prompts:/support-agent@prod": SimpleNamespace(
                name="support-agent", version=3, template="x", tags={}
            )
        },
    )
    outcome = ADAPTER.observe_release(None, _prepared_action())
    assert outcome.status == "applied"
    assert outcome.provider_result["version"] == 3


def test_observe_release_reports_a_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_mlflow(
        monkeypatch,
        load_refs={
            "prompts:/support-agent@prod": SimpleNamespace(
                name="support-agent", version=2, template="x", tags={}
            )
        },
    )
    outcome = ADAPTER.observe_release(None, _prepared_action(after_ref="3"))
    assert outcome.status == "failed"
    assert outcome.error_code == "target_not_observed"


def test_observe_release_reports_a_missing_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_mlflow(monkeypatch, load_refs={})
    outcome = ADAPTER.observe_release(None, _prepared_action())
    assert outcome.status == "failed"
    assert outcome.error_code == "prompt_alias_not_found"


def test_observe_release_fails_closed_when_mlflow_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "mlflow", None)
    outcome = ADAPTER.observe_release(None, _prepared_action())
    assert outcome.status == "failed"
    assert outcome.error_code == "mlflow_unavailable"


def test_malformed_target_ref_is_rejected() -> None:
    bad = PreparedAction(
        resource_pin_id="WSRR-bad",
        resource_type="prompt",
        action="promote",
        target_ref="not-a-prompt-ref",
        before_ref=None,
        after_ref="1",
        metadata={},
    )
    with pytest.raises(WorkspaceReleaseAdapterError, match="malformed"):
        ADAPTER._promote(bad)
