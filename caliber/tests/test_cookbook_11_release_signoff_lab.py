"""Cookbook 11: the 20-candidate dataset against the server's own scoring and request schema."""

from __future__ import annotations

import json
from collections import Counter
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from caliber.routes.releases import _evaluate_candidate
from caliber.schemas import ReleaseCandidateCreateRequest

LAB = Path(__file__).resolve().parents[2] / "docs-site/cookbooks/11-release-readiness-factory"
DATASET = LAB / "assets/dataset/release-candidates.jsonl"


def _rows() -> list[dict[str, Any]]:
    return [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]


def _oracle(row: dict[str, Any]) -> dict[str, Any]:
    """The expected outcome worked out with exact fractions, independent of the server's floats."""
    criteria = row["criteria"]
    weight = sum(Fraction(str(c["weight"])) for c in criteria)
    weighted = sum(Fraction(str(c["weight"])) * Fraction(str(c["score"])) for c in criteria)
    score = round(float(weighted / weight), 6)
    blockers = [
        c["key"]
        for c in criteria
        if c["blocking"] and Fraction(str(c["score"])) < Fraction(str(c["threshold"]))
    ]
    ready = Fraction(str(score)) >= Fraction(str(row["required_score"])) and not blockers
    return {
        "weighted_score": score,
        "status": "ready" if ready else "blocked",
        "blockers": blockers,
    }


@pytest.mark.parametrize("row", _rows(), ids=lambda row: row["id"])
def test_dataset_expectation_equals_the_exact_arithmetic(row: dict[str, Any]) -> None:
    assert row["expect"] == _oracle(row)


@pytest.mark.parametrize("row", _rows(), ids=lambda row: row["id"])
def test_the_server_scores_every_candidate_as_the_dataset_says(row: dict[str, Any]) -> None:
    body = ReleaseCandidateCreateRequest(
        name=row["name"],
        artifact_type=row["artifact_type"],
        artifact_ref=row["artifact_ref"],
        version_ref=row["version_ref"],
        required_score=row["required_score"],
        criteria=row["criteria"],
        evidence=row["evidence"],
    )
    candidate = SimpleNamespace(
        criteria=[c.model_dump(mode="json") for c in body.criteria],
        waivers=[],
        required_score=body.required_score,
        weighted_score=None,
        blockers=[],
        status="draft",
    )
    _evaluate_candidate(candidate)  # type: ignore[arg-type]
    assert candidate.weighted_score == row["expect"]["weighted_score"]
    assert candidate.status == row["expect"]["status"]
    assert [b["criterion_key"] for b in candidate.blockers] == row["expect"]["blockers"]


def test_dataset_covers_the_cases_the_lab_teaches() -> None:
    rows = _rows()
    assert len(rows) == 20
    assert len({row["id"] for row in rows}) == 20
    types = Counter(row["artifact_type"] for row in rows)
    assert types["prompt"] >= 5 and types["workflow"] >= 3
    assert types["skill"] >= 3 and types["knowledge_base"] >= 3
    outcomes = Counter(row["expect"]["status"] for row in rows)
    assert outcomes == {"ready": 14, "blocked": 6}
    by_id = {row["id"]: row for row in rows}
    # blocked by a blocker although the score is high; blocked by score with no blocker at all
    assert by_id["RC05"]["expect"]["weighted_score"] > by_id["RC05"]["required_score"]
    assert by_id["RC05"]["expect"]["blockers"] == ["human_review"]
    assert by_id["RC06"]["expect"]["blockers"] == []
    assert by_id["RC06"]["expect"]["weighted_score"] < by_id["RC06"]["required_score"]
    # a score exactly at its threshold is not a blocker; a weighted score exactly at the bar is ready
    assert by_id["RC08"]["expect"]["weighted_score"] == by_id["RC08"]["required_score"]
    assert by_id["RC09"]["expect"]["blockers"] == []
    # a non-blocking criterion below its threshold never blocks
    assert any(not c["blocking"] and c["score"] < c["threshold"] for c in by_id["RC10"]["criteria"])
    assert by_id["RC10"]["expect"]["status"] == "ready"
    # the waiver drill: the waiver can be the only thing in the way for RC16, never enough for RC17
    assert by_id["RC16"]["expect"]["blockers"] == ["rollback_rehearsed"]
    assert by_id["RC16"]["expect"]["weighted_score"] >= by_id["RC16"]["required_score"]
    assert by_id["RC17"]["expect"]["weighted_score"] < by_id["RC17"]["required_score"]
    # sizes: one criterion, and many
    assert len(by_id["RC11"]["criteria"]) == 1 and len(by_id["RC13"]["criteria"]) == 12


def test_every_row_has_evidence_and_a_rollback_version() -> None:
    for row in _rows():
        assert row["evidence"], row["id"]
        assert row["rollback_version"] and row["rollback_version"] != row["version_ref"], row["id"]
        assert row["note"], row["id"]
