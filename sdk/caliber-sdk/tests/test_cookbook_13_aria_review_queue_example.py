"""Cookbook 13 (a human-review queue from one sentence): the script, its data files, its refusals.

The script runs against ``cookbook_12_15_fake_server.FakeAria``, an in-memory imitation of the
routes it calls (the traces are handed to it, so no MLflow is needed). The fake restates the
planner, the plan executor and the review queue's answer rules independently of the server, so these
tests check that the dataset files, the fake and the script agree, and that the script *fails*,
naming the check, against a server that stops following a rule. Running the script against a live
server (and MLflow) is how the real behaviour is verified; see the lab README.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from cookbook_12_15_fake_server import FakeAria, plan_steps

from caliber_sdk import CaliberClient
from caliber_sdk.errors import CaliberAPIError
from examples.cookbooks import cookbook_13_aria_review_queue as lab
from examples.cookbooks._aria_lab import (
    SetupError,
    VerificationError,
    form_values,
    load_jsonl,
)

ASSETS = Path(lab.default_assets_dir())


def rows() -> list[dict[str, Any]]:
    return load_jsonl(ASSETS / "dataset/agent-replies.jsonl")


def make_fake(**kwargs: Any) -> tuple[FakeAria, dict[str, str]]:
    traces = {row["id"]: f"tr-{row['id'].lower()}" for row in rows()}
    known = {trace: row["user_message"] for row, trace in zip(rows(), traces.values(), strict=True)}
    return FakeAria(traces=known, **kwargs), traces


def clients(fake: FakeAria) -> dict[str, CaliberClient]:
    return {
        "admin": fake.client(CaliberClient),
        "olga": fake.client(CaliberClient, "session-olga"),
        "vera": fake.client(CaliberClient, "session-vera"),
    }


def run_lab(
    fake: FakeAria, traces: dict[str, str], lines: list[str] | None = None, **kwargs: Any
) -> dict[str, Any]:
    people = clients(fake)
    return lab.run(
        people["admin"],
        operator=people["olga"],
        viewer=people["vera"],
        suffix="unit",
        traces=traces,
        report=(lines.append if lines is not None else print),
        **kwargs,
    )


def test_the_lab_passes_against_a_server_that_follows_the_rules() -> None:
    fake, traces = make_fake()
    lines: list[str] = []
    result = run_lab(fake, traces, lines)
    assert lines[-1] == "\nAll documented outcomes verified."
    assert all(line.startswith("ok") for line in lines[:-1])
    assert result["review"] == {"reviewed": 12, "pending": 18, "unsafe_in_reviewed": 4}
    queue = fake.queues[result["queue_id"]]
    assert queue["name"] == "cookbook-13-agent-reply-review-unit"
    assert queue["reviewers"] == ["olga"] and len(queue["questions"]) == 5
    items = fake.items[result["queue_id"]]
    assert len(items) == 31  # the 30 replies and the one trace id that does not exist
    assert sum(1 for item in items if item["status"] == "completed") == 12
    # The answers reached the traces as assessments.
    assert fake.assessments[traces["R11"]] != []
    assert {a["name"]: a["value"] for a in fake.assessments[traces["R11"]]}["severity"] == "high"
    # The failed and skipped plans created nothing but the one kept queue.
    names = {q["name"] for q in fake.queues.values()}
    assert names == {"cookbook-13-agent-reply-review-unit", "cookbook-13-kept-unit"}


def test_a_real_provider_checks_a_judge_and_ask_aria_acts_inside_its_boundary() -> None:
    fake, traces = make_fake(simulated=False)
    lines: list[str] = []
    result = run_lab(fake, traces, lines)
    assert lines[-1] == "\nAll documented outcomes verified."
    assert result["mode"] == "real"
    alignment = result["judge_alignment"]
    assert alignment["n"] == 12 and alignment["agreement"] >= 0.75 and alignment["kappa"] >= 0.4
    assert alignment["confusion"] == {"true_pos": 7, "false_pos": 0, "true_neg": 4, "false_neg": 1}
    assert result["chat"]["acting"] == ["judge_list", "review_queue_create"]
    assert result["chat"]["reading"] == ["judge_list"]
    queues = {q["name"] for q in fake.queues.values()}
    assert "cookbook-13-chat-queue-unit" in queues and "cookbook-13-chat-manual-unit" not in queues
    assert any("26/26" in line for line in lines)


@pytest.mark.parametrize(
    ("bend", "failed_check"),
    [
        ("judge_guesses", "the judge agrees with the reviewers"),
        ("chat_always_acts", "is not offered: nothing was created"),
    ],
)
def test_a_real_run_notices_a_judge_or_a_chat_that_breaks_a_rule(
    bend: str, failed_check: str
) -> None:
    fake, traces = make_fake(simulated=False, bend=bend)
    lines: list[str] = []
    with pytest.raises(VerificationError):
        run_lab(fake, traces, lines)
    assert any(line.startswith("FAIL") and failed_check in line for line in lines)


def test_a_simulated_stack_runs_no_judge_and_makes_no_chat_calls() -> None:
    fake, traces = make_fake()
    result = run_lab(fake, traces)
    assert result["mode"] == "simulated" and "judge_alignment" not in result
    assert not any(call.startswith(("POST /assistant", "POST /judges")) for call in fake.calls)


def test_the_safety_judge_spec_is_a_valid_judge() -> None:
    judge = form_values(ASSETS / "judges/reply-safety.judge.json")
    assert set(judge) == {"name", "feedback_value_type", "instructions"}
    assert judge["feedback_value_type"] == "bool"
    assert "{{ inputs }}" in judge["instructions"] and "{{ outputs }}" in judge["instructions"]
    assert "\n" not in judge["instructions"]


def test_the_goals_dataset_matches_the_planner_and_is_varied() -> None:
    goals = load_jsonl(ASSETS / "dataset/goals.jsonl")
    assert len(goals) == 26
    assert len({g["id"] for g in goals}) == 26 and len({g["goal"] for g in goals}) == 26
    for goal in goals:
        drawn = [step["cap"]["key"] for step in plan_steps(goal["goal"])]
        assert drawn == goal["expected_steps"], goal["id"]
    assert {g["outcome"] for g in goals} == {"as_asked", "missed", "nothing", "unasked"}
    assert all(g["note"] for g in goals)
    assert {g["id"] for g in goals if not g["expected_steps"]} == {
        g["id"] for g in goals if g["outcome"] == "nothing"
    }
    # A review queue always brings its add_items step.
    assert all(
        "review_queue.add_items" in g["expected_steps"]
        for g in goals
        if "review_queue.create" in g["expected_steps"]
    )


def test_the_agent_replies_are_complete_and_varied() -> None:
    replies = rows()
    assert len(replies) == 30 and len({r["id"] for r in replies}) == 30
    assert len({r["reply"] for r in replies}) == 30
    queue = form_values(ASSETS / "review-queues/agent-reply-review.queue.json")
    keys = {q["key"] for q in queue["questions"]}
    required = {q["key"] for q in queue["questions"] if q["required"]}
    options = next(q for q in queue["questions"] if q["key"] == "severity")["options"]
    for row in replies:
        review = row["review"]
        assert required <= set(review) <= keys, row["id"]
        assert all(isinstance(review[k], bool) for k in ("safety", "citation_ok", "tone_ok"))
        # an unsafe reply carries a severity, a safe one does not
        assert (review.get("severity") in options) == (review["safety"] is False), row["id"]
    unsafe = [r for r in replies if not r["review"]["safety"]]
    bad_citation = [r for r in replies if not r["review"]["citation_ok"]]
    bad_tone = [r for r in replies if not r["review"]["tone_ok"]]
    clean = [
        r for r in replies if all(r["review"][k] for k in ("safety", "citation_ok", "tone_ok"))
    ]
    assert (len(unsafe), len(bad_citation), len(bad_tone), len(clean)) == (7, 10, 6, 12)
    # The twelve the lab reviews by hand cover every kind of problem.
    reviewed = [r for r in replies if r["id"] in lab.REVIEWED_IDS]
    assert len(reviewed) == 12
    assert any(not r["review"]["safety"] for r in reviewed)
    assert any(not r["review"]["citation_ok"] for r in reviewed)
    assert any(not r["review"]["tone_ok"] for r in reviewed)
    assert any(all(r["review"][k] for k in ("safety", "citation_ok", "tone_ok")) for r in reviewed)


def test_the_form_specs_are_valid() -> None:
    queue = form_values(ASSETS / "review-queues/agent-reply-review.queue.json")
    assert set(queue) == {"name", "description", "reviewers", "questions"}
    enqueue = form_values(ASSETS / "enqueue.json")
    assert set(enqueue) == {"trace_ids", "assigned_to"}
    expected = json.loads((ASSETS / "expected-plan.json").read_text("utf-8"))
    assert [s["capability_key"] for s in expected["expected_steps"]] == [
        "review_queue.create",
        "review_queue.add_items",
    ]
    assert expected["goal"] == lab.INTENT


def test_the_script_notices_a_plan_that_stays_stuck_after_a_skip() -> None:
    """With ``stuck_skip`` the plan stays paused with no question to answer."""
    fake, traces = make_fake(bend="stuck_skip")
    lines: list[str] = []
    with pytest.raises(VerificationError):
        run_lab(fake, traces, lines)
    assert any(
        line.startswith("FAIL") and "skipping the queue skips the enqueue step" in line
        for line in lines
    )


def test_the_script_notices_a_plan_that_runs_for_an_owner_without_the_scope() -> None:
    fake, traces = make_fake(bend="no_scope_floor")
    lines: list[str] = []
    with pytest.raises(VerificationError):
        run_lab(fake, traces, lines)
    assert any(
        line.startswith("FAIL") and "a plan owned by an account without the operator scope" in line
        for line in lines
    )


def test_a_reviewed_trace_that_is_missing_from_the_server_is_an_api_error() -> None:
    """If a trace the lab reviews is not there, the write-back fails (502); the script lets the
    error through and ``main`` reports it as a failed verification (next test)."""
    fake, traces = make_fake()
    fake.known_traces.pop(traces["R01"])
    with pytest.raises(CaliberAPIError, match="failed writing review answers"):
        run_lab(fake, traces)


def test_main_reports_an_api_error_as_a_failed_verification(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake, traces = make_fake()
    fake.known_traces.pop(traces["R01"])
    monkeypatch.setenv("CALIBER_BASE_URL", "https://caliber.test")
    monkeypatch.setattr(lab, "env_client", lambda **_: fake.client(CaliberClient))
    monkeypatch.setattr(
        lab, "sign_in", lambda _b, user, _p: {"olga": "session-olga", "vera": "session-vera"}[user]
    )
    monkeypatch.setattr(lab, "seed_traces", lambda _assets: traces)

    class Routed(CaliberClient):
        def __init__(self, base_url: str, *, token: str) -> None:
            super().__init__(
                base_url, token=token, http_client=httpx.Client(transport=fake.transport())
            )

    monkeypatch.setattr(lab, "CaliberClient", Routed)
    assert lab.main([]) == 1
    assert "VERIFICATION FAILED" in capsys.readouterr().err


def test_setup_problems_are_named_not_stack_traces() -> None:
    fake, traces = make_fake()
    people = clients(fake)
    with pytest.raises(SetupError, match="admin personal access token"):
        lab.run(people["olga"], operator=people["olga"], viewer=people["vera"], traces=traces)
    with pytest.raises(SetupError, match="CALIBER_OPERATOR_USERS=admin,olga"):
        lab.run(people["admin"], operator=people["vera"], viewer=people["vera"], traces=traces)
    off, _ = make_fake()
    off.capabilities = lambda **_: httpx.Response(  # type: ignore[method-assign]
        404, json={"detail": "disabled", "status_code": 404, "reason_code": "feature_disabled"}
    )
    off_people = clients(off)
    with pytest.raises(SetupError, match="CALIBER_FEATURE_ARIA=true"):
        lab.run(
            off_people["admin"],
            operator=off_people["olga"],
            viewer=off_people["vera"],
            traces=traces,
        )


def test_seed_traces_loads_the_assets_script(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    written: list[tuple[str, Any]] = []

    class _Span:
        trace_id = "tr-fake"

        def __enter__(self) -> _Span:
            _Span.trace_id = f"tr-{len(written)}"
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def set_inputs(self, value: Any) -> None:
            written.append(("inputs", value))

        def set_outputs(self, value: Any) -> None:
            written.append(("outputs", value))

    fake_mlflow = types.SimpleNamespace(
        set_experiment=lambda name: written.append(("experiment", name)),
        start_span=lambda **_: _Span(),
        set_trace_tag=lambda trace_id, key, value: written.append((key, value)),
        flush_trace_async_logging=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "mlflow", fake_mlflow)
    mapping = lab.seed_traces(ASSETS)
    assert (
        sorted(mapping) == sorted(row["id"] for row in rows()) and len(set(mapping.values())) == 30
    )
    assert ("experiment", "cookbook-13-flagged-replies") in written
    assert ("cookbook13.row", "R01") in written


def test_main_runs_the_lab_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake, traces = make_fake()
    monkeypatch.setenv("CALIBER_BASE_URL", "https://caliber.test")
    monkeypatch.setattr(lab, "env_client", lambda **_: fake.client(CaliberClient))
    monkeypatch.setattr(
        lab,
        "sign_in",
        lambda _base, user, _password: {"olga": "session-olga", "vera": "session-vera"}[user],
    )
    monkeypatch.setattr(lab, "seed_traces", lambda _assets: traces)

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
    assert cleaned["review_queues"] >= 2 and cleaned["judges"] == 0
    assert all(
        q["status"] == "archived"
        for q in fake.queues.values()
        if q["name"].startswith("cookbook-13-")
    )


def test_main_reports_a_setup_problem_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake, _ = make_fake()
    fake.tokens["calpat_admin"] = "olga"  # the token belongs to a non-admin
    monkeypatch.setenv("CALIBER_BASE_URL", "https://caliber.test")
    monkeypatch.setattr(lab, "env_client", lambda **_: fake.client(CaliberClient))
    monkeypatch.setattr(lab, "sign_in", lambda *_: "session-olga")
    monkeypatch.setattr(lab, "ensure_accounts", lambda *_: None)
    assert lab.main([]) == 1
    assert "VERIFICATION FAILED" in capsys.readouterr().err
