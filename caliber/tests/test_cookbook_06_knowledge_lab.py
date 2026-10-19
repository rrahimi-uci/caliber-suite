"""Cookbook 06 (Grounded Knowledge Assistant): its dataset, and the template's confidence node.

The lab README states what the policy corpus and the question sets contain, and the runnable
example (``sdk/caliber-sdk/examples/cookbooks/cookbook_06_grounded_knowledge_assistant.py``)
asserts it against a live server. These tests keep the files honest without a server:

* every supporting clause in the question sets is a literal passage of the document it names,
  so a policy edit cannot silently orphan a question;
* the JSONL rows have the ``input`` / ``expected`` shape that both ``POST /eval-datasets/{id}/examples``
  and the Prompts page's "Upload Dataset" read;
* the installed template's ``confidence`` node reads paths that exist in a real
  ``knowledge_query`` result. Before this lab it read ``citations`` and ``citation_count`` at the top
  of the result, which does not exist there (citations sit under ``versions``), so every answer scored
  the bias alone, ``0.1``, and was flagged for review.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from caliber.knowledge.schemas import (
    KnowledgeQueryCitationSchema,
    KnowledgeQueryResultSchema,
    KnowledgeQueryVersionResultSchema,
)
from caliber.workflows.cookbook_catalog import build_cookbook_catalog
from caliber.workflows.data_transform import apply_data_transform

REPO_ROOT = Path(__file__).resolve().parents[2]
ASSETS = REPO_ROOT / "docs-site/cookbooks/06-knowledge-retrieval-policy-qa-lab/assets"
SOURCES = ASSETS / "dataset/sources"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("*", "")).strip().lower()


def _rows(name: str) -> list[dict]:
    lines = (ASSETS / "dataset" / name).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _documents() -> dict[str, str]:
    return {
        path.name: _norm(path.read_text(encoding="utf-8"))
        for path in SOURCES.glob("*.md")
        if path.name != "README.md"
    }


def test_the_corpus_is_eleven_policy_documents_of_real_length() -> None:
    documents = _documents()
    assert len(documents) == 11
    for name, text in documents.items():
        assert len(text.split()) >= 300, f"{name} is too short to be a realistic policy"
    # every document carries the id, owner, version and (for the ones that change) an effective date
    for path in SOURCES.glob("*.md"):
        if path.name == "README.md":
            continue
        head = path.read_text(encoding="utf-8")[:600]
        assert "Document ID:" in head and "Owner:" in head, path.name


def test_the_question_sets_have_the_shape_both_importers_read() -> None:
    for name in ("qa-eval.jsonl", "calibration-questions.jsonl"):
        rows = _rows(name)
        assert rows, name
        ids = [row["id"] for row in rows]
        assert len(ids) == len(set(ids)), f"{name} repeats an id"
        for row in rows:
            assert set(row) <= {"id", "tags", "input", "expected"}, (name, row["id"])
            assert isinstance(row["input"]["question"], str) and row["input"]["question"].strip()
            assert isinstance(row["expected"], dict)


def test_every_supporting_clause_is_a_literal_passage_of_its_document() -> None:
    documents = _documents()
    for row in _rows("qa-eval.jsonl"):
        expected = row["expected"]
        for source in expected["sources"]:
            assert source in documents, (row["id"], source)
        evidence = _norm(expected.get("evidence", ""))
        if expected["sources"]:
            assert evidence, row["id"]
            assert any(evidence in documents[s] for s in expected["sources"]), (row["id"], evidence)
        if expected["sources"] and expected["expected"]:
            assert any(_norm(expected["expected"]) in documents[s] for s in expected["sources"]), (
                row["id"],
                expected["expected"],
            )


def test_the_question_mix_is_what_the_readme_says() -> None:
    rows = _rows("qa-eval.jsonl")
    by_decision: dict[str, int] = {}
    for row in rows:
        by_decision[row["expected"]["decision"]] = (
            by_decision.get(row["expected"]["decision"], 0) + 1
        )
    assert by_decision == {"answer": 39, "clarify": 2, "abstain": 7}
    tags = {tag for row in rows for tag in row["tags"]}
    assert {"trap", "table", "effective-date", "contradiction", "out_of_scope"} <= tags
    # the questions with no supporting clause are exactly the abstain rows
    assert {r["id"] for r in rows if not r["expected"]["sources"]} == {
        r["id"] for r in rows if r["expected"]["decision"] == "abstain"
    }
    # both refund documents back the two contradiction questions
    for row in rows:
        if "contradiction" in row["tags"]:
            assert set(row["expected"]["sources"]) == {"refund-policy.md", "refund-faq.md"}


def test_the_calibration_set_is_the_answerable_part_of_the_question_set() -> None:
    qa = {r["input"]["question"]: r for r in _rows("qa-eval.jsonl") if r["expected"]["sources"]}
    calibration = _rows("calibration-questions.jsonl")
    assert len(calibration) == len(qa) == 41
    for row in calibration:
        twin = qa[row["input"]["question"]]
        assert row["expected"]["sources"] == twin["expected"]["sources"]
        assert row["expected"]["answer"] == twin["expected"]["answer"]
        assert set(row["expected"]) == {"sources", "answer", "evidence"}  # what KB Calibrate reads


def test_the_unreadable_file_really_is_not_a_pdf() -> None:
    data = (ASSETS / "dataset/unreadable/scanned-handbook.pdf").read_bytes()
    assert data.startswith(b"%PDF-")
    assert b"startxref" not in data  # truncated: no cross-reference table, so extraction fails


def _query_result(citation_count: int) -> dict:
    citations = [
        KnowledgeQueryCitationSchema(
            chunk_id=f"KBC-{n}",
            label=f"[{n + 1}] data-retention-policy.md",
            source_bucket="policy-corpus",
            source_key="policies/data-retention-policy.md",
            object_store_path="/object-store?bucket=policy-corpus&key=policies%2Fdata-retention-policy.md",
            score=0.7,
        )
        for n in range(citation_count)
    ]
    version = KnowledgeQueryVersionResultSchema(
        knowledge_base_version_id="KBV-1",
        knowledge_base_id="KB-1",
        version_number=1,
        knowledge_base_name="policy-qa-kb",
        chunking_strategy="markdown",
        embedding_model="sentence-transformers/all-MiniLM-L6-v2",
        answer="digest" if citations else None,
        citations=citations,
    )
    return KnowledgeQueryResultSchema(question="q", versions=[version]).model_dump()


def _template_confidence_config() -> dict:
    recipe = next(r for r in build_cookbook_catalog()["recipes"] if r["id"] == "06")
    return recipe["manifest_template"]["nodes"]["confidence"]["config"]


def test_the_template_confidence_node_reads_a_real_query_result() -> None:
    config = _template_confidence_config()
    cited_twice = apply_data_transform("confidence", _query_result(3), config)["result"]
    cited_once = apply_data_transform("confidence", _query_result(1), config)["result"]
    uncited = apply_data_transform("confidence", _query_result(0), config)["result"]

    assert cited_twice["matched_signals"] == ["has-citations", "multiple-sources"]
    assert cited_twice["confidence"] == 0.9 and cited_twice["needs_review"] is False
    assert cited_once["matched_signals"] == ["has-citations"]
    assert cited_once["needs_review"] is False
    # no citation at all is the case the node exists for: only the bias remains, and a person looks
    assert uncited["matched_signals"] == []
    assert uncited["confidence"] == 0.1 and uncited["needs_review"] is True
