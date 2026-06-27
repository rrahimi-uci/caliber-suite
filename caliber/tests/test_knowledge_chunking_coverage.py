"""Deterministic coverage for caliber.knowledge.chunking.

Exercises every chunking strategy, the library-import fallback windows, the
semantic merge/flush internals, the markdown/semantic-unit helpers and the
small scalar-setting / vector helpers. No DB and no network: the only external
boundary (the embedding backend, and the optional ``langchain_text_splitters``
import) is faked or monkeypatched.
"""

from __future__ import annotations

import sys

import pytest

from caliber.knowledge.chunking import (
    ChunkFragment,
    _cosine_similarity,
    _fixed_window_chunks,
    _float_setting,
    _int_setting,
    _markdown_sections_fallback,
    _mean_vectors,
    _semantic_units,
    _split_recursive,
    _token_window_chunks,
    chunk_text,
    list_chunking_strategies,
)


class _OneHotEmbedder:
    """Returns an orthonormal one-hot vector per text → low cross-similarity.

    Adjacent units are maximally dissimilar (cosine 0), so the similarity-based
    flush branch fires as soon as the projected character budget is exceeded.
    """

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        size = len(texts)
        out: list[list[float]] = []
        for index in range(size):
            vector = [0.0] * max(size, 1)
            vector[index] = 1.0
            out.append(vector)
        return out


class _ConstantEmbedder:
    """Every text maps to the same vector → similarity is always 1.0.

    With a high threshold the similarity-based flush never triggers, so only the
    hard character-cap flush (``projected > chunk_size * 1.35``) can split.
    """

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


def _force_no_langchain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the inner ``from langchain_text_splitters import ...`` raise.

    Setting the module entry to ``None`` makes the import machinery raise
    ``ModuleNotFoundError`` (caught by the broad ``except`` in each splitter),
    routing execution into the pure-python fallback windows.
    """

    monkeypatch.setitem(sys.modules, "langchain_text_splitters", None)


# --------------------------------------------------------------------------- #
# chunk_text dispatch + guard clauses (lines 94, 106, 108)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("blank", ["", "   ", "\n\t  \n"])
def test_chunk_text_blank_input_returns_empty(blank: str) -> None:
    assert chunk_text(blank, "recursive") == []
    assert chunk_text(blank, "semantic") == []  # short-circuits before embedder check


def test_chunk_text_semantic_without_embedder_raises() -> None:
    with pytest.raises(ValueError, match="requires an embedding backend"):
        chunk_text("some real content here", "semantic", {})


def test_chunk_text_unknown_strategy_raises() -> None:
    with pytest.raises(ValueError, match="unknown chunking strategy 'nope'"):
        chunk_text("content", "nope")


# --------------------------------------------------------------------------- #
# Happy path through the real langchain splitters (lines 124-125, 143-144,
# 158-159, 334-335 + _doc_to_chunk)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("strategy", ["recursive", "character", "token", "markdown"])
def test_chunk_text_real_splitters_produce_clean_fragments(strategy: str) -> None:
    text = (
        "# Heading\n\n"
        "First paragraph with enough words to matter for the splitter logic.\n\n"
        "## Subsection\n\n"
        "Second paragraph that also carries a little weight in the document.\n\n"
        "Third paragraph rounding out the body of the source document nicely."
    )
    fragments = chunk_text(
        text,
        strategy,
        {"chunk_size": 60, "chunk_overlap": 10},
        base_metadata={"document_id": "doc-1"},
    )
    assert fragments
    assert all(frag.content.strip() for frag in fragments)
    assert all(frag.metadata.get("document_id") == "doc-1" for frag in fragments)


# --------------------------------------------------------------------------- #
# Library-import fallbacks (lines 126-127, 145-146, 160-161) which in turn
# exercise _fixed_window_chunks (274-290) and _token_window_chunks (300-318).
# --------------------------------------------------------------------------- #


def test_recursive_falls_back_to_fixed_window(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_no_langchain(monkeypatch)
    text = "abcdefghij" * 60  # 600 chars, no separators -> fixed window
    # chunk_size requests below the recursive minimum (200) are clamped up to 200.
    fragments = chunk_text(text, "recursive", {"chunk_size": 100, "chunk_overlap": 20})
    assert len(fragments) >= 2
    # overlap < chunk_size so windows advance and cover the tail
    assert fragments[0].start_index == 0
    assert fragments[-1].end_index == len(text)
    assert all(len(f.content) <= 200 for f in fragments)  # clamped chunk_size


def test_character_falls_back_to_fixed_window(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_no_langchain(monkeypatch)
    text = "x" * 250
    fragments = chunk_text(text, "character", {"chunk_size": 200, "chunk_overlap": 0})
    assert fragments
    assert all(f.metadata == {} or "document_id" not in f.metadata for f in fragments)


def test_token_falls_back_to_token_window(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_no_langchain(monkeypatch)
    text = " ".join(f"word{i}" for i in range(120))
    fragments = chunk_text(text, "token", {"chunk_size": 50, "chunk_overlap": 5})
    assert len(fragments) >= 2
    assert fragments[0].start_index == 0
    assert fragments[-1].end_index <= len(text)


# --------------------------------------------------------------------------- #
# _fixed_window_chunks edge cases (lines 274-290)
# --------------------------------------------------------------------------- #


def test_fixed_window_non_positive_chunk_size_returns_empty() -> None:
    assert _fixed_window_chunks("abc", chunk_size=0, overlap=0, metadata={}) == []
    assert _fixed_window_chunks("abc", chunk_size=-5, overlap=0, metadata={}) == []


def test_fixed_window_single_pass_breaks_on_end() -> None:
    # whole text fits in one window; loop must break after the first iteration
    fragments = _fixed_window_chunks(
        "hello world", chunk_size=100, overlap=0, metadata={"k": "v"}
    )
    assert len(fragments) == 1
    assert fragments[0].content == "hello world"
    assert fragments[0].metadata == {"k": "v"}
    assert fragments[0].start_index == 0
    assert fragments[0].end_index == len("hello world")


def test_fixed_window_skips_whitespace_only_slice() -> None:
    # A slice that is purely whitespace must not become a fragment.
    text = "AAAAA" + (" " * 10) + "BBBBB"
    fragments = _fixed_window_chunks(text, chunk_size=5, overlap=0, metadata={})
    assert all(f.content.strip() for f in fragments)
    assert [f.content for f in fragments] == ["AAAAA", "BBBBB"]


# --------------------------------------------------------------------------- #
# _token_window_chunks edge cases (lines 300-318)
# --------------------------------------------------------------------------- #


def test_token_window_empty_text_returns_empty() -> None:
    assert _token_window_chunks("   ", chunk_size=10, overlap=2, metadata={}) == []


def test_token_window_single_window_breaks() -> None:
    fragments = _token_window_chunks(
        "one two three", chunk_size=50, overlap=0, metadata={"m": 1}
    )
    assert len(fragments) == 1
    assert fragments[0].content == "one two three"
    assert fragments[0].metadata == {"m": 1}


# --------------------------------------------------------------------------- #
# Markdown: oversized section recurses (line 184) + header lineage metadata.
# --------------------------------------------------------------------------- #


def test_markdown_oversized_section_is_recursively_split() -> None:
    big_body = " ".join(f"sentence{i}." for i in range(200))
    text = f"# Title\n\n{big_body}"
    fragments = chunk_text(text, "markdown", {"chunk_size": 200, "chunk_overlap": 20})
    assert len(fragments) >= 2  # the oversized section was broken up
    assert all(f.content.strip() for f in fragments)


def test_markdown_small_section_kept_whole_with_header_metadata() -> None:
    text = "# Alpha\n\nshort body\n\n## Beta\n\nanother short body"
    fragments = chunk_text(text, "markdown", {"chunk_size": 4000})
    assert fragments
    # at least one fragment carries header lineage from the splitter
    assert any(f.metadata for f in fragments)


# --------------------------------------------------------------------------- #
# _markdown_sections_fallback (lines 343-364) — call the fallback directly so we
# do not depend on whether the optional splitter is installed.
# --------------------------------------------------------------------------- #


def test_markdown_fallback_without_headers_returns_single_fragment() -> None:
    sections = _markdown_sections_fallback("just plain prose, no headers at all")
    assert len(sections) == 1
    assert sections[0].metadata == {}
    assert sections[0].content == "just plain prose, no headers at all"


def test_markdown_fallback_splits_on_headers_with_lineage() -> None:
    text = "# One\n\nbody one\n\n## Two\n\nbody two\n\n### Three\n\nbody three"
    sections = _markdown_sections_fallback(text)
    assert len(sections) == 3
    levels = [s.metadata["headers"][0]["level"] for s in sections]
    titles = [s.metadata["headers"][0]["title"] for s in sections]
    assert levels == [1, 2, 3]
    assert titles == ["One", "Two", "Three"]
    # contiguous, non-overlapping spans
    assert sections[0].start_index == 0
    assert sections[-1].end_index == len(text)


def test_markdown_sections_via_chunk_text_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the import-error path inside _markdown_sections (lines 336-337).
    _force_no_langchain(monkeypatch)
    text = "# H1\n\nalpha body\n\n## H2\n\nbeta body"
    fragments = chunk_text(text, "markdown", {"chunk_size": 4000})
    assert fragments
    assert any(f.metadata.get("headers") for f in fragments)


# --------------------------------------------------------------------------- #
# _semantic_units (lines 370, 379, 389-421)
# --------------------------------------------------------------------------- #


def test_semantic_units_whitespace_only_returns_verbatim_fragment() -> None:
    # re.split yields no non-empty paragraphs -> the early single-fragment branch.
    units = _semantic_units("   \n  \t ", chunk_size=100, metadata={"d": 1})
    assert len(units) == 1
    assert units[0].start_index == 0
    assert units[0].metadata == {"d": 1}


def test_semantic_units_small_paragraphs_kept_whole() -> None:
    text = "First para here.\n\nSecond para here.\n\nThird para here."
    units = _semantic_units(text, chunk_size=1000, metadata={})
    assert [u.content for u in units] == [
        "First para here.",
        "Second para here.",
        "Third para here.",
    ]
    # spans line up with the source positions
    for unit in units:
        assert text[unit.start_index : unit.end_index] == unit.content


def test_semantic_units_oversized_paragraph_splits_by_sentence() -> None:
    big = " ".join(f"Sentence number {i} here." for i in range(40))
    units = _semantic_units(big, chunk_size=60, metadata={"src": "x"})
    assert len(units) > 1
    assert all(len(u.content) <= 80 for u in units)  # buffered to roughly chunk_size
    assert all(u.metadata == {"src": "x"} for u in units)


def test_semantic_units_oversized_paragraph_without_sentence_breaks() -> None:
    # One paragraph over the limit but with no sentence terminators -> the
    # "pieces empty / fall back to whole paragraph" branch (lines 390-396).
    big = "x" * 500
    units = _semantic_units(big, chunk_size=100, metadata={})
    assert len(units) == 1
    assert units[0].content == big


# --------------------------------------------------------------------------- #
# _split_semantic merge + flush internals (lines 208, 218, 232, 241-242,
# 249-253, 255-259, 264) via chunk_text(strategy="semantic").
# --------------------------------------------------------------------------- #


def test_semantic_single_unit_returns_units_directly() -> None:
    # One paragraph that fits a unit -> len(units) <= 1 short-circuit (line 208).
    fragments = chunk_text(
        "one small paragraph",
        "semantic",
        {"chunk_size": 4000},
        embedder=_OneHotEmbedder(),
    )
    assert len(fragments) == 1
    assert fragments[0].content == "one small paragraph"


def test_semantic_similarity_flush_splits_dissimilar_units() -> None:
    paragraphs = [f"Topic {i} " + ("word " * 12) for i in range(6)]
    text = "\n\n".join(paragraphs)
    fragments = chunk_text(
        text,
        "semantic",
        {"chunk_size": 200, "chunk_overlap": 8, "semantic_similarity_threshold": 0.5},
        base_metadata={"document_id": "d"},
        embedder=_OneHotEmbedder(),
    )
    assert len(fragments) >= 2  # similarity-based flush fired
    assert all(f.content.strip() for f in fragments)
    assert all(f.metadata.get("document_id") == "d" for f in fragments)


def test_semantic_hard_cap_flush_and_header_metadata() -> None:
    paragraphs = [f"Para {i} " + ("alpha " * 20) for i in range(8)]
    text = "\n\n".join(paragraphs)
    fragments = chunk_text(
        text,
        "semantic",
        # high threshold + constant embedder -> only the hard cap can flush
        {"chunk_size": 200, "chunk_overlap": 0, "semantic_similarity_threshold": 0.9},
        base_metadata={"headers": [{"level": 1, "title": "Root"}], "document_id": "d"},
        embedder=_ConstantEmbedder(),
    )
    assert len(fragments) >= 2  # hard char-cap flush fired
    # the header lineage carried on every unit is surfaced on each merged chunk
    assert all(f.metadata.get("headers") == [{"level": 1, "title": "Root"}] for f in fragments)


def test_semantic_with_overlap_carries_tail_chars() -> None:
    paragraphs = [f"Section {i} " + ("token " * 15) for i in range(5)]
    text = "\n\n".join(paragraphs)
    fragments = chunk_text(
        text,
        "semantic",
        {"chunk_size": 180, "chunk_overlap": 20, "semantic_similarity_threshold": 0.5},
        embedder=_OneHotEmbedder(),
    )
    assert fragments
    assert all(isinstance(f, ChunkFragment) for f in fragments)


# --------------------------------------------------------------------------- #
# _doc_to_chunk via _split_recursive metadata propagation when the real
# splitter is present (covers _doc_to_chunk start/end index inference).
# --------------------------------------------------------------------------- #


def test_split_recursive_directly_returns_indexed_fragments() -> None:
    text = " ".join(f"clause{i}" for i in range(80))
    fragments = _split_recursive(
        text, {"chunk_size": 80, "chunk_overlap": 10}, {"document_id": "doc-z"}
    )
    assert fragments
    assert all(f.metadata.get("document_id") == "doc-z" for f in fragments)


# --------------------------------------------------------------------------- #
# _mean_vectors (lines 446-454) and _cosine_similarity (lines 457-465)
# --------------------------------------------------------------------------- #


def test_mean_vectors_empty_returns_empty() -> None:
    assert _mean_vectors([]) == []


def test_mean_vectors_averages_componentwise() -> None:
    assert _mean_vectors([[2.0, 4.0], [4.0, 8.0]]) == [3.0, 6.0]


def test_cosine_similarity_zero_paths() -> None:
    assert _cosine_similarity([], [1.0]) == 0.0  # empty left
    assert _cosine_similarity([1.0], []) == 0.0  # empty right
    assert _cosine_similarity([1.0, 2.0], [1.0]) == 0.0  # length mismatch
    assert _cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0  # zero-norm left


def test_cosine_similarity_identical_and_orthogonal() -> None:
    assert _cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert _cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# _int_setting (lines 476-490) and _float_setting (lines 500-511)
# --------------------------------------------------------------------------- #


def test_int_setting_default_when_missing() -> None:
    assert _int_setting({}, "k", 7, minimum=0, maximum=100) == 7


def test_int_setting_bool_is_coerced_to_int() -> None:
    # bool is checked before int; True -> 1, clamped up to the minimum.
    assert _int_setting({"k": True}, "k", 5, minimum=3, maximum=100) == 3
    assert _int_setting({"k": False}, "k", 5, minimum=0, maximum=100) == 0


def test_int_setting_int_is_clamped() -> None:
    assert _int_setting({"k": 5}, "k", 0, minimum=0, maximum=100) == 5
    assert _int_setting({"k": 999}, "k", 0, minimum=0, maximum=100) == 100
    assert _int_setting({"k": -5}, "k", 0, minimum=0, maximum=100) == 0


def test_int_setting_float_truncated_then_clamped() -> None:
    assert _int_setting({"k": 12.9}, "k", 0, minimum=0, maximum=100) == 12


def test_int_setting_numeric_string_parsed() -> None:
    assert _int_setting({"k": "42"}, "k", 0, minimum=0, maximum=100) == 42


def test_int_setting_bad_string_falls_back_to_default() -> None:
    assert _int_setting({"k": "not-a-number"}, "k", 9, minimum=0, maximum=100) == 9


def test_int_setting_unsupported_type_falls_back_to_default() -> None:
    assert _int_setting({"k": [1, 2]}, "k", 11, minimum=0, maximum=100) == 11


def test_float_setting_numeric_types_clamped() -> None:
    assert _float_setting({"k": 0.5}, "k", 0.1, minimum=0.0, maximum=1.0) == pytest.approx(0.5)
    assert _float_setting({"k": 9}, "k", 0.1, minimum=0.0, maximum=1.0) == pytest.approx(1.0)
    assert _float_setting({"k": True}, "k", 0.1, minimum=0.0, maximum=5.0) == pytest.approx(1.0)


def test_float_setting_numeric_string_parsed() -> None:
    assert _float_setting({"k": "0.33"}, "k", 0.1, minimum=0.0, maximum=1.0) == pytest.approx(0.33)


def test_float_setting_bad_string_falls_back_to_default() -> None:
    assert _float_setting({"k": "nan-ish-text!"}, "k", 0.7, minimum=0.0, maximum=1.0) == 0.7


def test_float_setting_unsupported_type_falls_back_to_default() -> None:
    assert _float_setting({"k": object()}, "k", 0.42, minimum=0.0, maximum=1.0) == 0.42


# --------------------------------------------------------------------------- #
# Catalog sanity
# --------------------------------------------------------------------------- #


def test_list_chunking_strategies_returns_full_catalog() -> None:
    strategies = list_chunking_strategies()
    ids = {s.strategy_id for s in strategies}
    assert {"recursive", "semantic", "markdown", "token", "character"} <= ids


def test_semantic_chunking_carries_real_overlap() -> None:
    """Regression (#24): a configured chunk_overlap must produce ACTUAL shared
    text between consecutive semantic chunks. Previously the tail was never
    carried forward, so overlap was zero (and a phantom char budget caused
    premature flushes)."""
    text = "\n\n".join(f"Section {i} " + "token " * 15 for i in range(5))
    fragments = chunk_text(
        text,
        "semantic",
        {"chunk_size": 180, "chunk_overlap": 20, "semantic_similarity_threshold": 0.5},
        embedder=_OneHotEmbedder(),
    )
    assert len(fragments) >= 2, "expected multiple semantic fragments"

    def shared_boundary(a: str, b: str) -> int:
        return max((k for k in range(1, min(len(a), len(b)) + 1) if a[-k:] == b[:k]), default=0)

    overlaps = [
        shared_boundary(fragments[i].content, fragments[i + 1].content)
        for i in range(len(fragments) - 1)
    ]
    assert all(o > 0 for o in overlaps), f"expected real overlap between chunks, got {overlaps}"
