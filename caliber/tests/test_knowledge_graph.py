from __future__ import annotations

import caliber.knowledge.graph as knowledge_graph
from caliber.knowledge.graph import build_graph_bundle


def _chunk(content: str, *, chunk_id: str = "chunk-1") -> dict[str, object]:
    return {
        "chunk_id": chunk_id,
        "document_id": "doc-1",
        "source_key": "guide.md",
        "content": content,
        "metadata": {
            "headers": [
                {"title": "Product Guide"},
                {"title": "Alert Routing"},
            ]
        },
    }


def test_build_graph_bundle_spacy_falls_back_to_heuristic(monkeypatch) -> None:
    def _raise(_model_name: str):
        raise OSError("spaCy model 'missing-model' is not installed")

    monkeypatch.setattr(knowledge_graph, "_load_spacy_model", _raise)

    bundle = build_graph_bundle(
        [_chunk("Caliber Platform links Alert Router to Notifier Service.")],
        backend="spacy",
        spacy_model="missing-model",
    )

    assert bundle.metadata["requested_backend"] == "spacy"
    assert bundle.metadata["applied_backend"] == "heuristic"
    assert "missing-model" in str(bundle.metadata["fallback_reason"])
    assert bundle.entities
    assert bundle.graph["metadata"]["applied_backend"] == "heuristic"


def test_build_graph_bundle_uses_spacy_entities_when_available(monkeypatch) -> None:
    class _FakeEntity:
        def __init__(self, text: str, label: str) -> None:
            self.text = text
            self.label_ = label

    class _FakeDoc:
        def __init__(self) -> None:
            self.ents = [
                _FakeEntity("Notifier Service", "ORG"),
                _FakeEntity("Agent Fleet", "ORG"),
            ]

    class _FakeNlp:
        def __call__(self, _text: str) -> _FakeDoc:
            return _FakeDoc()

    monkeypatch.setattr(knowledge_graph, "_load_spacy_model", lambda _model_name: _FakeNlp())

    bundle = build_graph_bundle(
        [_chunk("review queue escalates incidents through notifier service")],
        backend="spacy",
        spacy_model="fake-model",
    )

    labels = {entity.label for entity in bundle.entities}
    assert bundle.metadata["applied_backend"] == "spacy"
    assert {"Notifier Service", "Agent Fleet"} <= labels
