"""Cookbook 12 (an evaluation harness from one sentence): the script, its data files, its refusals.

The script runs against ``cookbook_12_15_fake_server.FakeAria``, an in-memory imitation of the
routes it calls. The fake restates Aria's planner independently of the server, so these tests check
that the dataset files, the fake and the script agree, and that the script *fails*, naming the
check, against a server that stops following a rule. Running the script against a live server is how
the real planner and executor are verified; see the lab README.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from cookbook_12_15_fake_server import FakeAria, plan_steps

from caliber_sdk import CaliberClient
from examples.cookbooks import cookbook_12_aria_evaluation_harness as lab
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
    assert result["rows"] == {
        "count": 30,
        "categories": ["account", "billing", "refund", "safety", "shipping", "warranty"],
    }
    assert result["evaluation"] == "refused (simulated provider)"
    judge = fake.judges[result["judge_id"]]
    dataset = fake.datasets[result["dataset_id"]]
    assert judge["name"] == "cookbook-12-AnswerFaithfulness-unit"
    assert dataset["name"] == "cookbook-12-support-faithfulness-eval-unit"
    assert dataset["version"] == 31 and len(fake.examples[dataset["dataset_id"]]) == 30
    # The denied run created the test set and no judge; the failed runs created nothing.
    assert "cookbook-12-denied-unit" not in {j["name"] for j in fake.judges.values()}
    assert "cookbook-12-kept-set-unit" in {d["name"] for d in fake.datasets.values()}
    assert "cookbook-12-viewer-unit" not in {j["name"] for j in fake.judges.values()}


def test_a_real_provider_is_asked_to_score_every_row() -> None:
    fake = FakeAria(simulated=False)
    lines: list[str] = []
    result = run_lab(fake, lines)
    assert result["evaluation"] == "completed"
    assert any("a real provider scored every row" in line for line in lines)


def test_the_goals_dataset_matches_the_planner_and_is_varied() -> None:
    goals = load_jsonl(ASSETS / "dataset/goals.jsonl")
    assert len(goals) == 28
    assert len({g["id"] for g in goals}) == 28 and len({g["goal"] for g in goals}) == 28
    for goal in goals:
        drawn = [step["cap"]["key"] for step in plan_steps(goal["goal"])]
        assert drawn == goal["expected_steps"], goal["id"]
    outcomes = {g["outcome"] for g in goals}
    assert outcomes == {"as_asked", "missed", "nothing", "unasked"}
    # Every outcome is explained, and the empty plans are exactly the ones that drew no steps.
    assert all(g["note"] for g in goals)
    assert {g["id"] for g in goals if not g["expected_steps"]} == {
        g["id"] for g in goals if g["outcome"] == "nothing"
    }


def test_the_faithfulness_cases_are_complete_and_varied() -> None:
    rows = load_jsonl(ASSETS / "dataset/faithfulness-cases.jsonl")
    assert len(rows) == 30 and len({r["id"] for r in rows}) == 30
    assert all(
        set(r["input"]) == {"question", "evidence"} and r["expected"]["answer"] for r in rows
    )
    tags = {tag for r in rows for tag in r["tags"]}
    assert {"hard-negative", "unsupported", "out-of-scope", "edge", "partial-evidence"} <= tags
    assert len({r["input"]["question"] for r in rows}) == 30


def test_the_judge_and_test_set_specs_are_valid_form_values() -> None:
    judge = form_values(ASSETS / "judges/answer-faithfulness.judge.json")
    assert set(judge) == {"name", "feedback_value_type", "instructions"}
    assert "\n" not in judge["instructions"] and "{{ outputs }}" in judge["instructions"]
    dataset = form_values(ASSETS / "datasets/support-faithfulness.dataset.json")
    assert set(dataset) == {"name", "description", "tags"}
    expected = json.loads((ASSETS / "expected-plan.json").read_text("utf-8"))
    assert [s["capability_key"] for s in expected["expected_steps"]] == [
        "judge.create",
        "eval_dataset.create",
    ]
    assert expected["goal"] == lab.INTENT


@pytest.mark.parametrize(
    ("bend", "failed_check"),
    [
        ("links_name", "neither step has an input wired from the other"),
        ("no_scope_floor", "a plan owned by an account without the operator scope"),
    ],
)
def test_the_script_notices_a_server_that_breaks_a_rule(bend: str, failed_check: str) -> None:
    lines: list[str] = []
    with pytest.raises(VerificationError):
        run_lab(FakeAria(bend=bend), lines)
    assert any(line.startswith("FAIL") and failed_check in line for line in lines)


def test_a_goal_the_planner_draws_differently_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = load_jsonl

    def altered(path: Path) -> list[dict[str, Any]]:
        rows = real(path)
        if path.name == "goals.jsonl":
            rows[0] = {**rows[0], "expected_steps": ["judge.create"]}
        return rows

    monkeypatch.setattr(lab, "load_jsonl", altered)
    lines: list[str] = []
    with pytest.raises(VerificationError):
        run_lab(FakeAria(), lines)
    assert any("FAIL the planner drew the documented plan for every goal" in line for line in lines)
    assert any("G01: drew ['judge.create', 'eval_dataset.create']" in line for line in lines)


def test_setup_problems_are_named_not_stack_traces() -> None:
    people = clients(FakeAria())
    # a token that is not an admin's
    with pytest.raises(SetupError, match="admin personal access token"):
        lab.run(people["olga"], operator=people["olga"], viewer=people["vera"])
    # scopes that do not match what the lab needs
    with pytest.raises(SetupError, match="CALIBER_OPERATOR_USERS=admin,olga"):
        lab.run(people["admin"], operator=people["vera"], viewer=people["vera"])
    # Aria switched off on the server
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
    real_client = CaliberClient

    class Routed(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, base_url: str, *, token: str) -> None:
            super().__init__(
                base_url, token=token, http_client=httpx.Client(transport=fake.transport())
            )

    monkeypatch.setattr(lab, "CaliberClient", Routed)
    assert lab.main(["--suffix", "main"]) == 0
    out = capsys.readouterr().out
    assert "All documented outcomes verified." in out and '"judge_id"' in out
    assert {"olga", "vera"} <= fake.accounts

    assert lab.main(["--cleanup"]) == 0
    cleaned = json.loads(capsys.readouterr().out)
    assert cleaned["judges"] >= 1 and cleaned["test_sets"] >= 2
    assert all(
        j["status"] == "archived"
        for j in fake.judges.values()
        if j["name"].startswith("cookbook-12-")
    )


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
