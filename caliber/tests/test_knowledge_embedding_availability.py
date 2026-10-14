"""The knowledge-base options say whether an embedding model can actually run (alpha gap P1-23).

``GET /knowledge-bases/options`` reported every embedding model as ``available: true`` by
checking only for flagged dependency versions. On an install without ``sentence-transformers``
the model list looked fine, creation returned 201, and the build later failed. The options now
also look for the packages that run a local model, and creation fails fast when they are
missing.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable

import pytest
from starlette.testclient import TestClient

import caliber.knowledge.embeddings as knowledge_embeddings
import caliber.knowledge.service as knowledge_service
from caliber import runtime_advisories
from caliber.knowledge.embeddings import KnowledgeDependencyError
from caliber.runtime_advisories import (
    LOCAL_EMBEDDING_EXTRA,
    local_embedding_missing_reason,
)

OPTIONS = "/ajax-api/2.0/mlflow/caliber/knowledge-bases/options"


def _only_absent(*names: str) -> Callable[[str], object | None]:
    """A ``find_spec`` that reports ``names`` as not installed and everything else as found."""
    real = importlib.util.find_spec

    def find_spec(name: str, *args: object, **kwargs: object) -> object | None:
        if name in names:
            return None
        return real(name, *args, **kwargs)  # type: ignore[arg-type]

    return find_spec


def test_nothing_is_reported_when_the_packages_are_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime_advisories.importlib.util, "find_spec", lambda name, *a, **k: object()
    )
    assert local_embedding_missing_reason() is None


def test_a_missing_sentence_transformers_names_the_package_and_the_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime_advisories.importlib.util, "find_spec", _only_absent("sentence_transformers")
    )
    reason = local_embedding_missing_reason()
    assert reason is not None
    assert "'sentence-transformers'" in reason
    assert "langchain-huggingface" not in reason  # installed, so not blamed
    assert "is not installed" in reason
    assert LOCAL_EMBEDDING_EXTRA in reason
    assert "knowledge-local" in reason
    assert "pre-cache" in reason


def test_both_missing_packages_are_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runtime_advisories.importlib.util,
        "find_spec",
        _only_absent("sentence_transformers", "langchain_huggingface"),
    )
    reason = local_embedding_missing_reason() or ""
    assert "'sentence-transformers' and 'langchain-huggingface'" in reason
    assert "are not installed" in reason
    assert "packages" in reason


def test_options_mark_every_embedding_model_unavailable_when_a_package_is_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        knowledge_service,
        "local_embedding_missing_reason",
        lambda: "Local embedding models need the optional package 'sentence-transformers'.",
    )
    monkeypatch.setattr(
        knowledge_service, "local_embedding_block_reason", lambda *, allow_flagged=False: None
    )
    response = client.get(OPTIONS)
    assert response.status_code == 200, response.text
    models = response.json()["data"]["embedding_models"]
    assert models
    for model in models:
        assert model["available"] is False, model["id"]
        assert "sentence-transformers" in model["unavailable_reason"]
        # No override can install a package, so it must not be offered as one.
        assert model["requires_override"] is False, model["id"]


def test_a_flagged_install_still_asks_for_the_override(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(knowledge_service, "local_embedding_missing_reason", lambda: None)
    monkeypatch.setattr(
        knowledge_service,
        "local_embedding_block_reason",
        lambda *, allow_flagged=False: "blocked: torch 2.12.0 is flagged",
    )
    models = client.get(OPTIONS).json()["data"]["embedding_models"]
    for model in models:
        assert model["available"] is False
        assert model["unavailable_reason"] == "blocked: torch 2.12.0 is flagged"
        assert model["requires_override"] is True


def test_a_missing_package_outranks_a_flagged_one(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        knowledge_service, "local_embedding_missing_reason", lambda: "not installed"
    )
    monkeypatch.setattr(
        knowledge_service,
        "local_embedding_block_reason",
        lambda *, allow_flagged=False: "flagged",
    )
    models = client.get(OPTIONS).json()["data"]["embedding_models"]
    assert {m["unavailable_reason"] for m in models} == {"not installed"}
    assert not any(m["requires_override"] for m in models)


def test_everything_is_available_when_nothing_is_missing_or_flagged(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(knowledge_service, "local_embedding_missing_reason", lambda: None)
    monkeypatch.setattr(
        knowledge_service, "local_embedding_block_reason", lambda *, allow_flagged=False: None
    )
    models = client.get(OPTIONS).json()["data"]["embedding_models"]
    assert all(m["available"] and m["unavailable_reason"] is None for m in models)
    assert not any(m["requires_override"] for m in models)


def test_creation_fails_fast_with_the_reason_when_a_package_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        knowledge_embeddings, "local_embedding_missing_reason", lambda: "package missing"
    )
    monkeypatch.setattr(
        knowledge_embeddings,
        "local_embedding_block_reason",
        lambda *, allow_flagged=False: pytest.fail("the flagged check must not run first"),
    )
    with pytest.raises(KnowledgeDependencyError, match="package missing"):
        knowledge_embeddings.ensure_embedding_backend_runtime_available()
