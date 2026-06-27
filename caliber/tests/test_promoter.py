"""Tests for the Promoter abstraction."""

from __future__ import annotations

import sys
import types

import pytest

from caliber.promoter import (
    CompositePromoter,
    FakePromoter,
    MLflowPromoter,
    PromoterError,
    PromotionRequest,
    PromotionResult,
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


def test_mlflow_promoter_deletes_orphan_version_on_alias_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 1 audit (#3): if ``register_prompt`` succeeds but
    ``set_prompt_alias`` fails, the orphan version must be deleted —
    otherwise a retried approval stacks duplicate versions tagged
    with the same ``approval_id``."""
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
    # Orphan cleanup fired with the version that ``register_prompt``
    # produced. Without this, a retry would stack a v8, v9, v10, etc.
    assert delete_calls == [{"name": "support-agent", "version": 7}]


def test_mlflow_promoter_alias_failure_message_includes_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the cleanup-delete itself fails, the operator must see
    the failing version number in the error so they can manually
    reconcile."""
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
    with pytest.raises(PromoterError, match=r"Cleanup of v3 also failed"):
        promoter.promote(_request())
