"""Cookbook 14 (a governance starter kit from one sentence): the script, its data, its refusals.

The script runs against ``cookbook_12_15_fake_server.FakeAria``, an in-memory imitation of the
routes it calls. The fake restates the planner and the plan executor independently of the server,
so these tests check that the dataset files, the fake and the script agree, and that the script
*fails*, naming the check, against a server that stops following a rule. Running the script against
a live server is how the real planner and executor are verified; see the lab README.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from cookbook_12_15_fake_server import FakeAria, plan_steps

from caliber_sdk import CaliberClient
from examples.cookbooks import cookbook_14_aria_governance_starter_kit as lab
from examples.cookbooks._aria_lab import (
    SetupError,
    VerificationError,
    form_values,
    load_jsonl,
)

ASSETS = Path(lab.default_assets_dir())


def clients(fake: FakeAria) -> dict[str, CaliberClient]:
    return {
        "admin": fake.client(CaliberClient),
        "olga": fake.client(CaliberClient, "session-olga"),
        "vera": fake.client(CaliberClient, "session-vera"),
    }


def run_lab(fake: FakeAria, lines: list[str] | None = None, **kwargs: Any) -> dict[str, Any]:
    people = clients(fake)
    return lab.run(
        people["admin"],
        operator=people["olga"],
        viewer=people["vera"],
        suffix="unit",
        report=(lines.append if lines is not None else print),
        **kwargs,
    )


def test_the_lab_passes_against_a_server_that_follows_the_rules() -> None:
    fake = FakeAria()
    lines: list[str] = []
    result = run_lab(fake, lines)
    assert lines[-1] == "\nAll documented outcomes verified."
    assert all(line.startswith("ok") for line in lines[:-1])
    assert result["evaluation"] == "refused (simulated provider)"
    assert result["cases"]["count"] == 30 and result["cases"]["high_risk"] == 5
    assert fake.judges[result["judge_id"]]["name"] == "cookbook-14-AnswerFaithfulness-kit-unit"
    assert fake.queues[result["queue_id"]]["name"] == "cookbook-14-governance-review-kit-unit"
    dataset = fake.datasets[result["dataset_id"]]
    assert dataset["name"] == "cookbook-14-release-candidates-eval-kit-unit"
    assert dataset["version"] == 31 and len(fake.examples[dataset["dataset_id"]]) == 30
    # kit + auto kit + the denied kit's judge and test set + the half way kit's judge
    assert len(fake.judges) == 4 and len(fake.queues) == 2 and len(fake.datasets) == 3


def test_the_goals_dataset_matches_the_planner_and_is_varied() -> None:
    goals = load_jsonl(ASSETS / "dataset/goals.jsonl")
    assert len(goals) == 28
    assert len({g["id"] for g in goals}) == 28 and len({g["goal"] for g in goals}) == 28
    for goal in goals:
        drawn = [step["cap"]["key"] for step in plan_steps(goal["goal"])]
        assert drawn == goal["expected_steps"], goal["id"]
    assert {g["outcome"] for g in goals} == {"as_asked", "missed", "nothing", "unasked"}
    assert all(g["note"] for g in goals)
    assert {g["id"] for g in goals if not g["expected_steps"]} == {
        g["id"] for g in goals if g["outcome"] == "nothing"
    }
    # Whatever the order of the sentence, a plan lists its steps in registry order.
    registry = [*lab.KIT, "workflow.calibrate"]
    for goal in goals:
        assert goal["expected_steps"] == [k for k in registry if k in goal["expected_steps"]]


def test_the_release_candidate_cases_are_complete_and_varied() -> None:
    rows = load_jsonl(ASSETS / "dataset/release-candidate-cases.jsonl")
    assert len(rows) == 30 and len({r["id"] for r in rows}) == 30
    assert len({r["input"]["question"] for r in rows}) == 30
    assert all(
        set(r["input"]) == {"question", "evidence"} and r["expected"]["answer"] for r in rows
    )
    tags = {tag for r in rows for tag in r["tags"]}
    assert {"hard-negative", "high-risk", "edge", "unsupported", "out-of-scope"} <= tags
    assert {r["tags"][0] for r in rows} == {
        "leave",
        "expenses",
        "it",
        "benefits",
        "conduct",
        "payroll",
        "remote",
        "onboarding",
    }


def test_the_form_specs_are_valid_form_values() -> None:
    judge = form_values(ASSETS / "judges/answer-faithfulness.judge.json")
    assert set(judge) == {"name", "feedback_value_type", "instructions", "tags"}
    assert "\n" not in judge["instructions"]
    queue = form_values(ASSETS / "review-queues/governance-review.queue.json")
    assert set(queue) == {"name", "description", "questions", "reviewers"}
    assert [q["key"] for q in queue["questions"]] == ["faithful", "citation_ok", "tone", "notes"]
    dataset = form_values(ASSETS / "datasets/release-candidates.dataset.json")
    assert set(dataset) == {"name", "description", "tags"}
    expected = json.loads((ASSETS / "expected-plan.json").read_text("utf-8"))
    assert [s["capability_key"] for s in expected["expected_steps"]] == lab.KIT
    assert expected["goal"] == lab.INTENT


@pytest.mark.parametrize(
    ("bend", "failed_check"),
    [
        ("links_name", "only the enqueue step has a wired input"),
        ("stuck_skip", "denying the queue skips it and the enqueue step with it"),
        ("no_scope_floor", "a plan owned by an account without the operator scope"),
    ],
)
def test_the_script_notices_a_server_that_breaks_a_rule(bend: str, failed_check: str) -> None:
    lines: list[str] = []
    with pytest.raises((VerificationError, AssertionError)):
        run_lab(FakeAria(bend=bend), lines)
    assert any(line.startswith("FAIL") and failed_check in line for line in lines)


def test_setup_problems_are_named_not_stack_traces() -> None:
    people = clients(FakeAria())
    with pytest.raises(SetupError, match="admin personal access token"):
        lab.run(people["olga"], operator=people["olga"], viewer=people["vera"])
    with pytest.raises(SetupError, match="CALIBER_OPERATOR_USERS=admin,olga"):
        lab.run(people["admin"], operator=people["vera"], viewer=people["vera"])
    off = FakeAria()
    off.capabilities = lambda **_: httpx.Response(  # type: ignore[method-assign]
        404, json={"detail": "disabled", "status_code": 404, "reason_code": "feature_disabled"}
    )
    off_people = clients(off)
    with pytest.raises(SetupError, match="CALIBER_FEATURE_ARIA=true"):
        lab.run(off_people["admin"], operator=off_people["olga"], viewer=off_people["vera"])


def test_main_runs_the_lab_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeAria()
    monkeypatch.setenv("CALIBER_BASE_URL", "https://caliber.test")
    monkeypatch.setattr(lab, "env_client", lambda: fake.client(CaliberClient))
    monkeypatch.setattr(
        lab,
        "sign_in",
        lambda _base, user, _password: {"olga": "session-olga", "vera": "session-vera"}[user],
    )

    class Routed(CaliberClient):
        def __init__(self, base_url: str, *, token: str) -> None:
            super().__init__(
                base_url, token=token, http_client=httpx.Client(transport=fake.transport())
            )

    monkeypatch.setattr(lab, "CaliberClient", Routed)
    assert lab.main(["--suffix", "main"]) == 0
    out = capsys.readouterr().out
    assert "All documented outcomes verified." in out and '"queue_id"' in out
    assert lab.main(["--cleanup"]) == 0
    cleaned = json.loads(capsys.readouterr().out)
    assert cleaned["judges"] >= 3 and cleaned["review_queues"] >= 2 and cleaned["test_sets"] >= 3
    assert all(j["status"] == "archived" for j in fake.judges.values())
    assert all(q["status"] == "archived" for q in fake.queues.values())


def test_main_reports_a_setup_problem_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeAria()
    fake.tokens["calpat_admin"] = "olga"  # the token belongs to a non-admin
    monkeypatch.setenv("CALIBER_BASE_URL", "https://caliber.test")
    monkeypatch.setattr(lab, "env_client", lambda: fake.client(CaliberClient))
    monkeypatch.setattr(lab, "sign_in", lambda *_: "session-olga")
    monkeypatch.setattr(lab, "ensure_accounts", lambda *_: None)
    assert lab.main([]) == 1
    assert "VERIFICATION FAILED" in capsys.readouterr().err
