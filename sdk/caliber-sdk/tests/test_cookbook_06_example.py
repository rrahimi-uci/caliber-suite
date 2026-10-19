"""Cookbook 06 runs end to end against a stateful stand-in for the knowledge routes.

The real embeddings, rankings and Recall@k arithmetic are covered by running the script against a
live stack (the lab README quotes that run); this keeps the script's call sequence, its reading
of every response and its verification logic covered offline. See ``cookbook_06_fake_server``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from cookbook_06_fake_server import FakeKnowledgeServer

from caliber_sdk import CaliberClient
from examples.cookbooks.cookbook_06_grounded_knowledge_assistant import (
    assets_dir,
    cleanup,
    load_corpus,
    load_jsonl,
    norm,
    run,
    split_frontmatter,
)


def test_cookbook_06_runs_the_knowledge_lab_end_to_end() -> None:
    assets = assets_dir()
    server = FakeKnowledgeServer(load_corpus(assets), load_jsonl(assets / "dataset/qa-eval.jsonl"))
    caliber = server.client(CaliberClient)
    with caliber:
        report = run(caliber, suffix="t1", poll=0)

    assert report["verified"] is True and report["checks"] > 50
    assert report["simulated"] == ["eval", "llm"]
    assert report["v1_summary"]["processed_sources"] == 11
    assert report["sweeps"]["hybrid"]["hit_at_3"] == 41
    assert report["sweeps"]["hybrid"]["hit_at_1"] > report["sweeps"]["dense"]["hit_at_1"]
    assert report["gate"]["outside"][1] < 0.42 <= report["gate"]["answerable"][0]
    assert len(report["calibration"]) == 8
    runs = report["workflow_runs"]
    assert runs["answerable"]["needs_review"] is False
    assert runs["outside_the_corpus"]["needs_review"] is True
    assert runs["trap"]["needs_review"] is False
    assert report["activate_failed"].startswith("409 ")
    assert report["evaluation"].startswith("refused: 400 ")

    # the order the README documents
    calls = server.calls
    ordered = [
        "POST /object-store/buckets/policy-corpus/objects",
        "POST /knowledge-bases",
        "POST /knowledge/query",
        "POST /eval-datasets",
        "POST /knowledge-bases/KB-06/calibrate",
        "POST /knowledge-bases/KB-06/baseline",
        "POST /knowledge-bases/KB-06/versions",
        "POST /knowledge-bases/KB-06/rollback",
        "POST /cookbooks/06/install",
        "PATCH /workflow-versions/WFV-06",
        "POST /workflow-versions/WFV-06/run",
        "POST /review-queues",
        "POST /judges",
        "POST /evaluations",
    ]
    positions = [calls.index(call) for call in ordered]
    assert positions == sorted(positions)


def test_the_script_fails_loudly_when_the_corpus_and_the_questions_disagree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drifted = tmp_path / "assets"
    shutil.copytree(assets_dir(), drifted)
    policy = drifted / "dataset/sources/refund-policy.md"
    policy.write_text(policy.read_text().replace("14-day window", "7-day window"))
    monkeypatch.setenv("COOKBOOK_06_ASSETS", str(drifted))
    server = FakeKnowledgeServer(
        load_corpus(drifted), load_jsonl(drifted / "dataset/qa-eval.jsonl")
    )
    with server.client(CaliberClient) as caliber, pytest.raises(AssertionError) as raised:
        run(caliber, suffix="t2", poll=0)
    assert "literal passage" in str(raised.value)
    assert (
        "POST /knowledge-bases" not in server.calls
    )  # nothing was built from a corpus that drifted


def test_helpers_read_the_assets_the_readme_names() -> None:
    meta, template = split_frontmatter((assets_dir() / "prompts/kb-answer.md").read_text())
    assert meta["name"] == "kb-answer"
    assert "{{ retrieved_chunks }}" in template and not template.startswith("---")
    assert norm("A  **Bold**\nline") == "a bold line"


def test_cleanup_with_nothing_to_remove_is_a_noop(capsys) -> None:  # type: ignore[no-untyped-def]
    class Empty:
        def list(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            return []

    class Nothing:
        knowledge_bases = workflows = prompts = judges = review_queues = eval_datasets = Empty()

    cleanup(Nothing(), "none")  # type: ignore[arg-type]
    assert "removed: nothing" in capsys.readouterr().out


def test_cleanup_removes_only_what_the_suffix_created(capsys) -> None:  # type: ignore[no-untyped-def]
    from types import SimpleNamespace as NS

    calls: list[str] = []

    class Rows:
        def __init__(self, kind: str, rows: list[NS]) -> None:
            self.kind, self.rows = kind, rows

        def list(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            return self.rows

        def delete(self, ident):  # type: ignore[no-untyped-def]
            calls.append(f"delete {self.kind} {ident}")

        def update(self, ident, **changes):  # type: ignore[no-untyped-def]
            calls.append(f"update {self.kind} {ident} {changes}")

    class Client:
        knowledge_bases = Rows(
            "kb",
            [
                NS(name="policy-qa-kb-s1", knowledge_base_id="KB-1"),
                NS(name="policy-qa-kb-other", knowledge_base_id="KB-2"),
            ],
        )
        workflows = Rows("wf", [NS(name="cookbook-06-kb-assistant-s1", workflow_id="WF-1")])
        prompts = Rows("prompt", [NS(prompt_name="kb-answer-s1"), NS(prompt_name="kb-answer-x")])
        judges = Rows("judge", [NS(name="CitationFaithfulness-s1", judge_id="J-1")])
        review_queues = Rows("queue", [NS(name="policy-qa-review-s1", queue_id="Q-1")])
        eval_datasets = Rows("set", [NS(name="policy-qa-eval-s1", dataset_id="ED-1")])

    cleanup(Client(), "s1")  # type: ignore[arg-type]
    assert "delete kb KB-1" in calls and "delete kb KB-2" not in calls
    assert "delete prompt kb-answer-s1" in calls and "delete prompt kb-answer-x" not in calls
    assert any(c.startswith("update wf WF-1") for c in calls)
    assert any(c.startswith("update judge J-1") for c in calls)
    assert any(c.startswith("update queue Q-1") for c in calls)
    assert any(c.startswith("update set ED-1") for c in calls)  # matches the eval name, and the
    assert "removed: knowledge base policy-qa-kb-s1" in capsys.readouterr().out
