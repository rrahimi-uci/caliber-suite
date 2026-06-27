"""Chunking strategies for knowledge-base builds."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from caliber.knowledge.embeddings import HuggingFaceEmbeddingBackend


@dataclass(frozen=True)
class ChunkingStrategySpec:
    """One chunking strategy exposed to the build UI."""

    strategy_id: str
    name: str
    description: str
    defaults: dict[str, object]
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChunkFragment:
    """One chunk produced by a splitter before persistence."""

    content: str
    metadata: dict[str, Any]
    start_index: int | None = None
    end_index: int | None = None


CHUNKING_STRATEGIES: tuple[ChunkingStrategySpec, ...] = (
    ChunkingStrategySpec(
        strategy_id="recursive",
        name="Recursive Character",
        description="Balanced default that preserves paragraphs and recursively backs off to smaller separators.",
        defaults={"chunk_size": 1200, "chunk_overlap": 180},
        tags=("recommended", "general-purpose"),
    ),
    ChunkingStrategySpec(
        strategy_id="semantic",
        name="Semantic",
        description="Builds topical chunks by grouping adjacent text units with similar embeddings.",
        defaults={"chunk_size": 1400, "chunk_overlap": 120, "semantic_similarity_threshold": 0.78},
        tags=("semantic", "retrieval-quality"),
    ),
    ChunkingStrategySpec(
        strategy_id="markdown",
        name="Markdown Aware",
        description="Preserves headings and section lineage before recursively refining oversized sections.",
        defaults={"chunk_size": 1400, "chunk_overlap": 120},
        tags=("markdown", "section-aware"),
    ),
    ChunkingStrategySpec(
        strategy_id="token",
        name="Token Based",
        description="Splits by token count for prompts or corpora that need tighter control over context budgets.",
        defaults={"chunk_size": 300, "chunk_overlap": 40},
        tags=("token-budget", "llm"),
    ),
    ChunkingStrategySpec(
        strategy_id="character",
        name="Character",
        description="Simple fixed-width chunking that is fast and predictable for raw text corpora.",
        defaults={"chunk_size": 1200, "chunk_overlap": 120},
        tags=("fast", "baseline"),
    ),
)

_HEADER_RE = re.compile(r"^(#{1,6})\s+(.*)$", flags=re.MULTILINE)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_TOKEN_RE = re.compile(r"\S+\s*")


def list_chunking_strategies() -> list[ChunkingStrategySpec]:
    return list(CHUNKING_STRATEGIES)


def chunk_text(
    text: str,
    strategy: str,
    config: dict[str, object] | None = None,
    *,
    base_metadata: dict[str, Any] | None = None,
    embedder: HuggingFaceEmbeddingBackend | None = None,
) -> list[ChunkFragment]:
    """Split a text payload using one of the supported strategies."""
    cfg = dict(config or {})
    metadata = dict(base_metadata or {})
    cleaned = text.strip()
    if not cleaned:
        return []

    if strategy == "recursive":
        return _split_recursive(cleaned, cfg, metadata)
    if strategy == "character":
        return _split_character(cleaned, cfg, metadata)
    if strategy == "token":
        return _split_token(cleaned, cfg, metadata)
    if strategy == "markdown":
        return _split_markdown(cleaned, cfg, metadata)
    if strategy == "semantic":
        if embedder is None:
            raise ValueError("semantic chunking requires an embedding backend")
        return _split_semantic(cleaned, cfg, metadata, embedder)
    raise ValueError(f"unknown chunking strategy {strategy!r}")


def _split_recursive(
    text: str, config: dict[str, object], metadata: dict[str, Any]
) -> list[ChunkFragment]:
    chunk_size = _int_setting(config, "chunk_size", 1200, minimum=200, maximum=10000)
    overlap = _int_setting(config, "chunk_overlap", 180, minimum=0, maximum=chunk_size - 1)
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter  # noqa: PLC0415

        splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=overlap,
            add_start_index=True,
        )
        documents = splitter.create_documents([text], metadatas=[metadata])
        return [_doc_to_chunk(doc) for doc in documents if doc.page_content.strip()]
    except Exception:
        return _fixed_window_chunks(text, chunk_size=chunk_size, overlap=overlap, metadata=metadata)


def _split_character(
    text: str, config: dict[str, object], metadata: dict[str, Any]
) -> list[ChunkFragment]:
    chunk_size = _int_setting(config, "chunk_size", 1200, minimum=200, maximum=10000)
    overlap = _int_setting(config, "chunk_overlap", 120, minimum=0, maximum=chunk_size - 1)
    try:
        from langchain_text_splitters import CharacterTextSplitter  # noqa: PLC0415

        splitter = CharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=overlap,
            separator="\n\n",
        )
        documents = splitter.create_documents([text], metadatas=[metadata])
        return [_doc_to_chunk(doc) for doc in documents if doc.page_content.strip()]
    except Exception:
        return _fixed_window_chunks(text, chunk_size=chunk_size, overlap=overlap, metadata=metadata)


def _split_token(
    text: str, config: dict[str, object], metadata: dict[str, Any]
) -> list[ChunkFragment]:
    chunk_size = _int_setting(config, "chunk_size", 300, minimum=50, maximum=4000)
    overlap = _int_setting(config, "chunk_overlap", 40, minimum=0, maximum=chunk_size - 1)
    try:
        from langchain_text_splitters import TokenTextSplitter  # noqa: PLC0415

        splitter = TokenTextSplitter(chunk_size=chunk_size, chunk_overlap=overlap)
        documents = splitter.create_documents([text], metadatas=[metadata])
        return [_doc_to_chunk(doc) for doc in documents if doc.page_content.strip()]
    except Exception:
        return _token_window_chunks(text, chunk_size=chunk_size, overlap=overlap, metadata=metadata)


def _split_markdown(
    text: str, config: dict[str, object], metadata: dict[str, Any]
) -> list[ChunkFragment]:
    chunk_size = _int_setting(config, "chunk_size", 1400, minimum=200, maximum=10000)
    overlap = _int_setting(config, "chunk_overlap", 120, minimum=0, maximum=chunk_size - 1)

    sections = _markdown_sections(text)
    chunks: list[ChunkFragment] = []
    for section in sections:
        section_meta = {**metadata, **section.metadata}
        if len(section.content) <= chunk_size:
            chunks.append(
                ChunkFragment(
                    content=section.content,
                    metadata=section_meta,
                    start_index=section.start_index,
                    end_index=section.end_index,
                )
            )
            continue
        chunks.extend(
            _split_recursive(
                section.content,
                {"chunk_size": chunk_size, "chunk_overlap": overlap},
                section_meta,
            )
        )
    return [chunk for chunk in chunks if chunk.content.strip()]


def _overlap_carry(
    units: list[ChunkFragment], indices: list[int], overlap: int
) -> tuple[list[int], int]:
    """Trailing unit indices of ``indices`` covering ~``overlap`` chars, plus
    their combined length — used to seed the next semantic chunk so consecutive
    chunks actually share boundary text."""
    carried: list[int] = []
    carried_chars = 0
    for idx in reversed(indices):
        unit_len = len(units[idx].content)
        if carried and carried_chars + unit_len > overlap:
            break
        carried.insert(0, idx)
        carried_chars += unit_len
    return carried, carried_chars


def _split_semantic(
    text: str,
    config: dict[str, object],
    metadata: dict[str, Any],
    embedder: HuggingFaceEmbeddingBackend,
) -> list[ChunkFragment]:
    chunk_size = _int_setting(config, "chunk_size", 1400, minimum=300, maximum=12000)
    overlap = _int_setting(config, "chunk_overlap", 120, minimum=0, maximum=chunk_size - 1)
    threshold = _float_setting(
        config, "semantic_similarity_threshold", 0.78, minimum=0.1, maximum=0.99
    )

    units = _semantic_units(text, chunk_size=max(220, chunk_size // 2), metadata=metadata)
    if len(units) <= 1:
        return units

    vectors = embedder.embed_texts([unit.content for unit in units])
    chunks: list[ChunkFragment] = []
    current_indices: list[int] = [0]
    current_chars = len(units[0].content)

    def flush() -> None:
        nonlocal current_indices, current_chars
        if not current_indices:
            return
        selected = [units[index] for index in current_indices]
        start = selected[0].start_index
        end = selected[-1].end_index
        joined = "\n\n".join(item.content for item in selected if item.content.strip())
        if not joined.strip():
            current_indices = []
            current_chars = 0
            return
        merged = dict(metadata)
        headers = [
            item.metadata.get("headers") for item in selected if item.metadata.get("headers")
        ]
        if headers:
            merged["headers"] = headers[-1]
        chunks.append(
            ChunkFragment(content=joined, metadata=merged, start_index=start, end_index=end)
        )
        if overlap > 0 and selected:
            # Carry the trailing unit(s) (covering ~``overlap`` chars) into the
            # next chunk so consecutive semantic chunks actually share boundary
            # text. Previously only ``current_chars`` was seeded (with the tail
            # length) while ``current_indices`` was emptied, so there was NO real
            # overlap and the phantom char budget triggered premature flushes.
            current_indices, current_chars = _overlap_carry(units, current_indices, overlap)
        else:
            current_indices = []
            current_chars = 0

    centroid = list(vectors[0])
    for index in range(1, len(units)):
        similarity = _cosine_similarity(centroid, vectors[index])
        projected = current_chars + len(units[index].content)
        if current_indices and projected > chunk_size and similarity < threshold:
            flush()
            # flush() seeds current_indices/current_chars with the overlap carry
            # (the trailing unit(s)); EXTEND it rather than replace, so the
            # carried tail actually overlaps into this next chunk.
            current_indices.append(index)
            current_chars += len(units[index].content)
            centroid = list(vectors[index])
            continue
        if current_indices and projected > chunk_size * 1.35:
            flush()
            # flush() seeds current_indices/current_chars with the overlap carry
            # (the trailing unit(s)); EXTEND it rather than replace, so the
            # carried tail actually overlaps into this next chunk.
            current_indices.append(index)
            current_chars += len(units[index].content)
            centroid = list(vectors[index])
            continue
        current_indices.append(index)
        current_chars = projected
        centroid = _mean_vectors([vectors[item] for item in current_indices])
    flush()
    return chunks or units


def _fixed_window_chunks(
    text: str,
    *,
    chunk_size: int,
    overlap: int,
    metadata: dict[str, Any],
) -> list[ChunkFragment]:
    if chunk_size <= 0:
        return []
    overlap = min(max(overlap, 0), max(chunk_size - 1, 0))
    step = max(chunk_size - overlap, 1)
    chunks: list[ChunkFragment] = []
    for start in range(0, len(text), step):
        end = min(start + chunk_size, len(text))
        content = text[start:end].strip()
        if content:
            chunks.append(
                ChunkFragment(
                    content=content, metadata=dict(metadata), start_index=start, end_index=end
                )
            )
        if end >= len(text):
            break
    return chunks


def _token_window_chunks(
    text: str,
    *,
    chunk_size: int,
    overlap: int,
    metadata: dict[str, Any],
) -> list[ChunkFragment]:
    tokens = list(_TOKEN_RE.finditer(text))
    if not tokens:
        return []
    chunks: list[ChunkFragment] = []
    step = max(chunk_size - overlap, 1)
    for start_idx in range(0, len(tokens), step):
        end_idx = min(start_idx + chunk_size, len(tokens))
        start = tokens[start_idx].start()
        end = tokens[end_idx - 1].end()
        content = text[start:end].strip()
        if content:
            chunks.append(
                ChunkFragment(
                    content=content, metadata=dict(metadata), start_index=start, end_index=end
                )
            )
        if end_idx >= len(tokens):
            break
    return chunks


def _markdown_sections(text: str) -> list[ChunkFragment]:
    try:
        from langchain_text_splitters import MarkdownHeaderTextSplitter  # noqa: PLC0415

        splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=[
                ("#", "h1"),
                ("##", "h2"),
                ("###", "h3"),
                ("####", "h4"),
            ],
            strip_headers=False,
        )
        documents = splitter.split_text(text)
        return [_doc_to_chunk(doc) for doc in documents if doc.page_content.strip()]
    except Exception:
        return _markdown_sections_fallback(text)

    return _markdown_sections_fallback(text)


def _markdown_sections_fallback(text: str) -> list[ChunkFragment]:
    matches = list(_HEADER_RE.finditer(text))
    if not matches:
        return [ChunkFragment(content=text, metadata={})]

    sections: list[ChunkFragment] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        content = text[start:end].strip()
        if not content:
            continue
        level = len(match.group(1))
        title = match.group(2).strip()
        sections.append(
            ChunkFragment(
                content=content,
                metadata={"headers": [{"level": level, "title": title}]},
                start_index=start,
                end_index=end,
            )
        )
    return sections


def _semantic_units(text: str, *, chunk_size: int, metadata: dict[str, Any]) -> list[ChunkFragment]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if not paragraphs:
        return [
            ChunkFragment(content=text, metadata=dict(metadata), start_index=0, end_index=len(text))
        ]

    fragments: list[ChunkFragment] = []
    cursor = 0
    for paragraph in paragraphs:
        start = text.find(paragraph, cursor)
        if start < 0:
            start = cursor
        end = start + len(paragraph)
        cursor = end
        if len(paragraph) <= chunk_size:
            fragments.append(
                ChunkFragment(
                    content=paragraph, metadata=dict(metadata), start_index=start, end_index=end
                )
            )
            continue
        pieces = [piece.strip() for piece in _SENTENCE_RE.split(paragraph) if piece.strip()]
        if not pieces:
            fragments.append(
                ChunkFragment(
                    content=paragraph, metadata=dict(metadata), start_index=start, end_index=end
                )
            )
            continue
        local_cursor = start
        buffer = ""
        buffer_start = start
        for piece in pieces:
            piece_start = text.find(piece, local_cursor)
            if piece_start < 0:
                piece_start = local_cursor
            piece_end = piece_start + len(piece)
            local_cursor = piece_end
            proposed = f"{buffer} {piece}".strip() if buffer else piece
            if buffer and len(proposed) > chunk_size:
                fragments.append(
                    ChunkFragment(
                        content=buffer.strip(),
                        metadata=dict(metadata),
                        start_index=buffer_start,
                        end_index=piece_start,
                    )
                )
                buffer = piece
                buffer_start = piece_start
                continue
            buffer = proposed
        if buffer.strip():
            fragments.append(
                ChunkFragment(
                    content=buffer.strip(),
                    metadata=dict(metadata),
                    start_index=buffer_start,
                    end_index=end,
                )
            )
    return fragments


def _doc_to_chunk(doc: Any) -> ChunkFragment:
    metadata = dict(getattr(doc, "metadata", {}) or {})
    start = metadata.get("start_index")
    end = None
    if isinstance(start, int):
        end = start + len(getattr(doc, "page_content", ""))
    return ChunkFragment(
        content=str(getattr(doc, "page_content", "")),
        metadata=metadata,
        start_index=start if isinstance(start, int) else None,
        end_index=end,
    )


def _mean_vectors(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    dim = len(vectors[0])
    sums = [0.0] * dim
    for vector in vectors:
        for index, value in enumerate(vector):
            sums[index] += value
    return [value / len(vectors) for value in sums]


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _int_setting(
    config: dict[str, object],
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = config.get(key, default)
    if isinstance(raw, bool):
        value = int(raw)
    elif isinstance(raw, int):
        value = raw
    elif isinstance(raw, float):
        value = int(raw)
    elif isinstance(raw, str):
        try:
            value = int(raw)
        except ValueError:
            return default
    else:
        return default
    return min(max(value, minimum), maximum)


def _float_setting(
    config: dict[str, object],
    key: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw = config.get(key, default)
    if isinstance(raw, (bool, int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw)
        except ValueError:
            return default
    else:
        return default
    return min(max(value, minimum), maximum)
