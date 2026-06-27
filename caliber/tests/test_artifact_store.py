"""Tests for the ArtifactStore abstraction."""

from __future__ import annotations

import builtins
import sys
from types import SimpleNamespace

import pytest

from caliber.artifact_store import FakeArtifactStore, MLflowArtifactStore, build_store


def test_fake_returns_none_for_unknown_agent() -> None:
    store = FakeArtifactStore()
    assert store.get_active_prompt("support-agent") is None


def test_fake_returns_stored_prompt() -> None:
    store = FakeArtifactStore({"support-agent": "you are helpful"})
    assert store.get_active_prompt("support-agent") == "you are helpful"


def test_fake_set_overrides() -> None:
    store = FakeArtifactStore({"support-agent": "v1"})
    store.set("support-agent", "v2")
    assert store.get_active_prompt("support-agent") == "v2"


def test_build_store_fake() -> None:
    assert isinstance(build_store("fake"), FakeArtifactStore)


def test_build_store_mlflow() -> None:
    assert isinstance(build_store("MLFLOW"), MLflowArtifactStore)


def test_build_store_unknown_raises() -> None:
    with pytest.raises(ValueError, match=r"unknown artifact_store_provider"):
        build_store("bogus")


def test_mlflow_store_returns_none_when_mlflow_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def _missing_mlflow(name, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "mlflow":
            raise ImportError("no mlflow")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _missing_mlflow)
    assert MLflowArtifactStore().get_active_prompt("support-agent") is None


def test_mlflow_store_returns_none_without_load_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "mlflow", SimpleNamespace(genai=SimpleNamespace()))
    assert MLflowArtifactStore().get_active_prompt("support-agent") is None


def test_mlflow_store_prefers_genai_load_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def _load_prompt(ref: str, *, allow_missing: bool):
        calls.append(ref)
        assert allow_missing is True
        return SimpleNamespace(template="prompt text")

    monkeypatch.setitem(
        sys.modules,
        "mlflow",
        SimpleNamespace(genai=SimpleNamespace(load_prompt=_load_prompt)),
    )
    store = MLflowArtifactStore(alias="staging")
    assert store.get_active_prompt("support-agent") == "prompt text"
    assert calls == ["prompts:/support-agent@staging"]


def test_mlflow_store_falls_back_to_content_attr(monkeypatch: pytest.MonkeyPatch) -> None:
    def _load_prompt(_ref: str, *, allow_missing: bool):
        assert allow_missing is True
        return SimpleNamespace(content="legacy content")

    monkeypatch.setitem(sys.modules, "mlflow", SimpleNamespace(load_prompt=_load_prompt))
    assert MLflowArtifactStore().get_active_prompt("support-agent") == "legacy content"


def test_mlflow_store_handles_missing_exception_and_bad_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raises(_ref: str, *, allow_missing: bool):
        raise RuntimeError("registry down")

    monkeypatch.setitem(sys.modules, "mlflow", SimpleNamespace(load_prompt=_raises))
    assert MLflowArtifactStore().get_active_prompt("support-agent") is None

    def _bad_content(_ref: str, *, allow_missing: bool):
        return SimpleNamespace(template={"not": "text"})

    monkeypatch.setitem(sys.modules, "mlflow", SimpleNamespace(load_prompt=_bad_content))
    assert MLflowArtifactStore().get_active_prompt("support-agent") is None


def test_mlflow_store_returns_none_for_skill_without_session_factory() -> None:
    assert MLflowArtifactStore().get_active_skill("triage") is None


def test_mlflow_store_reads_active_skill_from_session_factory() -> None:
    skill = SimpleNamespace(name="triage", status="active", content="skill body")

    class _Query:
        def filter(self, *args: object) -> _Query:
            return self

        def first(self) -> object:
            return skill

    class _Session:
        def __enter__(self) -> _Session:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def query(self, *args: object) -> _Query:
            return _Query()

    assert (
        MLflowArtifactStore(session_factory=lambda: _Session()).get_active_skill("triage")
        == "skill body"
    )


def test_mlflow_store_returns_none_when_skill_session_fails() -> None:
    class _BadSession:
        def __enter__(self) -> _BadSession:
            raise RuntimeError("db unavailable")

        def __exit__(self, *args: object) -> None:
            return None

    assert (
        MLflowArtifactStore(session_factory=lambda: _BadSession()).get_active_skill("triage")
        is None
    )
