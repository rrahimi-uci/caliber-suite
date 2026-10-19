"""The full SDK cookbook examples are executable and stay tied to the docs."""

from __future__ import annotations

import csv
import io
import json as jsonlib
from typing import Any

import httpx
import pytest

from caliber_sdk import CaliberClient
from examples.cookbooks.cookbook_01_trustworthy_intake_classifier import run as run_01
from examples.cookbooks.cookbook_02_precision_skills import assets_dir, load_cases
from examples.cookbooks.cookbook_02_precision_skills import run as run_02
from examples.cookbooks.cookbook_09_self_healing_workflows import run as run_09
from examples.cookbooks.cookbook_10_trustworthy_evaluation import run as run_10
from examples.cookbooks.cookbook_16_observability_triage import run as run_16
from examples.cookbooks.cookbook_17_financial_analysis import (
    CSV_FIELDS,
    MONTHLY_FINANCIALS,
    PROMPT_ALIAS,
    PROMPT_TEMPLATE,
    build_financial_csv,
    expected_statistics,
    parse_analysis,
    parse_turn_analysis,
)
from examples.cookbooks.cookbook_17_financial_analysis import run as run_17
from examples.cookbooks.cookbook_18_projects_roles_tokens_audit import run as run_18

BASE = "https://caliber.test"


def stub_server(routes: dict[str, Any]) -> CaliberClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.rsplit("/caliber", 1)[-1]
        key = f"{request.method} {path}"
        if key not in routes:  # pragma: no cover
            raise AssertionError(f"example called an unstubbed route: {key}")
        body = routes[key]
        payload = body(request) if callable(body) else body
        if isinstance(payload, httpx.Response):
            return payload
        return httpx.Response(200, json={"data": payload})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return CaliberClient(BASE, token="calpat_test", http_client=http)


def recipe_payload(
    cookbook_id: str,
    title: str,
    *,
    prerequisites: list[str] | None = None,
) -> dict[str, Any]:
    prerequisites = prerequisites or []
    checks = [{"label": item, "status": "operator_confirmation_required"} for item in prerequisites]
    return {
        "recipes": [
            {
                "id": cookbook_id,
                "title": title,
                "summary": title,
                "prerequisites": prerequisites,
                "readiness": {
                    "status": "configuration_required" if checks else "ready",
                    "checks": checks,
                },
            }
        ]
    }


def cookbook_listing(
    cookbook_id: str,
    title: str,
    *,
    prerequisite: str | None = None,
) -> dict[str, Any]:
    return recipe_payload(
        cookbook_id,
        title,
        prerequisites=[prerequisite] if prerequisite else None,
    )


PROMPT_01 = "cookbook-01-intake-classifier-t"


def _error(status: int, detail: str) -> httpx.Response:
    return httpx.Response(status, json={"detail": detail, "status_code": status})


def _error_forbidden(request: httpx.Request) -> httpx.Response:
    return _error(403, "missing required scope; expected one of ['caliber.operator']")


def _dataset_rows_01() -> int:
    from examples.cookbooks.cookbook_01_trustworthy_intake_classifier import (
        default_assets_dir,
        load_dataset,
    )

    return len(load_dataset(default_assets_dir() / "dataset/intake-classifier.jsonl"))


def _stub_01(
    *,
    llm_simulated: bool,
    registry: dict[str, int],
    weak_keeps_json: bool = False,
    eval_error: str | None = None,
    real_job: str = "candidate_ready",
    noop_candidate: bool = False,
    v2_pass_rate: tuple[float, float] | None = None,
    seen: dict[str, Any] | None = None,
) -> CaliberClient:
    """A server for Cookbook 01, with the stateful bits (alias, applied jobs) the script checks.

    ``llm_simulated=False`` is a stack with a real model. ``weak_keeps_json`` makes the weaker v2
    still answer with a bare JSON record, as a real instruction-tuned model does (a stand-in model
    does not). ``eval_error`` makes every scored run fail the way the server reports it (201 with
    ``status: failed``). ``real_job`` is how a real calibration run ends. ``noop_candidate``
    makes the optimizer return the live prompt unchanged (``+0 / -0 lines``), as a strong hosted
    model did. ``v2_pass_rate`` overrides the (v1, v2) pass rates a real model scores. ``seen``
    collects the calibration requests.
    """
    n = _dataset_rows_01()
    seen = seen if seen is not None else {}
    seen.setdefault("calibrations", [])
    applied: set[str] = set()
    runs = iter(["RFN-good", "RFN-strict"])
    eval_runs = iter(["EVR-1", "EVR-2"])
    record = {
        "intent": "billing",
        "priority": "high",
        "confidence": 0.9,
        "needs_review": False,
        "reason": "r",
    }

    def evaluation(request: httpx.Request) -> Any:
        body = jsonlib.loads(request.content)
        if body["dataset_id"] != "ED-01":
            return _error(404, f"eval dataset {body['dataset_id']!r} not found")
        if llm_simulated:
            return _error(400, "evaluation requires a real LLM provider; set CALIBER_LLM_PROVIDER")
        if eval_error:
            return {
                "run_id": next(eval_runs),
                "status": "failed",
                "label": body["label"],
                "n_examples": n,
                "pass_rate": 0.0,
                "aggregate": {},
                "error_message": eval_error,
                "results": [{"prediction": "", "error": eval_error}] * n,
            }
        strong = body["subject_ref"].endswith("@1")
        keeps_json = strong or weak_keeps_json
        prediction = jsonlib.dumps(record) if keeps_json else "Sure!\n```json\n{}\n```"
        if v2_pass_rate is not None:
            pass_rate, f1 = (v2_pass_rate[0], 0.4) if strong else (v2_pass_rate[1], 0.4)
        elif weak_keeps_json:
            pass_rate, f1 = (0.2, 0.43) if strong else (0.02, 0.35)
        else:
            pass_rate, f1 = (0.77, 0.54) if strong else (0.0, 0.27)
        return {
            "run_id": next(eval_runs),
            "status": "completed",
            "label": body["label"],
            "n_examples": n,
            "pass_rate": pass_rate,
            "aggregate": {"token_f1": f1},
            "results": [{"prediction": prediction}] * n,
        }

    def calibration(request: httpx.Request) -> dict[str, Any]:
        seen["calibrations"].append(jsonlib.loads(request.content))
        return {"job": {"job_id": next(runs)}}

    def job(request: httpx.Request) -> dict[str, Any]:
        job_id = request.url.path.rsplit("/", 1)[-1]
        if job_id == "RFN-strict":
            return {"job_id": job_id, "status": "rejected"}
        if not llm_simulated and real_job == "failed":
            return {
                "job_id": job_id,
                "status": "failed",
                "error_message": "eval provider error: no valid score for Guidelines",
            }
        if not llm_simulated and real_job == "rejected_bare":
            return {"job_id": job_id, "status": "rejected", "error_message": ""}
        if not llm_simulated and real_job == "rejected":
            return {
                "job_id": job_id,
                "status": "rejected",
                "error_message": "regression gate failed: overall 0.568 below min_aggregate_score",
                "eval_results": {
                    "provider": "mlflow",
                    "simulated": False,
                    "n_examples": n,
                    "gate": {
                        "passed": False,
                        "reasons": ["overall 0.568 below min_aggregate_score 0.850"],
                    },
                },
            }
        evidence = (
            {"provider": "fake", "simulated": True}
            if llm_simulated
            else {
                "provider": "mlflow",
                "simulated": False,
                "baseline": {"overall": 0.5227},
                "candidate": {"overall": 0.5682},
            }
        )
        return {
            "job_id": job_id,
            "status": "applied" if job_id in applied else "candidate_ready",
            "candidate": {
                "content": "old" if noop_candidate else "new",
                "baseline_content": "old",
                "diff_summary": "+0 / -0 lines" if noop_candidate else "+3 / -0 lines",
            },
            "eval_results": {
                "n_examples": n,
                "eval_dataset_version": n + 1,
                "gate": {"passed": True},
                **evidence,
            },
        }

    def apply(request: httpx.Request) -> Any:
        job_id = request.url.path.split("/")[-2]
        real_not_ready = not llm_simulated and real_job != "candidate_ready"
        if job_id in applied or job_id == "RFN-strict" or real_not_ready:
            return _error(409, f"job {job_id!r} cannot be applied")
        applied.add(job_id)
        registry["prod"] = 3
        return {"job_id": job_id, "promotion": {"artifact_ref": f"prompts:/{PROMPT_01}/3"}}

    def rollback(request: httpx.Request) -> dict[str, Any]:
        registry["prod"] = 1
        return {"version": 1, "rolled_back_from": 3}

    def live(request: httpx.Request) -> list[dict[str, Any]]:
        return [
            {
                "artifact_type": "prompt",
                "artifact_id": PROMPT_01,
                "version_id": f"v{registry['prod']}",
                "by": "@alice",
            }
        ]

    def audit(request: httpx.Request) -> dict[str, Any]:
        action = request.url.params["action"]
        details = {
            "promote_prompt": {"job_id": "RFN-good"},
            "apply_candidate": {},
            "rollback_prompt": {"to_version": 1},
        }[action]
        return {"entries": [{"log_id": 1, "actor": "@alice", "action": action, "details": details}]}

    return stub_server(
        {
            "GET /me": {"user_id": "@alice"},
            "GET /readiness": {"simulated": ["llm", "eval"] if llm_simulated else []},
            "GET /cookbooks": {"recipes": [{"id": "01", "title": "T", "steps": [{"id": "01.1"}]}]},
            "POST /prompts": {"name": PROMPT_01},
            f"POST /prompts/{PROMPT_01}/aliases/prod": {"version": 1},
            f"POST /prompts/{PROMPT_01}/versions": {"version": 2},
            "POST /eval-datasets": {"dataset_id": "ED-01"},
            "POST /eval-datasets/ED-01/examples": {"example_id": "EX-01"},
            "GET /eval-datasets/ED-01/examples": [{"example_id": f"EX-{i}"} for i in range(n)],
            "GET /eval-datasets/ED-01": {"dataset_id": "ED-01", "version": n + 1},
            "POST /evaluations": evaluation,
            "POST /prompts/calibration/runs": calibration,
            "GET /jobs/RFN-good": job,
            "GET /jobs/RFN-strict": job,
            "POST /jobs/RFN-good/apply": apply,
            "POST /jobs/RFN-strict/apply": apply,
            f"GET /prompts/{PROMPT_01}/versions": [
                {
                    "version": 3,
                    "provenance": {
                        "job_id": "RFN-good",
                        "eval_simulated": "true" if llm_simulated else "false",
                    },
                }
            ],
            "GET /releases/live": live,
            "GET /releases/timeline": lambda r: [
                {
                    "entity_id": PROMPT_01,
                    "action": "promote_prompt",
                    "actor": "@alice",
                    "details": {"from_version": 1, "to_version": 3},
                }
            ],
            "GET /audit-log": audit,
            f"POST /prompts/{PROMPT_01}/rollback": rollback,
        }
    )


def test_cookbook_01_releases_a_prompt_change_end_to_end_on_a_simulated_model() -> None:
    """The whole loop on the default simulated model: the evaluation is refused (and that is
    verified), the calibration evidence is marked simulated, a viewer cannot apply, a failing
    gate blocks, and Apply then rollback move the registry alias and leave an audit trail."""
    registry = {"prod": 1}
    viewer = stub_server({"POST /jobs/RFN-good/apply": _error_forbidden})
    with _stub_01(llm_simulated=True, registry=registry) as caliber, viewer:
        result = run_01(
            caliber,
            suffix="t",
            viewer=viewer,
            registry=lambda name: registry["prod"],
            poll_interval=0.01,
        )
    assert result["prompt_name"] == PROMPT_01
    assert result["llm_simulated"] is True
    assert result["evaluation"] == "refused: needs a real model"
    assert result["examples"] == _dataset_rows_01()
    assert result["applied_version"] == 3
    assert result["evidence"]["simulated"] is True
    assert result["blocked_job_id"] == "RFN-strict"
    assert all(result["checks"].values())
    assert registry["prod"] == 1  # rolled back


def test_cookbook_01_catches_the_regression_with_a_model_that_answers() -> None:
    """With a model, evaluation scores v1 and the weaker v2 and the contract checks catch it."""
    registry = {"prod": 1}
    with _stub_01(llm_simulated=False, registry=registry) as caliber:
        result = run_01(
            caliber,
            suffix="t",
            registry=lambda name: registry["prod"],
            skip_release=True,
            expect_regression=True,
        )
    evaluation = result["evaluation"]
    assert evaluation["contract_v1"]["valid_json"] == _dataset_rows_01()
    assert evaluation["contract_v2"]["valid_json"] == 0
    assert evaluation["regressed_examples"] == _dataset_rows_01()
    assert evaluation["pass_rate"] == [0.77, 0.0]
    assert "applied_version" not in result


def test_cookbook_01_fails_loudly_when_a_documented_outcome_does_not_hold() -> None:
    from examples.cookbooks.cookbook_01_trustworthy_intake_classifier import (
        LabVerificationError,
    )

    registry = {"prod": 2}  # the registry disagrees: v1 is not live
    with (
        _stub_01(llm_simulated=False, registry=registry) as caliber,
        pytest.raises(LabVerificationError, match="v1 is live in the MLflow registry"),
    ):
        run_01(caliber, suffix="t", registry=lambda name: registry["prod"], skip_release=True)


def test_cookbook_01_real_model_loop_asserts_invariants_not_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a stack with a real model (``simulated`` empty) the same script scores both prompts, and
    a v2 that still answers with JSON (a real model usually does) is not a failure: only
    invariants hold. The calibration evidence must be real, not marked simulated, the calibration
    scorer is the configured ``Guidelines`` judge (``Correctness`` needs labels this test set lacks)
    and the gate overrides come from the environment."""
    monkeypatch.setenv("COOKBOOK_01_MIN_SCORE", "0.5")
    monkeypatch.setenv("COOKBOOK_01_MAX_REGRESSION", "0.25")
    registry = {"prod": 1}
    seen: dict[str, Any] = {}
    viewer = stub_server({"POST /jobs/RFN-good/apply": _error_forbidden})
    with (
        _stub_01(
            llm_simulated=False, registry=registry, weak_keeps_json=True, seen=seen
        ) as caliber,
        viewer,
    ):
        result = run_01(
            caliber,
            suffix="t",
            viewer=viewer,
            registry=lambda name: registry["prod"],
            poll_interval=0.01,
        )
    assert result["llm_simulated"] is False and result["eval_simulated"] is False
    evaluation = result["evaluation"]
    assert evaluation["contract_v2"]["valid_json"] == _dataset_rows_01()  # the contract held
    assert evaluation["regressed_examples"] == 0
    assert evaluation["gate_fires"] is True  # the scorecard still caught it
    assert result["evidence"] == {
        "provider": "mlflow",
        "simulated": False,
        "n_examples": _dataset_rows_01(),
        "baseline": 0.5227,
        "candidate": 0.5682,
    }
    assert "blocked_job_id" not in result  # a real judge could score 1.0: no strict-gate check
    assert len(seen["calibrations"]) == 1
    request = seen["calibrations"][0]
    assert request["scorers"][0]["name"] == "Guidelines"
    assert request["scorers"][0]["config"]["guidelines"]
    assert request["gate"] == {"min_aggregate_score": 0.5, "max_regression_delta": 0.25}
    assert result["applied_version"] == 3
    assert all(result["checks"].values())
    assert registry["prod"] == 1  # rolled back


def test_cookbook_01_real_model_tolerates_an_unchanged_candidate_and_a_noisy_scorecard(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A strong hosted model: the optimizer may propose no edit (+0 / -0 lines), and v2 may score
    a row or two above v1 on the text-overlap scorecard. Neither is a failure; a clear win for v2
    (more than the noise margin) still is."""
    monkeypatch.delenv("COOKBOOK_01_MIN_SCORE", raising=False)
    monkeypatch.delenv("COOKBOOK_01_MAX_REGRESSION", raising=False)
    registry = {"prod": 1}
    with _stub_01(
        llm_simulated=False,
        registry=registry,
        noop_candidate=True,
        v2_pass_rate=(0.0227, 0.0455),  # v2 one row better: inside the noise margin
    ) as caliber:
        result = run_01(
            caliber, suffix="t", registry=lambda name: registry["prod"], poll_interval=0.01
        )
    out = capsys.readouterr().out
    assert "ok   the optimizer returned a candidate  [+0 / -0 lines]" in out
    assert "info the optimizer proposed no edit" in out
    assert "the candidate differs from the live prompt" not in out
    assert all(result["checks"].values())


def test_cookbook_01_real_model_where_v2_clearly_beats_v1_fails_the_scorecard_check(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from examples.cookbooks.cookbook_01_trustworthy_intake_classifier import (
        LabVerificationError,
    )

    registry = {"prod": 1}
    with (
        _stub_01(llm_simulated=False, registry=registry, v2_pass_rate=(0.1, 0.4)) as caliber,
        pytest.raises(LabVerificationError, match="the weaker v2 does not beat the baseline"),
    ):
        run_01(
            caliber,
            suffix="t",
            registry=lambda name: registry["prod"],
            poll_interval=0.01,
            skip_release=True,
        )
    assert (
        "FAIL the weaker v2 does not beat the baseline on the scorecard" in capsys.readouterr().out
    )


def test_cookbook_01_real_model_without_gate_overrides_uses_the_product_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COOKBOOK_01_MIN_SCORE", raising=False)
    monkeypatch.delenv("COOKBOOK_01_MAX_REGRESSION", raising=False)
    registry = {"prod": 1}
    seen: dict[str, Any] = {}
    with _stub_01(llm_simulated=False, registry=registry, seen=seen) as caliber:
        run_01(
            caliber,
            suffix="t",
            registry=lambda name: registry["prod"],
            poll_interval=0.01,
            calibration_timeout=5,
        )
    assert "gate" not in seen["calibrations"][0]


def test_cookbook_01_reports_a_failed_evaluation_with_the_server_error() -> None:
    """A run that cannot reach the model comes back 201 with ``status: failed`` and an empty
    aggregate. The script used to die with ``KeyError: 'token_f1'``; it now names the cause."""
    from examples.cookbooks.cookbook_01_trustworthy_intake_classifier import (
        LabVerificationError,
    )

    registry = {"prod": 1}
    with (
        _stub_01(llm_simulated=False, registry=registry, eval_error="Connection error.") as caliber,
        pytest.raises(LabVerificationError) as raised,
    ):
        run_01(caliber, suffix="t", registry=lambda name: registry["prod"], skip_release=True)
    assert "evaluation baseline v1 completed on every example" in str(raised.value)


def test_cookbook_01_failed_evaluation_message_carries_the_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from examples.cookbooks.cookbook_01_trustworthy_intake_classifier import (
        LabVerificationError,
    )

    registry = {"prod": 1}
    with (
        _stub_01(
            llm_simulated=False, registry=registry, eval_error="Error code: 404 - model not found"
        ) as caliber,
        pytest.raises(LabVerificationError),
    ):
        run_01(caliber, suffix="t", registry=lambda name: registry["prod"], skip_release=True)
    out = capsys.readouterr().out
    assert "FAIL evaluation baseline v1 completed on every example" in out
    assert "evaluation failed: Error code: 404 - model not found" in out


def test_cookbook_01_real_calibration_rejected_by_the_gate_is_reported(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A real judge can honestly reject the candidate; the script says why and cannot apply it."""
    from examples.cookbooks.cookbook_01_trustworthy_intake_classifier import (
        LabVerificationError,
    )

    registry = {"prod": 1}
    with (
        _stub_01(llm_simulated=False, registry=registry, real_job="rejected") as caliber,
        pytest.raises(LabVerificationError, match="calibration reached candidate_ready"),
    ):
        run_01(
            caliber,
            suffix="t",
            registry=lambda name: registry["prod"],
            poll_interval=0.01,
            calibration_timeout=5,
        )
    out = capsys.readouterr().out
    assert "ok   a rejected candidate cannot be applied (409)" in out
    assert "rejected by the gate: regression gate failed: overall 0.568" in out
    assert registry["prod"] == 1  # nothing was applied


def test_cookbook_01_accepts_an_honest_real_rejection_when_asked(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--accept-rejection``: a real rejection with a reason and real evidence is a verified
    outcome (exit 0, nothing applied), not a failure."""
    registry = {"prod": 1}
    with _stub_01(llm_simulated=False, registry=registry, real_job="rejected") as caliber:
        result = run_01(
            caliber,
            suffix="t",
            registry=lambda name: registry["prod"],
            poll_interval=0.01,
            calibration_timeout=5,
            accept_rejection=True,
        )
    out = capsys.readouterr().out
    assert "ok   a rejected candidate cannot be applied (409)" in out
    assert "ok   the gate rejected the candidate and says why" in out
    assert "ok   the rejection rests on real evidence" in out
    assert "calibration reached candidate_ready" not in out
    assert result["rejected"] is True
    assert "min_aggregate_score" in result["rejection"]
    assert registry["prod"] == 1  # nothing was applied


def test_cookbook_01_accepting_a_rejection_still_requires_it_to_be_explained_and_real(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The flag does not turn a rejection without evidence into a pass."""
    from examples.cookbooks.cookbook_01_trustworthy_intake_classifier import (
        LabVerificationError,
    )

    registry = {"prod": 1}
    with (
        _stub_01(llm_simulated=False, registry=registry, real_job="rejected_bare") as caliber,
        pytest.raises(LabVerificationError, match="the gate rejected the candidate and says why"),
    ):
        run_01(
            caliber,
            suffix="t",
            registry=lambda name: registry["prod"],
            poll_interval=0.01,
            calibration_timeout=5,
            accept_rejection=True,
        )
    assert "FAIL the rejection rests on real evidence" in capsys.readouterr().out


def test_cookbook_01_real_calibration_failure_is_reported_with_the_job_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from examples.cookbooks.cookbook_01_trustworthy_intake_classifier import (
        LabVerificationError,
    )

    registry = {"prod": 1}
    with (
        _stub_01(llm_simulated=False, registry=registry, real_job="failed") as caliber,
        pytest.raises(LabVerificationError, match="calibration did not fail"),
    ):
        run_01(
            caliber,
            suffix="t",
            registry=lambda name: registry["prod"],
            poll_interval=0.01,
            calibration_timeout=5,
        )
    assert "calibration failed: eval provider error: no valid score" in capsys.readouterr().out


def test_cookbook_01_contract_report_rules() -> None:
    from examples.cookbooks.cookbook_01_trustworthy_intake_classifier import contract_report

    good = jsonlib.dumps(
        {"intent": "bug", "priority": "low", "confidence": 1, "needs_review": False, "reason": "x"}
    )
    report = contract_report(
        [
            good,
            "Sure! " + good,  # prose around the JSON
            jsonlib.dumps({"intent": "refund", "priority": "low"}),  # bad enum and keys
            "",
        ],
        [{"intent": "bug"}, {"intent": "bug"}, {"intent": "bug"}, {"intent": "bug"}],
    )
    assert report["valid_json"] == 2
    assert report["allowed_intent_values"] == 1
    assert report["schema_keys"] == 1
    assert report["label_agreement"] == 1
    assert report["contract_ok"] == [True, False, False, False]


def test_cookbook_02_runs_the_skill_lab_end_to_end() -> None:
    """The lab's whole API path, replayed against a small stateful stand-in for the skills routes
    (``cookbook_02_fake_server``). The real selector, job and package format are covered by
    ``caliber/tests/test_cookbook_02_trigger_dataset.py`` and by the live run in the README."""
    pytest.importorskip("yaml", reason="the lab reads the skill's YAML frontmatter")
    from cookbook_02_fake_server import FakeSkillServer

    cases = load_cases(assets_dir())
    server = FakeSkillServer(cases)
    caliber = server.client(CaliberClient)
    viewer = FakeSkillServer(cases, viewer=True).client(CaliberClient)
    with caliber, viewer:
        report = run_02(caliber, viewer=viewer, skill_name="support-tone-and-citation-test")

    assert report["verified"] is True
    assert report["cases"] == 40
    assert report["round1"]["accuracy"] == 0.75
    assert report["round1"]["false_positives"] == [
        "T27",
        "T28",
        "T29",
        "T30",
        "T31",
        "T32",
        "T34",
        "T35",
    ]
    assert report["round1"]["false_negatives"] == ["T14", "T23"]
    assert report["round2"]["accuracy"] == 1.0
    assert report["render"]["unresolved"] == []
    assert report["render"]["partial_unresolved"] == ["policy_context", "user_message"]
    assert report["mode"] == "simulated"
    assert report["calibration"]["simulated"] is True and report["calibration"]["n_examples"] == 0
    assert "no_test_set" not in report["failures"]
    assert server.datasets == {}, "the simulated tier needs no test set"
    assert report["versions"]["history"] == [3, 2, 1]
    assert report["package"]["decisions_differing_after_metadata_restored"] == []
    assert report["package"]["decisions_differing_before_metadata_restored"]
    assert set(report["failures"]["viewer"]) == {"apply", "calibrate", "rollback", "create"}
    assert all(reason.startswith("403 ") for reason in report["failures"]["viewer"].values())
    assert report["failures"]["no_baseline"]["baseline_run_id"] is None
    assert report["archived"] == 3
    # the order the README documents
    calls = server.calls
    ordered = [
        "POST /skills",
        "POST /skills/test-runs",
        "POST /skills/SK-01/baseline",
        "POST /skills/SK-01/calibrate",
        "POST /jobs/RFN-01/apply",
        "POST /skills/SK-01/rollback",
        "GET /skills/SK-01/package.zip",
        "POST /skills/import-package.zip",
    ]
    positions = [calls.index(call) for call in ordered]
    assert positions == sorted(positions)


def _run_02_real(**fake_options: Any) -> tuple[Any, dict[str, Any]]:
    from cookbook_02_fake_server import FakeSkillServer

    server = FakeSkillServer(load_cases(assets_dir()), real=True, **fake_options)
    caliber = server.client(CaliberClient)
    viewer = FakeSkillServer(load_cases(assets_dir()), viewer=True).client(CaliberClient)
    with caliber, viewer:
        return server, run_02(caliber, viewer=viewer, skill_name="support-tone-and-citation-test")


def test_cookbook_02_on_a_real_stack_asserts_invariants_of_the_test_set_calibration() -> None:
    """With real providers the script imports the 8-row test set, calibrates against it and asserts
    invariants (not generated text or scores); a calibration with no test set must fail."""
    pytest.importorskip("yaml", reason="the lab reads the skill's YAML frontmatter")
    server, report = _run_02_real()

    assert report["verified"] is True and report["mode"] == "real"
    calibration = report["calibration"]
    assert calibration["simulated"] is False and calibration["provider"] == "mlflow"
    assert calibration["n_examples"] == 8 and calibration["attempts"] == 1
    assert 0 <= calibration["candidate_overall"] <= 1
    assert report["versions"]["history"] == [3, 2, 1]
    # the main skill, the no-baseline skill and the no-test-set run each calibrated once or more
    assert report["failures"]["no_test_set"]["status"] == "failed"
    assert report["failures"]["no_baseline"]["status"] == "candidate_ready"
    assert report["failures"]["no_baseline"]["baseline_run_id"] is None
    assert [len(rows) for rows in server.datasets.values()] == [8]


def test_cookbook_02_real_calibration_is_retried_when_the_gate_rejects_it() -> None:
    pytest.importorskip("yaml", reason="the lab reads the skill's YAML frontmatter")
    _, report = _run_02_real(rejections=2)

    assert report["verified"] is True
    assert report["calibration"]["attempts"] == 3


def test_cookbook_02_real_calibration_that_keeps_failing_is_reported() -> None:
    pytest.importorskip("yaml", reason="the lab reads the skill's YAML frontmatter")
    with pytest.raises(AssertionError, match=r"rejected.*3 attempt"):
        _run_02_real(rejections=9)


def test_cookbook_02_real_evidence_that_claims_to_be_simulated_is_refused() -> None:
    from types import SimpleNamespace

    from examples.cookbooks.cookbook_02_precision_skills import real_evidence

    job = SimpleNamespace(
        candidate={},
        eval_results={
            "simulated": True,
            "provider": "fake",
            "n_examples": 8,
            "eval_dataset_id": "ED-1",
            "candidate": {"overall": 0.9, "dimensions": {}},
            "baseline": {"overall": 0.9, "dimensions": {}},
            "gate": {"passed": True},
        },
    )
    with pytest.raises(AssertionError):
        real_evidence(job, "ED-1", 8)


def _lab09(**fake_options: Any) -> tuple[Any, Any]:
    from cookbook_09_fake_server import FakeWorkflowServer

    from examples.cookbooks.cookbook_09_self_healing_workflows import ASSETS, load_rows

    server = FakeWorkflowServer(load_rows(ASSETS), **fake_options)
    return server, server.client(CaliberClient)


def test_cookbook_09_finds_patches_proves_and_rolls_back() -> None:
    """The whole lab: 24 of 35 tickets fail on version 1, none on the patched version 2, the
    regression slice is untouched, and the staging alias rolls back."""
    server, caliber = _lab09()
    with caliber:
        result = run_09(caliber, suffix="t", cleanup=True)

    assert result["v1"] == {"completed": 11, "failed": 24}
    assert result["v1_failed_at"] == {"check_reference": 3, "decide": 4, "validate": 17}
    assert result["v2"] == {
        "completed": 35,
        "failed": 0,
        "decisions": {"approve": 10, "manual_review": 13, "rejected_input": 12},
    }
    assert result["regression_slice"] == 11
    assert result["retry"] == {"attempts": 2, "retry_run_id": "WR-38", "status": "failed"}
    assert result["propose_patch"] == {"patch_id": "WP-1", "patch_kind": "prompt", "refused": 400}
    assert result["alias"] == {"through_alias": ["completed", "failed"], "prod_refused": 400}
    assert result["debugger"]["decide"]["completed_steps"] == [
        "start",
        "validate",
        "check_reference",
        "lookup",
    ]
    assert all(entry["checkpoints"] == 0 for entry in result["debugger"].values())
    assert server.deleted == [result["workflow_id"]]
    # the call sequence the README documents
    assert server.calls[0] == "POST /workflows/import"
    workflow_id = result["workflow_id"]
    assert f"POST /workflow-versions/{result['version_1']}/propose-patch" in server.calls
    assert server.calls.index(f"POST /workflows/{workflow_id}/versions") < server.calls.index(
        f"POST /workflows/{workflow_id}/deployments/staging/promote"
    )


def test_cookbook_09_stops_when_the_patch_does_not_fix_the_failures() -> None:
    from examples.cookbooks.cookbook_09_self_healing_workflows import VerificationError

    _server, caliber = _lab09(broken_patch=True)
    with caliber, pytest.raises(VerificationError, match="v2 should complete"):
        run_09(caliber, suffix="broken")


def test_cookbook_09_stops_when_version_1_does_not_fail_as_documented() -> None:
    from types import SimpleNamespace

    from examples.cookbooks import cookbook_09_self_healing_workflows as lab

    rows = lab.load_rows(lab.ASSETS)
    runs = {}
    for row in rows:
        expected = row["expected"]
        if expected["v1_status"] == "completed":
            out = jsonlib.dumps(
                {"decision": expected["decision"], "reason_code": expected["reason_code"]}
            )
            runs[row["id"]] = SimpleNamespace(
                status="completed", extra={"summary": {"output": out}, "error_summary": None}
            )
        else:
            error = (
                f"python_code node '{expected['v1_failing_node']}' failed: "
                f"{expected['v1_error_contains']}"
            )
            runs[row["id"]] = SimpleNamespace(status="failed", extra={"error_summary": error})
    assert dict(lab.verify_version_1(rows, runs)) == lab.EXPECTED_V1_FAILURES

    runs["F12"].extra["error_summary"] = "python_code node 'validate' failed: something else"
    with pytest.raises(lab.VerificationError, match="F12: unexpected error"):
        lab.verify_version_1(rows, runs)
    runs["F12"].extra["error_summary"] = "python_code node 'decide' failed: KeyError"
    with pytest.raises(lab.VerificationError, match="F12: failed at decide"):
        lab.verify_version_1(rows, runs)
    runs["F12"].status = "completed"
    with pytest.raises(lab.VerificationError, match="F12: v1 should fail"):
        lab.verify_version_1(rows, runs)


def test_cookbook_09_helpers() -> None:
    from examples.cookbooks import cookbook_09_self_healing_workflows as lab

    error = "ToolExecutionError: python_code node 'decide' failed: KeyError"
    assert lab.failing_node(error) == "decide"
    assert lab.failing_node("no node named here") is None
    assert lab.failing_node(None) is None
    with pytest.raises(lab.VerificationError, match="boom"):
        lab.check(False, "boom")
    lab.check(True, "never raised")


def _lab10(
    *, simulated: bool = False, server_cls: type | None = None
) -> tuple[Any, Any, dict[str, Any]]:
    from cookbook_10_fake_server import FakeLabServer

    from examples.cookbooks.cookbook_10_trustworthy_evaluation import ASSETS, load_rows

    rows = load_rows(ASSETS)
    server = (server_cls or FakeLabServer)(rows, simulated=simulated)
    trace_ids = {row["id"]: f"tr-{row['id']}" for row in rows}
    server.trace_row = {trace_ids[row["id"]]: row for row in rows}
    return server, server.client(CaliberClient), trace_ids


def test_cookbook_10_certifies_one_judge_and_rejects_another() -> None:
    """The lab's whole API path with a model behind the server: the numbers are the ones the README
    documents (checked against a live stack with the stand-in model), arithmetic recomputed."""
    server, caliber, trace_ids = _lab10()
    with caliber:
        result = run_10(caliber, trace_ids=trace_ids, suffix="t", expect_stand_in=True)

    assert result["mode"] == "model" and result["result"] == "verified"
    assert result["cookbook"] == {"id": "10", "title": "Trustworthy Evaluation", "steps": 6}
    assert result["dataset"]["examples"] == 50 and result["dataset"]["version"] == 51
    assert result["invalid_template"] == {"status": 400, "variable": "evidence"}
    assert result["empty_dataset_evaluation"] == {"status": 400}
    runs = result["evaluations"]
    assert runs["baseline"]["judge"] == 1.0 and runs["baseline"]["contains_expected"] == 0.0
    assert runs["candidate"]["judge"] == 0.56 and runs["candidate"]["overall"] == 0.74
    assert runs["delta_overall"] == 0.0733
    assert result["review"]["labels_imported"] == 50

    strict = result["alignment"]["faithfulness"]
    assert (strict["agreement"], strict["kappa"], strict["decision"]) == (
        0.82,
        0.6365,
        "CERTIFIED",
    )
    assert strict["confusion"] == {"true_pos": 23, "false_pos": 5, "true_neg": 18, "false_neg": 4}
    assert strict["disagreements"] == [
        "J03",
        "J12",
        "J14",
        "J17",
        "J19",
        "J22",
        "J24",
        "J30",
        "J33",
    ]
    lenient = result["alignment"]["lenient"]
    assert (lenient["agreement"], lenient["kappa"], lenient["decision"]) == (
        0.58,
        0.0933,
        "REJECTED",
    )
    # Five rows already clear both bars (0.8 / 0.62), which is why the lab demands thirty.
    few = result["alignment"]["too_few_rows"]
    assert (few["n"], few["agreement"], few["decision"]) == (5, 0.8, "NOT_CERTIFIABLE")
    leak = result["alignment"]["rationale_leak"]
    assert leak["agreement"] == 0.66 and leak["agreement"] < strict["agreement"]
    assert result["feedback"] == {"hard_cases_added": 9, "dataset_version": 60}
    assert "POST /judges/JDG-1/alignment" in server.calls
    assert "archived" not in result and server.archived == []


def test_cookbook_10_cleanup_archives_what_it_created() -> None:
    server, caliber, trace_ids = _lab10()
    with caliber:
        result = run_10(
            caliber, trace_ids=trace_ids, suffix="c", expect_stand_in=True, cleanup=True
        )
    assert result["archived"] == {"datasets": 2, "judges": 2, "queues": 1, "prompts": 1}
    assert sorted(server.archived) == sorted(
        ["ED-1", "ED-2", "JDG-1", "JDG-2", "RVQ-1", "grounded-reply-c"]
    )


def test_cookbook_10_on_a_simulated_stack_verifies_the_refusals_instead() -> None:
    """With no model behind the server, Evaluations and the judge cannot run; the script reports
    exactly that, and still verifies everything that needs no model."""
    _server, caliber, trace_ids = _lab10(simulated=True)
    with caliber:
        result = run_10(caliber, trace_ids=trace_ids, suffix="s")

    assert result["mode"] == "simulated" and result["result"] == "verified"
    assert result["evaluation_refused"]["status"] == 400
    assert "real LLM provider" in result["evaluation_refused"]["detail"]
    assert "evaluations" not in result and "feedback" not in result
    assert result["review"]["labels_imported"] == 50
    assert result["alignment"]["scored"] == 0
    assert result["alignment"]["decision"] == "NOT_CERTIFIABLE"
    assert "OPENAI_API_KEY" in result["alignment"]["first_row_error"]
    calibration = result["calibration"]
    assert calibration["status"] == "candidate_ready" and calibration["simulated"] is True
    assert calibration["n_examples"] == 50 and calibration["dataset_version"] == 51
    assert calibration["gate_passed_on_a_canned_score"] is True


def test_cookbook_10_fails_loudly_when_a_documented_outcome_does_not_hold() -> None:
    """A judge that disagrees with the pinned numbers must stop the script (exit non-zero), not be
    reported as verified."""
    from cookbook_10_fake_server import FakeLabServer

    from examples.cookbooks.cookbook_10_trustworthy_evaluation import VerificationError

    class AlwaysPasses(FakeLabServer):
        def _verdict(self, judge_id: str, outputs: str, expectations: dict[str, Any]) -> bool:
            return True

    _, caliber, trace_ids = _lab10(server_cls=AlwaysPasses)
    with caliber, pytest.raises(VerificationError):
        run_10(caliber, trace_ids=trace_ids, suffix="x", expect_stand_in=True)

    # With a real model nothing is pinned, but a "strict" judge that agrees with people no more than
    # the deliberately lenient one is not a judge the lab can use: that is an invariant, not a pin.
    _, caliber, trace_ids = _lab10(server_cls=AlwaysPasses)
    with caliber, pytest.raises(VerificationError, match="not above the lenient"):
        run_10(caliber, trace_ids=trace_ids, suffix="y")


def test_cookbook_10_real_model_tier_asserts_invariants_not_numbers() -> None:
    """Without ``--expect-stand-in`` the script treats the model as a real one: the candidate is
    given the evidence (a prompt slot and a copy of the test set whose rows carry it), and only
    ranges and orderings are asserted. The fake applies the stand-in's rules, which satisfy them."""
    server, caliber, trace_ids = _lab10()
    with caliber:
        result = run_10(caliber, trace_ids=trace_ids, suffix="r", cleanup=True)

    assert result["mode"] == "real model" and result["result"] == "verified"
    # the plain set, the grounded copy, and the empty set the script refuses to evaluate
    assert result["archived"] == {"datasets": 3, "judges": 2, "queues": 1, "prompts": 1}
    assert server.prompts["grounded-reply-r"].endswith("{{ evidence }}\n")
    by_name = {d["name"]: server.examples[d["dataset_id"]] for d in server.datasets.values()}
    plain_rows = by_name["judge-certification-set-r"]
    with_evidence = by_name["judge-certification-grounded-r"]
    # 50 rows each; the plain set then took the nine disagreements back as hard cases
    assert len(plain_rows) == 50 + result["feedback"]["hard_cases_added"]
    assert len(with_evidence) == 50
    assert all("evidence" not in e["input"] for e in plain_rows)
    assert all(e["input"]["evidence"] for e in with_evidence)
    first = with_evidence[0]["input"]
    assert "cancel_date: June 2" in first["evidence"] and "decision" not in first["evidence"]
    runs = result["evaluations"]
    assert runs["candidate"]["contains_expected"] > runs["baseline"]["contains_expected"]
    assert (
        result["alignment"]["faithfulness"]["agreement"]
        > result["alignment"]["lenient"]["agreement"]
    )


def test_cookbook_10_real_model_tier_fails_when_the_candidate_gains_nothing() -> None:
    """If giving the candidate the evidence does not make it state the expected fact more often than
    the baseline, the real-model tier must fail rather than report a comparison with no signal."""
    from cookbook_10_fake_server import FakeLabServer

    from examples.cookbooks.cookbook_10_trustworthy_evaluation import VerificationError

    class IgnoresEvidence(FakeLabServer):
        def _reply(self, source: dict[str, Any], example: dict[str, Any], candidate: bool) -> str:
            return super()._reply(source, example, False)

    _, caliber, trace_ids = _lab10(server_cls=IgnoresEvidence)
    with caliber, pytest.raises(VerificationError, match="states the expected fact"):
        run_10(caliber, trace_ids=trace_ids, suffix="i")


@pytest.mark.parametrize(
    ("scored", "n", "agreement", "kappa", "decision"),
    [
        (50, 50, 0.82, 0.6365, "CERTIFIED"),
        (50, 50, 0.80, 0.60, "CERTIFIED"),
        (50, 50, 0.79, 0.90, "REJECTED"),
        (50, 50, 0.95, 0.59, "REJECTED"),
        (5, 5, 1.0, 1.0, "NOT_CERTIFIABLE"),
        (29, 29, 1.0, 1.0, "NOT_CERTIFIABLE"),
        (30, 30, 0.9, 0.8, "CERTIFIED"),
        (40, 50, 0.9, 0.8, "NOT_CERTIFIABLE"),
    ],
)
def test_cookbook_10_certification_rule(
    scored: int, n: int, agreement: float, kappa: float, decision: str
) -> None:
    from examples.cookbooks.cookbook_10_trustworthy_evaluation import decide

    assert decide(scored, n, agreement, kappa) == decision


def test_cookbook_10_metrics_match_a_worked_example() -> None:
    """Judge pass/fail against human pass/fail over ten rows, worked by hand:
    TP 4, FP 1, TN 4, FN 1 -> p_o 0.8, p_e 0.5, kappa 0.6."""
    from examples.cookbooks.cookbook_10_trustworthy_evaluation import (
        agreement,
        cohen_kappa,
        confusion,
    )

    judge = [True] * 4 + [True] + [False] * 4 + [False]
    human = [True] * 4 + [False] + [False] * 4 + [True]
    assert agreement(judge, human) == 0.8
    assert round(cohen_kappa(judge, human), 4) == 0.6
    assert confusion(judge, human) == {"true_pos": 4, "false_pos": 1, "true_neg": 4, "false_neg": 1}
    # A judge that always says "pass" agrees with a mostly-passing sample and measures nothing.
    assert cohen_kappa([True] * 10, [True] * 9 + [False]) == 0.0
    assert cohen_kappa([True] * 10, [True] * 10) == 1.0


def _lab16(**fake_options: Any) -> tuple[Any, Any]:
    from cookbook_16_fake_server import FakeObservabilityServer

    from examples.cookbooks.cookbook_16_observability_triage import ASSETS, _seed_module

    rows = _seed_module(ASSETS).load_rows(ASSETS / "dataset/prod-regression.sample.jsonl")
    server = FakeObservabilityServer(rows, **fake_options)
    return server, server.client(CaliberClient)


def test_cookbook_16_seeds_reads_captures_and_triages() -> None:
    """40 real runs become 12 error and 28 ok traces; 16 are captured and queued, 2 reviewed."""
    server, caliber = _lab16()
    with caliber:
        result = run_16(caliber, suffix="t", cleanup=True)

    assert result["seeded"] == 40
    assert result["traces"] == {"ERROR": 12, "OK": 28}
    assert result["node_tree"] == {
        "poison": "node.check_reference",
        "upstream": "node.decide",
        "validate": "node.validate",
    }
    assert result["ok_but_wrong"] == ["P25", "P26", "P27", "P28"]
    assert result["failure_paths"]["unknown_trace"] == 404
    assert result["failure_paths"]["future_window"] == 0
    assert result["test_set"]["examples"] == 16
    assert result["triage"]["queued"] == 16 and result["triage"]["answered"] == 2
    assert set(result["triage"]["assessments"]["P38"]) >= {"failure_mode", "severity"}
    # every captured example carries its gold outcome and its source trace
    assert all(
        "from-trace" in ex["tags"] and "decision" in ex["expected"]
        for ex in next(iter(server.examples.values()))
    )
    assert sorted(server.archived) == sorted([*server.datasets, *server.queues])
    assert server.calls.index("POST /workflows/import") < server.calls.index("POST /review-queues")


def test_cookbook_16_stops_when_failed_runs_do_not_leave_error_traces() -> None:
    from examples.cookbooks.cookbook_16_observability_triage import VerificationError

    _server, caliber = _lab16(ignore_failures=True)
    with caliber, pytest.raises(VerificationError, match="trace status counts"):
        run_16(caliber, suffix="broken")


def test_cookbook_16_helpers() -> None:
    from examples.cookbooks import cookbook_16_observability_triage as lab

    assert lab.kind_of({"tags": ["error", "poison", "timeout"]}) == "poison"
    assert lab.kind_of({"tags": ["ok", "correct"]}) is None
    with pytest.raises(lab.VerificationError, match="boom"):
        lab.check(False, "boom")
    with pytest.raises(lab.VerificationError, match="expected 40"):
        lab.run(None, assets=_short_assets())  # type: ignore[arg-type]


def _short_assets() -> Any:
    """A copy of the assets whose dataset has one row, to prove the size check fires first."""
    import shutil
    import tempfile
    from pathlib import Path

    from examples.cookbooks.cookbook_16_observability_triage import ASSETS

    root = Path(tempfile.mkdtemp(prefix="lab16-assets-"))
    shutil.copytree(ASSETS, root / "assets")
    dataset = root / "assets/dataset/prod-regression.sample.jsonl"
    dataset.write_text(dataset.read_text().splitlines()[0] + "\n")
    return root / "assets"


def test_cookbook_18_verifies_roles_tokens_and_audit_against_the_stand_in_server() -> None:
    """The lab's whole API path: team, role matrix, bound token, refusals, audit.

    The stateful stand-in (``cookbook_18_fake_server``) enforces the project-role rules the docs
    state; the script reads the roster, role matrix and failure scenarios from the lab's own
    dataset files and must verify every cell. ``test_cookbook_18_roles_tokens_audit.py`` covers the
    data files, the refusals, and that a server which breaks a rule fails the run."""
    from cookbook_18_fake_server import FakeCaliber

    from examples.cookbooks.cookbook_18_projects_roles_tokens_audit import load_roster

    fake = FakeCaliber(load_roster())

    def user_client(token: str, project: str | None = None) -> CaliberClient:
        return CaliberClient(
            BASE,
            token=token,
            project=project,
            http_client=httpx.Client(transport=fake.transport()),
            max_retries=0,
        )

    caliber = CaliberClient(
        BASE,
        token=fake.ADMIN_TOKEN,
        http_client=httpx.Client(transport=fake.transport()),
        max_retries=0,
    )
    with caliber:
        result = run_18(
            caliber,
            base_url=BASE,
            login_attempt=fake.login_attempt,
            user_client=user_client,
            use_caliberctl=False,
            sleep=fake.age,
            run_key="unit-test",
        )
    assert result["members"] == 8
    assert result["matrix_requests"] == 72
    assert result["simulated_providers"] == ["eval", "llm"]
    scenarios = result["failure_scenarios"]
    assert isinstance(scenarios, dict)
    assert set(scenarios) >= {"F01", "F10", "F11", "F12", "F15"}


def _financial_analysis_fixture() -> dict[str, Any]:
    return {
        "currency": "USD",
        "periods": 12,
        "percentile_method": "linear_interpolation_rank_(n-1)*p",
        "statistics": expected_statistics(),
        "observations": [
            "Revenue and operating income increased over the year.",
            "Cash increased while accounts payable remained controlled.",
        ],
    }


def test_cookbook_17_csv_has_realistic_balanced_monthly_financials() -> None:
    rows = list(csv.DictReader(io.StringIO(build_financial_csv().decode())))

    assert tuple(rows[0]) == CSV_FIELDS
    assert len(rows) == 12 == len(MONTHLY_FINANCIALS)
    assert [row["month"] for row in rows] == [f"2025-{month:02d}" for month in range(1, 13)]
    for row in rows:
        components = sum(
            int(row[column])
            for column in (
                "operating_expenses",
                "payroll",
                "cloud_compute_costs",
                "other_expenses",
            )
        )
        assert int(row["total_expenses"]) == components
        assert int(row["operating_income"]) == int(row["revenue"]) - components


def test_cookbook_17_rejects_incomplete_model_statistics() -> None:
    incomplete = _financial_analysis_fixture()
    del incomplete["statistics"]["revenue"]["p90"]

    with pytest.raises(ValueError, match="revenue"):
        parse_analysis(jsonlib.dumps(incomplete))


def test_cookbook_17_rejects_commentary_outside_json() -> None:
    payload = jsonlib.dumps(_financial_analysis_fixture())

    with pytest.raises(jsonlib.JSONDecodeError):
        parse_analysis(f"Analysis follows:\n{payload}")


@pytest.mark.parametrize("field", ["percentile_method", "observations"])
def test_cookbook_17_rejects_missing_output_contract_fields(field: str) -> None:
    incomplete = _financial_analysis_fixture()
    del incomplete[field]

    with pytest.raises(ValueError, match="top-level shape"):
        parse_analysis(jsonlib.dumps(incomplete))


def test_cookbook_17_requires_p50_to_equal_median_exactly() -> None:
    inconsistent = _financial_analysis_fixture()
    inconsistent["statistics"]["revenue"]["p50"] += 0.005

    with pytest.raises(ValueError, match="p50 does not equal median"):
        parse_analysis(jsonlib.dumps(inconsistent))


def test_cookbook_17_surfaces_provider_errors_before_persisting_output() -> None:
    with pytest.raises(RuntimeError, match="AuthenticationError"):
        parse_turn_analysis(
            {
                "assistant_message": {
                    "content": "I encountered an error: AuthenticationError",
                    "metadata_": {"error": True},
                }
            }
        )


def test_cookbook_17_creates_runs_and_persists_everything_through_typed_sdk() -> None:
    csv_bytes = build_financial_csv()
    analysis = _financial_analysis_fixture()
    scoped_headers: list[str | None] = []

    expected_result = (
        jsonlib.dumps(
            {
                "scenario": "monthly_company_financial_analysis",
                "project_id": "PRJ-17",
                "blob_bucket": "sdk-financial-unit-test",
                "input_object_key": "projects/PRJ-17/inputs/monthly-financials.csv",
                "managed_file_id": "FILE-17-IN",
                "prompt_name": "sdk-financial-analysis-unit-test",
                "prompt_version": 1,
                "prompt_alias": "prod",
                "assistant_session_id": "SESSION-17",
                "analysis": analysis,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()

    def project(request: httpx.Request) -> dict[str, Any]:
        assert "x-caliber-project" not in request.headers
        body = jsonlib.loads(request.content)
        assert body["name"] == "SDK Financial Analysis unit-test"
        return {"project_id": "PRJ-17", "name": body["name"]}

    def create_bucket(request: httpx.Request) -> dict[str, Any]:
        scoped_headers.append(request.headers.get("x-caliber-project"))
        assert jsonlib.loads(request.content) == {"name": "sdk-financial-unit-test"}
        return {"name": "sdk-financial-unit-test"}

    def upload(request: httpx.Request) -> dict[str, Any]:
        scoped_headers.append(request.headers.get("x-caliber-project"))
        if b'filename="monthly-financials.csv"' in request.content:
            assert b"monthly-financials.csv" in request.content
            assert CSV_FIELDS[0].encode() in request.content
            assert b"projects/PRJ-17/inputs/monthly-financials.csv" in request.content
            return {"key": "projects/PRJ-17/inputs/monthly-financials.csv"}
        assert b"monthly-financial-analysis.json" in request.content
        assert b"projects/PRJ-17/outputs/monthly-financial-analysis.json" in request.content
        assert expected_result in request.content
        return {"key": "projects/PRJ-17/outputs/monthly-financial-analysis.json"}

    def download(request: httpx.Request) -> httpx.Response:
        scoped_headers.append(request.headers.get("x-caliber-project"))
        key = request.url.params["key"]
        if key.endswith("monthly-financials.csv"):
            return httpx.Response(200, content=csv_bytes, headers={"content-type": "text/csv"})
        assert key.endswith("monthly-financial-analysis.json")
        return httpx.Response(
            200, content=expected_result, headers={"content-type": "application/json"}
        )

    def import_object(request: httpx.Request) -> dict[str, Any]:
        scoped_headers.append(request.headers.get("x-caliber-project"))
        assert jsonlib.loads(request.content) == {
            "key": "projects/PRJ-17/inputs/monthly-financials.csv",
            "path": "inputs/monthly-financials.csv",
        }
        return {"file_id": "FILE-17-IN", "project_id": "PRJ-17"}

    def create_prompt(request: httpx.Request) -> dict[str, Any]:
        scoped_headers.append(request.headers.get("x-caliber-project"))
        body = jsonlib.loads(request.content)
        assert body == {
            "name": "sdk-financial-analysis-unit-test",
            "template": PROMPT_TEMPLATE,
            "commit_message": "Create SDK financial-analysis cookbook prompt",
        }
        return {"name": body["name"], "version": 1}

    def create_session(request: httpx.Request) -> dict[str, Any]:
        scoped_headers.append(request.headers.get("x-caliber-project"))
        body = jsonlib.loads(request.content)
        assert PROMPT_TEMPLATE in body["goal"]
        assert body["metadata_"]["project_id"] == "PRJ-17"
        assert body["metadata_"]["blob_bucket"] == "sdk-financial-unit-test"
        assert body["metadata_"]["blob_key"].endswith("monthly-financials.csv")
        assert body["metadata_"]["managed_file_id"] == "FILE-17-IN"
        assert body["metadata_"]["prompt_ref"] == ("prompts:/sdk-financial-analysis-unit-test@prod")
        assert body["skill_mode"] == "off"
        return {"session_id": "SESSION-17"}

    def send_message(request: httpx.Request) -> dict[str, Any]:
        scoped_headers.append(request.headers.get("x-caliber-project"))
        body = jsonlib.loads(request.content)
        assert csv_bytes.decode() in body["content"]
        assert body["artifact_type"] == "prompt"
        assert body["skill_mode"] == "off"
        return {"assistant_message": {"content": jsonlib.dumps(analysis)}}

    caliber = stub_server(
        {
            "GET /me": {"user_id": "@alice"},
            "POST /projects": project,
            "GET /object-store/status": {"connected": True},
            "POST /object-store/buckets": create_bucket,
            "POST /object-store/buckets/sdk-financial-unit-test/objects": upload,
            "GET /object-store/buckets/sdk-financial-unit-test/object": download,
            "POST /object-store/buckets/sdk-financial-unit-test/object/import": import_object,
            "POST /prompts": create_prompt,
            "GET /prompts/sdk-financial-analysis-unit-test/versions/1": {
                "name": "sdk-financial-analysis-unit-test",
                "version": 1,
                "template": PROMPT_TEMPLATE,
            },
            "POST /prompts/sdk-financial-analysis-unit-test/aliases/prod": {
                "name": "sdk-financial-analysis-unit-test",
                "alias": PROMPT_ALIAS,
                "version": 1,
                "release_status": "applied",
            },
            "GET /prompts": [
                {
                    "agent_id": "sdk-financial-analysis-unit-test",
                    "prompt_name": "sdk-financial-analysis-unit-test",
                    "version": 1,
                    "alias": PROMPT_ALIAS,
                    "has_prompt": True,
                }
            ],
            "POST /assistant/sessions": create_session,
            "POST /assistant/sessions/SESSION-17/messages": send_message,
        }
    )
    with caliber:
        result = run_17(caliber, run_key="unit-test")
        caliber.whoami()

    assert result == {
        "project_id": "PRJ-17",
        "blob_bucket": "sdk-financial-unit-test",
        "input_object_key": "projects/PRJ-17/inputs/monthly-financials.csv",
        "managed_file_id": "FILE-17-IN",
        "prompt_name": "sdk-financial-analysis-unit-test",
        "prompt_version": 1,
        "prompt_alias": "prod",
        "assistant_session_id": "SESSION-17",
        "output_object_key": "projects/PRJ-17/outputs/monthly-financial-analysis.json",
        "analysis": analysis,
    }
    assert scoped_headers and set(scoped_headers) == {"PRJ-17"}
