"""Entity and relationship extraction for versioned knowledge bases."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations
from typing import Any, Literal

GraphExtractorBackend = Literal["heuristic", "spacy"]

GRAPH_ENTITY_TYPE_SPECS: tuple[tuple[str, str, str], ...] = (
    ("heading", "Headings", "Promote section headings into reusable graph entities."),
    (
        "phrase",
        "Title-case phrases",
        "Capture capitalized concepts and named phrases from source text.",
    ),
    ("acronym", "Acronyms", "Retain uppercase system or product abbreviations."),
    ("organization", "Organizations", "spaCy-detected organizations and institutions."),
    ("person", "People", "spaCy-detected people and role names."),
    ("location", "Locations", "spaCy-detected places, facilities, and geographies."),
    ("product", "Products", "spaCy-detected products, artifacts, and work outputs."),
    ("event", "Events", "spaCy-detected events and milestones."),
    ("group", "Groups", "spaCy-detected groups, communities, and affiliations."),
    ("concept", "Concepts", "spaCy-detected laws, policies, and abstract concepts."),
    ("language", "Languages", "spaCy-detected language names."),
    ("artifact", "Artifacts", "spaCy-detected work products and named artifacts."),
    ("date", "Dates", "spaCy-detected dates and time ranges."),
    ("time", "Times", "spaCy-detected times and durations."),
    ("quantity", "Quantities", "spaCy-detected quantities, percentages, and money values."),
    (
        "named_entity",
        "Named entities",
        "Fallback bucket for spaCy entities without a mapped subtype.",
    ),
)
GRAPH_ENTITY_TYPE_IDS = frozenset(item[0] for item in GRAPH_ENTITY_TYPE_SPECS)

_ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,}\b")
_TITLE_CASE_RE = re.compile(
    r"\b(?:[A-Z][a-z0-9]+(?:[-/][A-Z][a-z0-9]+)?)(?:\s+(?:[A-Z][a-z0-9]+(?:[-/][A-Z][a-z0-9]+)?)){0,4}\b"
)
_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
_MAX_TITLE_CASE_WORD_GAPS = 4
_MIN_ENTITY_KEY_LEN = 2
_STOPWORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "build",
    "by",
    "for",
    "from",
    "how",
    "in",
    "into",
    "is",
    "it",
    "its",
    "of",
    "on",
    "or",
    "that",
    "the",
    "their",
    "this",
    "to",
    "under",
    "version",
    "with",
}
_MAX_ENTITIES_PER_CHUNK = 12
_SPACY_LABEL_TYPES = {
    "CARDINAL": "quantity",
    "DATE": "date",
    "EVENT": "event",
    "FAC": "location",
    "GPE": "location",
    "LANGUAGE": "language",
    "LAW": "concept",
    "LOC": "location",
    "MONEY": "quantity",
    "NORP": "group",
    "ORG": "organization",
    "PERCENT": "quantity",
    "PERSON": "person",
    "PRODUCT": "product",
    "QUANTITY": "quantity",
    "TIME": "time",
    "WORK_OF_ART": "artifact",
}


@dataclass(frozen=True)
class KnowledgeGraphEntity:
    entity_key: str
    label: str
    entity_type: str
    aliases: list[str]
    mention_count: int
    source_documents: list[str]
    source_keys: list[str]
    source_chunks: list[str]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class KnowledgeGraphRelationship:
    source_entity_key: str
    target_entity_key: str
    relationship_type: str
    weight: float
    evidence_chunk_ids: list[str]
    source_documents: list[str]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class KnowledgeGraphBundle:
    entities: list[KnowledgeGraphEntity]
    relationships: list[KnowledgeGraphRelationship]
    graph: dict[str, Any]
    metadata: dict[str, Any]


@dataclass
class _EntityAggregate:
    label: str
    type_counts: Counter[str]
    aliases: set[str]
    source_documents: set[str]
    source_keys: set[str]
    source_chunks: set[str]
    heading_paths: set[str]
    mention_count: int = 0


@dataclass
class _RelationshipAggregate:
    evidence_chunk_ids: set[str]
    source_documents: set[str]
    weight: float = 0.0


@dataclass(frozen=True)
class _ExtractorResolution:
    requested_backend: GraphExtractorBackend
    applied_backend: GraphExtractorBackend
    spacy_model: str | None
    fallback_reason: str | None
    candidate_builder: Callable[[str, dict[str, Any]], dict[str, tuple[str, str, int, str | None]]]


def build_graph_bundle(
    chunk_exports: list[dict[str, Any]],
    *,
    backend: GraphExtractorBackend = "heuristic",
    spacy_model: str = "en_core_web_sm",
    max_entities_per_chunk: int = _MAX_ENTITIES_PER_CHUNK,
    entity_types: set[str] | None = None,
    minimum_entity_mentions: int = 1,
    minimum_relationship_weight: float = 1.0,
) -> KnowledgeGraphBundle:
    """Extract entities plus co-occurrence relationships from chunk exports."""

    resolution = _resolve_candidate_builder(backend=backend, spacy_model=spacy_model)
    entity_map: dict[str, _EntityAggregate] = {}
    relationship_map: dict[tuple[str, str], _RelationshipAggregate] = {}
    allowed_entity_types = {item.strip().lower() for item in (entity_types or set()) if item}
    min_mentions = max(1, int(minimum_entity_mentions))
    min_relationship_weight = max(0.0, float(minimum_relationship_weight))

    for chunk in chunk_exports:
        chunk_id = str(chunk.get("chunk_id") or "")
        document_id = str(chunk.get("document_id") or "")
        source_key = str(chunk.get("source_key") or "")
        metadata = chunk.get("metadata")
        text = str(chunk.get("content") or "")
        candidates = resolution.candidate_builder(
            text, metadata if isinstance(metadata, dict) else {}
        )
        ranked = sorted(
            candidates.items(),
            key=lambda item: (item[1][2], len(item[1][0])),
            reverse=True,
        )[:max_entities_per_chunk]
        chunk_entity_keys: list[str] = []
        for entity_key, (label, entity_type, score, heading_path) in ranked:
            if allowed_entity_types and entity_type.lower() not in allowed_entity_types:
                continue
            aggregate = entity_map.get(entity_key)
            if aggregate is None:
                aggregate = _EntityAggregate(
                    label=label,
                    type_counts=Counter(),
                    aliases=set(),
                    source_documents=set(),
                    source_keys=set(),
                    source_chunks=set(),
                    heading_paths=set(),
                )
                entity_map[entity_key] = aggregate
            aggregate.mention_count += max(1, int(score))
            aggregate.type_counts[entity_type] += 1
            aggregate.aliases.add(label)
            aggregate.source_documents.add(document_id)
            aggregate.source_keys.add(source_key)
            aggregate.source_chunks.add(chunk_id)
            if heading_path:
                aggregate.heading_paths.add(heading_path)
            if len(label) > len(aggregate.label):
                aggregate.label = label
            chunk_entity_keys.append(entity_key)

        for left, right in combinations(sorted(set(chunk_entity_keys)), 2):
            pair = (left, right)
            rel = relationship_map.get(pair)
            if rel is None:
                rel = _RelationshipAggregate(evidence_chunk_ids=set(), source_documents=set())
                relationship_map[pair] = rel
            rel.weight += 1.0
            if chunk_id:
                rel.evidence_chunk_ids.add(chunk_id)
            if document_id:
                rel.source_documents.add(document_id)

    entities = [
        KnowledgeGraphEntity(
            entity_key=entity_key,
            label=aggregate.label,
            entity_type=aggregate.type_counts.most_common(1)[0][0],
            aliases=sorted(aggregate.aliases),
            mention_count=aggregate.mention_count,
            source_documents=sorted(filter(None, aggregate.source_documents)),
            source_keys=sorted(filter(None, aggregate.source_keys)),
            source_chunks=sorted(filter(None, aggregate.source_chunks)),
            metadata={
                "heading_paths": sorted(aggregate.heading_paths),
                "variant_count": len(aggregate.aliases),
            },
        )
        for entity_key, aggregate in entity_map.items()
        if aggregate.mention_count >= min_mentions
    ]
    entities.sort(key=lambda item: (-item.mention_count, item.label.lower(), item.entity_key))
    entity_lookup = {entity.entity_key: entity for entity in entities}

    relationships = [
        KnowledgeGraphRelationship(
            source_entity_key=left,
            target_entity_key=right,
            relationship_type="co_occurs",
            weight=aggregate.weight,
            evidence_chunk_ids=sorted(filter(None, aggregate.evidence_chunk_ids)),
            source_documents=sorted(filter(None, aggregate.source_documents)),
            metadata={
                "source_label": entity_lookup[left].label,
                "target_label": entity_lookup[right].label,
                "evidence_count": len(aggregate.evidence_chunk_ids),
            },
        )
        for (left, right), aggregate in relationship_map.items()
        if left in entity_lookup
        and right in entity_lookup
        and aggregate.weight >= min_relationship_weight
    ]
    relationships.sort(
        key=lambda item: (-item.weight, item.source_entity_key, item.target_entity_key)
    )

    graph_metadata = {
        "requested_backend": resolution.requested_backend,
        "applied_backend": resolution.applied_backend,
        "spacy_model": resolution.spacy_model,
        "fallback_reason": resolution.fallback_reason,
        "max_entities_per_chunk": max_entities_per_chunk,
        "entity_types": sorted(allowed_entity_types),
        "minimum_entity_mentions": min_mentions,
        "minimum_relationship_weight": min_relationship_weight,
    }
    graph = {
        "format": "caliber-knowledge-graph/v1",
        "entity_count": len(entities),
        "relationship_count": len(relationships),
        "metadata": graph_metadata,
        "nodes": [
            {
                "entity_key": entity.entity_key,
                "label": entity.label,
                "entity_type": entity.entity_type,
                "mention_count": entity.mention_count,
                "source_documents": entity.source_documents,
                "source_keys": entity.source_keys,
            }
            for entity in entities
        ],
        "edges": [
            {
                "source_entity_key": relationship.source_entity_key,
                "target_entity_key": relationship.target_entity_key,
                "relationship_type": relationship.relationship_type,
                "weight": relationship.weight,
                "evidence_chunk_ids": relationship.evidence_chunk_ids,
            }
            for relationship in relationships
        ],
    }
    return KnowledgeGraphBundle(
        entities=entities,
        relationships=relationships,
        graph=graph,
        metadata=graph_metadata,
    )


def entity_key_for_label(label: str) -> str | None:
    """Normalize a display label into a stable entity key."""

    return _entity_key(label)


def _resolve_candidate_builder(
    *,
    backend: GraphExtractorBackend,
    spacy_model: str,
) -> _ExtractorResolution:
    if backend != "spacy":
        return _ExtractorResolution(
            requested_backend=backend,
            applied_backend="heuristic",
            spacy_model=None,
            fallback_reason=None,
            candidate_builder=_chunk_candidates_heuristic,
        )
    try:
        nlp = _load_spacy_model(spacy_model)
    except (ImportError, OSError) as exc:
        return _ExtractorResolution(
            requested_backend="spacy",
            applied_backend="heuristic",
            spacy_model=spacy_model,
            fallback_reason=str(exc),
            candidate_builder=_chunk_candidates_heuristic,
        )
    return _ExtractorResolution(
        requested_backend="spacy",
        applied_backend="spacy",
        spacy_model=spacy_model,
        fallback_reason=None,
        candidate_builder=lambda text, metadata: _chunk_candidates_spacy(text, metadata, nlp),
    )


@lru_cache(maxsize=4)
def _load_spacy_model(model_name: str) -> Any:
    try:
        import spacy  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise ImportError(
            "spaCy graph extraction needs the optional 'spacy' package. "
            "Install caliber-suite[knowledge] "
            "and the desired language model."
        ) from exc
    try:
        return spacy.load(model_name)
    except OSError as exc:  # pragma: no cover - depends on local model install
        raise OSError(
            f"spaCy model {model_name!r} is not installed. Run 'python -m spacy download {model_name}'."
        ) from exc


def _chunk_candidates_heuristic(
    text: str,
    metadata: dict[str, Any],
) -> dict[str, tuple[str, str, int, str | None]]:
    by_key: dict[str, tuple[str, str, int, str | None]] = {}
    for label, entity_type, score, heading_path in _heading_candidates(metadata):
        _merge_candidate(by_key, label, entity_type, score, heading_path)
    for label in _ACRONYM_RE.findall(text):
        _merge_candidate(by_key, label, "acronym", 3, None)
    for label in _TITLE_CASE_RE.findall(text):
        if label.count(" ") > _MAX_TITLE_CASE_WORD_GAPS:
            continue
        _merge_candidate(by_key, label, "phrase", 2, None)
    return by_key


def _chunk_candidates_spacy(
    text: str,
    metadata: dict[str, Any],
    nlp: Any,
) -> dict[str, tuple[str, str, int, str | None]]:
    by_key = _chunk_candidates_heuristic(text, metadata)
    if not text.strip():
        return by_key
    doc = nlp(text)
    for entity in getattr(doc, "ents", ()):
        label = " ".join(str(getattr(entity, "text", "")).strip().split())
        if not label:
            continue
        entity_type = _SPACY_LABEL_TYPES.get(
            str(getattr(entity, "label_", "")).upper(), "named_entity"
        )
        _merge_candidate(by_key, label, entity_type, 5, None)
    return by_key


def _heading_candidates(metadata: dict[str, Any]) -> list[tuple[str, str, int, str | None]]:
    headers = metadata.get("headers")
    if not isinstance(headers, list):
        return []
    parts: list[str] = []
    out: list[tuple[str, str, int, str | None]] = []
    for item in headers:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        parts.append(title)
        out.append((title, "heading", 4, " > ".join(parts)))
    return out


def _merge_candidate(
    by_key: dict[str, tuple[str, str, int, str | None]],
    label: str,
    entity_type: str,
    score: int,
    heading_path: str | None,
) -> None:
    cleaned = " ".join(label.strip().split())
    if not cleaned:
        return
    entity_key = _entity_key(cleaned)
    if not entity_key:
        return
    existing = by_key.get(entity_key)
    if existing is None or score > existing[2] or len(cleaned) > len(existing[0]):
        by_key[entity_key] = (cleaned, entity_type, score, heading_path)


def _entity_key(label: str) -> str | None:
    words = [word for word in re.split(r"\s+", label.strip()) if word]
    if not words:
        return None
    lowered = [word.lower() for word in words]
    if all(word in _STOPWORDS for word in lowered):
        return None
    joined = _NORMALIZE_RE.sub("-", " ".join(lowered)).strip("-")
    if len(joined) < _MIN_ENTITY_KEY_LEN:
        return None
    if joined in _STOPWORDS:
        return None
    return joined
