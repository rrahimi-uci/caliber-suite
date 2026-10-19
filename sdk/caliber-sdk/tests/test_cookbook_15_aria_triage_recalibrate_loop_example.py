"""Cookbook 15 (triage weak runs and recalibrate): the script, its data files, its refusals.

The script runs against ``cookbook_12_15_fake_server.FakeAria``, an in-memory imitation of the
routes it calls (the traces are handed to it, so no MLflow is needed; the calibration job and the
plan worker are simulated). The fake restates the planner, the plan executor and the async parking
independently of the server, so these tests check that the dataset files, the fake and the script
agree, and that the script *fails*, naming the check, against a server that stops following a rule.
Running the script against a live server (a real worker, a real refinement job) is how the real
behaviour is verified; see the lab README.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

import httpx
import pytest
from cookbook_12_15_fake_server import FakeAria, plan_steps

from caliber_sdk import CaliberClient
from examples.cookbooks import cookbook_15_aria_triage_recalibrate_loop as lab
from examples.cookbooks._aria_lab import (
    SetupError,
    VerificationError,
    form_values,
    load_jsonl,
)

ASSETS = Path(lab.default_assets_dir())


def runs() -> list[dict[str, Any]]:
    return load_jsonl(ASSETS / "dataset/support-runs.jsonl")


def make_fake(**kwargs: Any) -> tuple[FakeAria, dict[str, str]]:
    weak = [row for row in runs() if row["weak"]]
    traces = {row["id"]: f"tr-{row['id'].lower()}" for row in weak}
    known = {traces[row["id"]]: row["question"] for row in weak}
    fake = FakeAria(traces=known, **kwargs)
    fake.job_reads = 1
    return fake, traces


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
    kwargs.setdefault("patience", 0.01)
    return lab.run(
        people["admin"],
        operator=people["olga"],
        viewer=people["vera"],
        suffix="unit",
        traces=traces,
        report=(lines.append if lines is not None else print),
        sleep=lambda _seconds: None,
        **kwargs,
    )


def test_the_lab_passes_against_a_simulated_stack_where_the_calibration_is_rejected() -> None:
    fake, traces = make_fake()
    lines: list[str] = []
    result = run_lab(fake, traces, lines)
    assert lines[-1] == "\nAll documented outcomes verified."
    assert all(line.startswith("ok") for line in lines[:-1])
    assert result["calibration"]["job_status"] == "rejected"
    assert result["calibration"]["plan_status"] == "failed"
    assert result["calibration"]["advanced_by"] == "worker"
    assert result["triage"] == {"reviewed": 3, "pending": 9}
    queue = fake.queues[result["queue_id"]]
    assert queue["name"] == "cookbook-15-weak-runs-triage-unit"
    assert len(fake.items[result["queue_id"]]) == 12
    # One calibration job only: the refusals queued nothing.
    assert len(fake.jobs) == 1
    # The reviewer's verdict and the expected answer are on the triaged traces.
    stored = {a["name"]: a["value"] for a in fake.assessments[traces["S21"]]}
    assert stored["failure_mode"] == "wrong_tool" and stored["resolution_correct"] is False
    assert stored["expected_answer"].startswith("Order 5530 shipped")


def test_the_lab_passes_on_a_stack_where_the_calibration_finds_a_candidate() -> None:
    fake, traces = make_fake()
    fake.calibration_outcome = "candidate_ready"
    lines: list[str] = []
    result = run_lab(fake, traces, lines)
    assert result["calibration"]["job_status"] == "candidate_ready"
    assert result["calibration"]["plan_status"] == "completed"
    assert any("the calibration found a candidate: the plan completed" in line for line in lines)


def test_a_real_provider_run_checks_ask_aria_and_follows_the_job_either_way() -> None:
    for outcome, plan_status in (("rejected", "failed"), ("candidate_ready", "completed")):
        fake, traces = make_fake(simulated=False)
        fake.calibration_outcome = outcome
        lines: list[str] = []
        result = run_lab(fake, traces, lines)
        assert lines[-1] == "\nAll documented outcomes verified."
        assert result["mode"] == "real"
        assert result["calibration"]["job_status"] == outcome
        assert result["calibration"]["plan_status"] == plan_status
        assert result["chat"]["acting"] == ["judge_list", "review_queue_create"]
        assert result["chat"]["reading"] == ["judge_list"]
        queues = {q["name"] for q in fake.queues.values()}
        assert "cookbook-15-chat-queue-unit" in queues
        assert "cookbook-15-chat-manual-unit" not in queues
        assert len(fake.jobs) == 1  # the chat never started a calibration


def test_a_real_run_notices_a_chat_that_ignores_its_permission_boundary() -> None:
    fake, traces = make_fake(simulated=False, bend="chat_always_acts")
    lines: list[str] = []
    with pytest.raises(VerificationError):
        run_lab(fake, traces, lines)
    assert any(
        line.startswith("FAIL") and "is not offered: nothing was created" in line for line in lines
    )


def test_a_simulated_stack_makes_no_chat_calls_and_waits_a_short_while() -> None:
    fake, traces = make_fake()
    result = run_lab(fake, traces)
    assert result["mode"] == "simulated" and "chat" not in result
    assert not any(call.startswith("POST /assistant") for call in fake.calls)


def test_the_script_waits_longer_for_a_real_calibration(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []
    real_wait = lab.wait_for_resume

    def spy(*args: Any, patience: float, **kwargs: Any) -> Any:
        seen.append(patience)
        return real_wait(*args, patience=patience, **kwargs)

    monkeypatch.setattr(lab, "wait_for_resume", spy)
    for simulated in (True, False):
        fake, traces = make_fake(simulated=simulated)
        people = clients(fake)
        lab.run(
            people["admin"],
            operator=people["olga"],
            viewer=people["vera"],
            suffix="unit",
            traces=traces,
            report=lambda _line: None,
            sleep=lambda _seconds: None,
        )
    assert seen == [lab.WORKER_PATIENCE_SECONDS, lab.REAL_PATIENCE_SECONDS]
    assert lab.REAL_PATIENCE_SECONDS > lab.WORKER_PATIENCE_SECONDS


def test_without_a_worker_the_script_advances_the_plan_with_poll() -> None:
    fake, traces = make_fake()
    fake.worker = False
    result = run_lab(fake, traces)
    assert result["calibration"]["advanced_by"] == "poll"
    assert result["calibration"]["plan_status"] == "failed"
    assert any(
        call.startswith("POST /aria/plans/") and call.endswith("/poll") for call in fake.calls
    )


def test_the_goals_dataset_matches_the_planner_and_is_varied() -> None:
    goals = load_jsonl(ASSETS / "dataset/goals.jsonl")
    assert len(goals) == 28
    assert len({g["id"] for g in goals}) == 28 and len({g["goal"] for g in goals}) == 28
    for goal in goals:
        drawn = [step["cap"]["key"] for step in plan_steps(goal["goal"])]
        assert drawn == goal["expected_steps"], goal["id"]
    assert {g["outcome"] for g in goals} == {"as_asked", "unasked", "nothing"}
    assert all(g["note"] for g in goals)
    assert {g["id"] for g in goals if not g["expected_steps"]} == {
        g["id"] for g in goals if g["outcome"] == "nothing"
    }
    registry = [
        "judge.create",
        "review_queue.create",
        "eval_dataset.create",
        "review_queue.add_items",
        "workflow.calibrate",
    ]
    for goal in goals:
        assert goal["expected_steps"] == [k for k in registry if k in goal["expected_steps"]]
    # The word workflow plans a calibration whatever the sentence is about.
    assert all(
        "workflow.calibrate" in g["expected_steps"]
        for g in goals
        if "workflow" in g["goal"].lower()
    )


def test_the_support_runs_are_complete_consistent_and_varied() -> None:
    rows = runs()
    assert len(rows) == 30 and len({r["id"] for r in rows}) == 30
    assert len({r["question"] for r in rows}) == 30
    assert all(r["reference"] and r["reply"] is not None for r in rows)
    weak = [r for r in rows if r["weak"]]
    assert len(weak) == 12
    for row in rows:
        skipped_a_needed_tool = not set(row["required_tools"]) <= set(row["tools_called"])
        assert row["weak"] == skipped_a_needed_tool == bool(row["reason"]), row["id"]
    queue = form_values(ASSETS / "review-queues/triage-review.queue.json")
    modes = next(q for q in queue["questions"] if q["key"] == "failure_mode")["options"]
    assert {r["reason"].split(":", 1)[0] for r in weak} <= set(modes)
    assert {r["reason"].split(":", 1)[0] for r in weak} == {
        "missing_evidence",
        "wrong_tool",
        "hallucinated_claim",
        "bad_escalation",
    }
    for row_id in lab.TRIAGED:
        assert next(r for r in rows if r["id"] == row_id)["weak"]


def test_the_subject_files_are_consistent() -> None:
    manifest = json.loads(
        (ASSETS / "workflows/support-policy-agent.workflow.json").read_text("utf-8")
    )
    tools = {
        spec["name"]: spec
        for spec in (form_values(p) for p in sorted((ASSETS / "tools").glob("*.tool.json")))
    }
    assert set(tools) == {"lookup_policy", "get_order"}
    assert set(manifest["nodes"]["support_agent"]["tools"]) == set(tools)
    assert set(manifest["tools"]) == set(tools)
    assert all(t["side_effect_level"] == "read" for t in tools.values())
    gate = next(iter(manifest["deploy_gates"].values()))
    assert gate["thresholds"] == {"min_completion_rate": 1.0}
    assert manifest["artifacts"]["eval_datasets"][gate["dataset_ref"]]["dataset_name"]
    queue = form_values(ASSETS / "review-queues/triage-review.queue.json")
    assert [q["key"] for q in queue["questions"]] == [
        "resolution_correct",
        "failure_mode",
        "severity",
        "expected_answer",
    ]
    assert next(q for q in queue["questions"] if q["key"] == "expected_answer")["target"] == (
        "expectation"
    )
    expected = json.loads((ASSETS / "expected-plan.json").read_text("utf-8"))
    assert [s["capability_key"] for s in expected["expected_steps"]] == [
        "review_queue.create",
        "review_queue.add_items",
        "workflow.calibrate",
    ]
    assert expected["goal"] == lab.INTENT
    assert set(json.loads((ASSETS / "calibrate.json").read_text("utf-8"))) == {
        "workflow_id",
        "agent_id",
        "notes",
    }


def test_the_script_notices_a_calibration_plan_that_does_not_follow_its_job() -> None:
    """If the plan stayed 'completed' while the job was rejected, the mirror check fails."""
    fake, traces = make_fake()

    def lying(plan: dict[str, Any], step: dict[str, Any]) -> bool:
        step["status"] = "done"
        step["result"] = {}
        return True

    fake._resolve_job = lying  # type: ignore[method-assign]
    lines: list[str] = []
    with pytest.raises(VerificationError):
        run_lab(fake, traces, lines)
    assert any(line.startswith("FAIL") and "ends where the job ended" in line for line in lines)


@pytest.mark.parametrize(
    ("bend", "failed_check"),
    [
        ("no_scope_floor", "a plan owned by an account without the operator scope"),
    ],
)
def test_the_script_notices_a_server_that_breaks_a_rule(bend: str, failed_check: str) -> None:
    fake, traces = make_fake(bend=bend)
    lines: list[str] = []
    with pytest.raises(VerificationError):
        run_lab(fake, traces, lines)
    assert any(line.startswith("FAIL") and failed_check in line for line in lines)


def test_a_plan_stuck_after_a_skip_fails_loudly() -> None:
    fake, traces = make_fake(bend="stuck_skip")
    with pytest.raises(VerificationError):
        run_lab(fake, traces)


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


def test_the_assets_scripts_load_and_seed_only_the_weak_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    monkeypatch.setitem(
        sys.modules,
        "mlflow",
        types.SimpleNamespace(
            set_experiment=lambda name: written.append(("experiment", name)),
            start_span=lambda **_: _Span(),
            set_trace_tag=lambda _trace, key, value: written.append((key, value)),
            flush_trace_async_logging=lambda: None,
        ),
    )
    mapping = lab.seed_traces(ASSETS)
    assert sorted(mapping) == sorted(r["id"] for r in runs() if r["weak"])
    assert ("experiment", "cookbook-15-weak-support-runs") in written
    assert ("cookbook15.failure_mode", "wrong_tool") in written
    assert not any(value == "S01" for _key, value in written)  # a run the agent got right
    assert lab.load_subject_setup(ASSETS).NAME_PREFIX == "cookbook-15-"


def test_the_subject_setup_registers_each_tool_once_and_builds_the_subject() -> None:
    fake, _ = make_fake()
    admin = fake.client(CaliberClient)
    module = lab.load_subject_setup(ASSETS)
    first = module.setup(admin, "one", ASSETS)
    second = module.setup(admin, "two", ASSETS)
    assert first["tools"] == second["tools"] == ["get_order", "lookup_policy"]
    assert len(fake.tools) == 2  # the second run kept the tools it found
    assert first["workflow_id"] != second["workflow_id"] and first["agent_id"] != second["agent_id"]
    assert fake.versions[first["version_id"]]["status"] == "published"
    assert len(fake.examples[first["dataset_id"]]) == 30
    assert (
        fake.workflows[first["workflow_id"]]["manifest"]["artifacts"]["eval_datasets"][
            "support_eval"
        ]["dataset_name"]
        == first["gate_name"]
    )


def test_the_subject_setup_signs_in_when_it_has_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    module = lab.load_subject_setup(ASSETS)
    monkeypatch.setenv("CALIBER_BASE_URL", "https://caliber.test")
    monkeypatch.delenv("CALIBER_TOKEN", raising=False)
    monkeypatch.setenv("CALIBER_USER", "admin")
    monkeypatch.setenv("CALIBER_PASSWORD", "pw")
    seen: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        seen.update(url=url, **kwargs)
        return httpx.Response(
            200,
            json={"data": {"user_id": "admin"}},
            headers={"set-cookie": "caliber_session=session-abc; Path=/"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    client = module._client()
    assert seen["json"] == {"user_id": "admin", "password": "pw"}
    assert seen["url"].endswith("/ajax-api/2.0/mlflow/caliber/auth/login")
    assert client is not None
    monkeypatch.setenv("CALIBER_TOKEN", "calpat_x")
    assert module._client() is not None


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
    assert "All documented outcomes verified." in out and '"calibration"' in out
    assert lab.main(["--cleanup"]) == 0
    cleaned = json.loads(capsys.readouterr().out)
    assert cleaned["review_queues"] >= 1 and cleaned["test_sets"] >= 1 and cleaned["workflows"] >= 2
    assert all(
        q["status"] == "archived"
        for q in fake.queues.values()
        if q["name"].startswith("cookbook-15-")
    )


def test_main_reports_a_setup_problem_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake, _ = make_fake()
    fake.tokens["calpat_admin"] = "olga"
    monkeypatch.setenv("CALIBER_BASE_URL", "https://caliber.test")
    monkeypatch.setattr(lab, "env_client", lambda **_: fake.client(CaliberClient))
    monkeypatch.setattr(lab, "sign_in", lambda *_: "session-olga")
    monkeypatch.setattr(lab, "ensure_accounts", lambda *_: None)
    assert lab.main([]) == 1
    assert "VERIFICATION FAILED" in capsys.readouterr().err


def test_load_helpers_name_a_missing_script(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        lab.load_subject_setup(tmp_path)
    with pytest.raises(FileNotFoundError):
        lab.seed_traces(tmp_path)
    assert importlib.util.find_spec("examples.cookbooks._aria_lab") is not None
