"""Tests for the Promoter abstraction."""

from __future__ import annotations

import sys
import types

import pytest
from sqlalchemy.orm import Session

from caliber.db.models import CaliberReleaseOperation
from caliber.promoter import (
    CompositePromoter,
    FakePromoter,
    MLflowPromoter,
    PromoterError,
    PromotionRequest,
    PromotionResult,
    RollbackRequest,
    build_promoter,
)


def _request(**overrides: object) -> PromotionRequest:
    defaults: dict[str, object] = {
        "agent_id": "support-agent",
        "artifact_type": "prompt",
        "new_content": "you are a helpful agent",
        "rationale": "addresses missing tool call",
        "approval_id": "AP-1",
    }
    defaults.update(overrides)
    return PromotionRequest(**defaults)  # type: ignore[arg-type]


def test_fake_promoter_records_calls() -> None:
    promoter = FakePromoter()
    promoter.promote(_request(agent_id="a"))
    promoter.promote(_request(agent_id="b"))
    assert [c.agent_id for c in promoter.calls] == ["a", "b"]


def test_fake_promoter_returns_synthetic_ref_by_default() -> None:
    promoter = FakePromoter()
    result = promoter.promote(_request())
    assert isinstance(result, PromotionResult)
    assert result.artifact_ref.startswith("prompt://support-agent")
    assert result.rotated_at is not None
    assert result.details["approval_id"] == "AP-1"


def test_fake_promoter_can_be_overridden_with_specific_result() -> None:
    canned = PromotionResult(
        artifact_ref="prompt://x/v42",
        rotated_at=__import__("datetime").datetime.now(),
        details={"v": 42},
    )
    promoter = FakePromoter(result=canned)
    assert promoter.promote(_request()) is canned


def test_fake_promoter_can_simulate_failure() -> None:
    promoter = FakePromoter(fail_with=PromoterError("registry down"))
    with pytest.raises(PromoterError, match=r"registry down"):
        promoter.promote(_request())


def test_mlflow_promoter_rejects_non_prompt_artifact_types() -> None:
    """Multi-artifact bundles aren't supported by this promoter yet — they
    route through a dedicated bundle promoter in a later milestone."""
    with pytest.raises(PromoterError, match=r"only supports artifact_type='prompt'"):
        MLflowPromoter().promote(_request(artifact_type="dataset"))


def test_build_promoter_fake() -> None:
    promoter = build_promoter("fake")
    assert isinstance(promoter, CompositePromoter)


def test_build_promoter_mlflow() -> None:
    promoter = build_promoter("mlflow")
    assert isinstance(promoter, CompositePromoter)


def test_build_promoter_unknown_raises() -> None:
    with pytest.raises(PromoterError, match=r"unknown promoter_provider"):
        build_promoter("bogus")


def test_mlflow_promoter_preserves_registered_version_for_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An alias timeout is indeterminate, so deleting its target could break a
    release that actually landed. Preserve it for observed reconciliation."""
    stub = types.ModuleType("mlflow")

    class _StubVersion:
        version = 7
        uri = "prompts:/support-agent/7"

    register_calls: list[dict[str, object]] = []
    delete_calls: list[dict[str, object]] = []

    def register_prompt(**kwargs: object) -> _StubVersion:
        register_calls.append(kwargs)
        return _StubVersion()

    def set_prompt_alias(**kwargs: object) -> None:
        raise RuntimeError("alias service unavailable")

    def delete_prompt_version(**kwargs: object) -> None:
        delete_calls.append(kwargs)

    # Expose under the ``mlflow.genai`` namespace per the new API.
    genai = types.ModuleType("mlflow.genai")
    genai.register_prompt = register_prompt  # type: ignore[attr-defined]
    genai.set_prompt_alias = set_prompt_alias  # type: ignore[attr-defined]
    genai.delete_prompt_version = delete_prompt_version  # type: ignore[attr-defined]
    stub.genai = genai  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "mlflow", stub)
    monkeypatch.setitem(sys.modules, "mlflow.genai", genai)

    promoter = MLflowPromoter()
    with pytest.raises(PromoterError, match=r"alias rotation"):
        promoter.promote(_request())

    assert len(register_calls) == 1
    assert delete_calls == []


def test_mlflow_promoter_captures_exact_previous_live_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The promoter records the version that was actually live on the alias
    before rotating — NOT ``version_after - 1``. Here the alias points at v2
    while the new version is v6 (intermediate versions never rotated), so the
    exact previous-live is 2, and 5 would be wrong."""
    stub = types.ModuleType("mlflow")

    def load_prompt(ref: str, **_kwargs: object) -> object:
        assert ref == "prompts:/support-agent@prod"
        return types.SimpleNamespace(version=2)

    def register_prompt(**kwargs: object) -> object:
        return types.SimpleNamespace(version=6, uri="prompts:/support-agent/6")

    alias_calls: list[dict[str, object]] = []

    def set_prompt_alias(**kwargs: object) -> None:
        alias_calls.append(kwargs)

    genai = types.ModuleType("mlflow.genai")
    genai.load_prompt = load_prompt  # type: ignore[attr-defined]
    genai.register_prompt = register_prompt  # type: ignore[attr-defined]
    genai.set_prompt_alias = set_prompt_alias  # type: ignore[attr-defined]
    stub.genai = genai  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlflow", stub)
    monkeypatch.setitem(sys.modules, "mlflow.genai", genai)

    result = MLflowPromoter().promote(_request())
    assert result.details["version"] == 6
    assert result.details["version_before"] == 2  # exact, not 6 - 1 = 5
    assert alias_calls == [{"name": "support-agent", "alias": "prod", "version": 6}]


def test_mlflow_promoter_apply_path_commits_intent_before_alias_effect(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    stub = types.ModuleType("mlflow")

    class _StubVersion:
        version = 7
        uri = "prompts:/support-agent/7"

    def load_prompt(_ref: str, **_kwargs: object) -> object:
        return types.SimpleNamespace(version=4)

    def register_prompt(**_kwargs: object) -> _StubVersion:
        return _StubVersion()

    def set_prompt_alias(**_kwargs: object) -> None:
        operation = db_session.query(CaliberReleaseOperation).one()
        assert operation.status == "applying"
        assert operation.version_before == 4
        assert operation.version_after == 7

    genai = types.ModuleType("mlflow.genai")
    genai.load_prompt = load_prompt  # type: ignore[attr-defined]
    genai.register_prompt = register_prompt  # type: ignore[attr-defined]
    genai.set_prompt_alias = set_prompt_alias  # type: ignore[attr-defined]
    stub.genai = genai  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlflow", stub)
    monkeypatch.setitem(sys.modules, "mlflow.genai", genai)

    result = MLflowPromoter().promote(_request(session=db_session, actor="@operator"))

    db_session.expire_all()
    operation = db_session.query(CaliberReleaseOperation).one()
    assert operation.status == "applied"
    assert operation.approval_id == "AP-1"
    assert result.details["version"] == 7


def test_mlflow_promoter_rollback_commits_intent_before_alias_effect(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    stub = types.ModuleType("mlflow")

    def load_prompt(_ref: str, **_kwargs: object) -> object:
        return types.SimpleNamespace(version=7)

    def set_prompt_alias(**_kwargs: object) -> None:
        operation = db_session.query(CaliberReleaseOperation).one()
        assert operation.status == "applying"
        assert operation.operation_type == "rollback"
        assert operation.version_before == 7
        assert operation.version_after == 4

    genai = types.ModuleType("mlflow.genai")
    genai.load_prompt = load_prompt  # type: ignore[attr-defined]
    genai.set_prompt_alias = set_prompt_alias  # type: ignore[attr-defined]
    stub.genai = genai  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlflow", stub)
    monkeypatch.setitem(sys.modules, "mlflow.genai", genai)

    result = MLflowPromoter().rollback(
        RollbackRequest(
            agent_id="support-agent",
            artifact_type="prompt",
            version_before=4,
            checkpoint_id="CK-1",
            session=db_session,
            actor="@operator",
        )
    )

    db_session.expire_all()
    operation = db_session.query(CaliberReleaseOperation).one()
    assert operation.status == "applied"
    assert result.details["operation_id"] == operation.operation_id


def test_build_checkpoint_prefers_exact_version_before_over_subtraction() -> None:
    """``apply._build_checkpoint`` uses the promoter-reported ``version_before``
    when present, falling back to ``version_after - 1`` only when it's absent."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from caliber.apply import _build_checkpoint

    approval = SimpleNamespace(approval_id="AP-9", agent_id="support-agent")
    candidate = {"artifact_type": "prompt"}

    exact = _build_checkpoint(
        approval,  # type: ignore[arg-type]
        candidate,
        PromotionResult(
            artifact_ref="prompts:/support-agent/6",
            rotated_at=datetime.now(timezone.utc),
            details={"version": 6, "version_before": 2},
        ),
    )
    assert exact.version_before == 2  # exact, not 5
    assert exact.artifact_ref_before == "prompts:/support-agent/2"

    # Backward-compat: a promoter that doesn't report version_before falls back.
    fallback = _build_checkpoint(
        approval,  # type: ignore[arg-type]
        candidate,
        PromotionResult(
            artifact_ref="prompts:/support-agent/6",
            rotated_at=datetime.now(timezone.utc),
            details={"version": 6},
        ),
    )
    assert fallback.version_before == 5


def test_mlflow_promoter_alias_failure_requires_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The surfaced error identifies the target and reconciliation obligation."""
    stub = types.ModuleType("mlflow")

    class _StubVersion:
        version = 3
        uri = "prompts:/support-agent/3"

    def register_prompt(**kwargs: object) -> _StubVersion:
        return _StubVersion()

    def set_prompt_alias(**kwargs: object) -> None:
        raise RuntimeError("alias boom")

    def delete_prompt_version(**kwargs: object) -> None:
        raise RuntimeError("delete boom")

    genai = types.ModuleType("mlflow.genai")
    genai.register_prompt = register_prompt  # type: ignore[attr-defined]
    genai.set_prompt_alias = set_prompt_alias  # type: ignore[attr-defined]
    genai.delete_prompt_version = delete_prompt_version  # type: ignore[attr-defined]
    stub.genai = genai  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlflow", stub)
    monkeypatch.setitem(sys.modules, "mlflow.genai", genai)

    promoter = MLflowPromoter()
    with pytest.raises(PromoterError, match=r"version 3.*needs reconciliation"):
        promoter.promote(_request())
