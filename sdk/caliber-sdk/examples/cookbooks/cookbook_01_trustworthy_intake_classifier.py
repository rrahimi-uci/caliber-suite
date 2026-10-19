"""Cookbook 01: release a prompt change you can trust, end to end, with the SDK.

This is the executable form of ``docs-site/cookbooks/01-prompt-regression-lab/README.md``. It runs
the alpha's one promise against a real CALIBER server and exits 0 only when every documented
outcome was verified:

1. author a prompt (v1, strict JSON) and make it live;
2. import a labelled test set from ``assets/dataset/intake-classifier.jsonl``;
3. register a weaker v2 (the regression) without making it live;
4. evaluate v1 and v2 on the test set and compare them. This needs a model: with the simulated LLM
   the evaluation is refused (400) and the script verifies that refusal instead, because CALIBER
   never invents a scorecard;
5. calibrate the live prompt and wait for the candidate; read its evidence and diff;
6. exercise the failure paths: a viewer cannot apply (403), a gate that fails blocks the candidate
   (409 on apply), evaluating a missing test set is a 404;
7. apply the candidate, then verify the live alias moved in the MLflow registry, the live board,
   the release timeline and the audit log, each naming who applied it;
8. roll back and verify the alias returned.

The script reads ``GET /readiness`` and runs in one of two modes. When ``simulated`` lists
``llm``/``eval`` (the alpha default) the refusal and the ``simulated`` evidence markers are
verified. When it is empty (a real model: a hosted provider or a local Ollama) the evaluations
really run, and only *invariants* are asserted (answers parse as the contract, scores are in
range, the evidence has the real example count and is not marked simulated), never an exact score
or answer, because a model's output varies. A failed evaluation or calibration is reported as
``evaluation failed: <error>`` / ``calibration failed: <error>`` with the server's own message.

Environment (see the README): ``CALIBER_BASE_URL`` (the server root, for example
``http://127.0.0.1:5001``, without the API path), ``CALIBER_TOKEN`` (an operator or admin
token), ``MLFLOW_TRACKING_URI`` (to read the registry directly). Optional: ``COOKBOOK_01_ASSETS``
(a different assets directory); ``COOKBOOK_01_HTTP_TIMEOUT`` (seconds a synchronous evaluation may
take, default 900); ``COOKBOOK_01_CALIBRATION_TIMEOUT`` (seconds to wait for the candidate, default
1500 on a real model); ``COOKBOOK_01_MIN_SCORE`` and ``COOKBOOK_01_MAX_REGRESSION`` (the calibration
gate on a real model; unset means the product defaults, 0.85 and 0.02).

    python sdk/caliber-sdk/examples/cookbooks/cookbook_01_trustworthy_intake_classifier.py
    python ... --skip-release       # evaluation and regression only (a stand-in model, see README)
    python ... --expect-regression  # also assert the weaker v2 breaks the contract (stand-in model)
    python ... --cleanup            # delete what earlier runs created
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from caliber_sdk import CaliberClient
from caliber_sdk.errors import CaliberAPIError
from caliber_sdk.transport import API_PREFIX

NAME_PREFIX = "cookbook-01-"
ALLOWED_INTENTS = frozenset({"billing", "how_to", "bug", "account", "feature_request", "unknown"})
ALLOWED_PRIORITIES = frozenset({"low", "medium", "high", "urgent"})
SCHEMA_KEYS = frozenset({"intent", "priority", "confidence", "needs_review", "reason"})
EVAL_SCORERS = ["token_f1"]
#: A candidate may not lose more than this much pass rate against the baseline.
MAX_PASS_RATE_DROP = 0.05
#: On a real model a share of answers may stray from the contract; below this the prompt is not
#: working at all and the lab stops. (A stand-in or a strong model scores 1.0.)
MIN_CONTRACT_RATE = 0.9
#: The v2 prompt may not *beat* the baseline on the scorecard by more than this (scores wobble).
SCORE_NOISE = 0.02
VIEWER_USER = "cookbook01-viewer"
VIEWER_PASSWORD = "cookbook01-viewer-pass"


class LabVerificationError(AssertionError):
    """At least one documented outcome of the lab did not hold."""


def default_assets_dir() -> Path:
    override = os.environ.get("COOKBOOK_01_ASSETS")
    if override:
        return Path(override)
    return (
        Path(__file__).resolve().parents[4] / "docs-site/cookbooks/01-prompt-regression-lab/assets"
    )


def prompt_body(path: Path) -> str:
    """The template: everything below the YAML front matter of a prompt asset file."""
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        text = text.split("\n---\n", 1)[1]
    return text.strip() + "\n"


def load_dataset(path: Path) -> list[dict[str, Any]]:
    rows = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    for row in rows:
        if "input" not in row or "expected" not in row:
            raise LabVerificationError(f"{path.name}: row {row.get('id')} lacks input/expected")
    return rows


def calibration_scorers(assets: Path) -> list[dict[str, Any]]:
    """The calibration scorer: MLflow's ``Guidelines`` judge, configured from the asset file.

    ``Correctness`` (the form's default) needs an ``expected_response``/``expected_facts`` per
    example, which this test set does not have (its labels are ``intent``/``priority``/
    ``needs_review``), so on a real judge it scores nothing and the run fails. ``Guidelines``
    judges the answer against written rules and needs only the input and the output.
    """
    config = json.loads((assets / "calibration/guidelines.json").read_text(encoding="utf-8"))
    return [{"name": "Guidelines", "config": config}]


def gate_from_env() -> dict[str, float] | None:
    """Gate overrides for a real calibration run (``None``: the product defaults, 0.85 / 0.02).

    A small local model is a noisy judge (the same baseline scored 0.52 and 0.39 on two runs), so
    the README lowers both bars for it and says so; a hosted model uses the defaults.
    """
    gate: dict[str, float] = {}
    if value := os.environ.get("COOKBOOK_01_MIN_SCORE"):
        gate["min_aggregate_score"] = float(value)
    if value := os.environ.get("COOKBOOK_01_MAX_REGRESSION"):
        gate["max_regression_delta"] = float(value)
    return gate or None


def evaluation_failure(item: Any) -> str | None:
    """``evaluation failed: <server error>`` when a scored run did not complete, else ``None``.

    The server answers 201 with ``status: failed`` and an ``error_message`` (a refused connection,
    a model that does not exist, a rejected key) and an empty aggregate; reading ``aggregate``
    first dies with a ``KeyError`` that hides the cause.
    """
    if item.status == "completed":
        return None
    return f"evaluation failed: {item.error_message or item.error or item.status}"


def contract_report(predictions: list[str], expected: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministic rule checks over the model's raw answers.

    * ``valid_json``: the whole answer parses as one JSON object (prose or a code fence fails);
    * ``allowed_intent_values`` / ``allowed_priority_values``: the enums the prompt promises;
    * ``schema_keys``: exactly intent, priority, confidence, needs_review, reason;
    * ``label_agreement``: intent, priority and needs_review all equal the labelled expectation.
    """
    counts = {
        "valid_json": 0,
        "allowed_intent_values": 0,
        "allowed_priority_values": 0,
        "schema_keys": 0,
        "label_agreement": 0,
    }
    contract_ok: list[bool] = []
    for raw, gold in zip(predictions, expected, strict=True):
        try:
            record = json.loads(raw)
        except (TypeError, ValueError):
            record = None
        if not isinstance(record, dict):
            contract_ok.append(False)
            continue
        counts["valid_json"] += 1
        intent_ok = record.get("intent") in ALLOWED_INTENTS
        priority_ok = record.get("priority") in ALLOWED_PRIORITIES
        keys_ok = set(record) == SCHEMA_KEYS
        counts["allowed_intent_values"] += intent_ok
        counts["allowed_priority_values"] += priority_ok
        counts["schema_keys"] += keys_ok
        counts["label_agreement"] += all(record.get(key) == value for key, value in gold.items())
        contract_ok.append(intent_ok and priority_ok and keys_ok)
    return {"n": len(predictions), **counts, "contract_ok": contract_ok}


def registry_alias_version(name: str, alias: str = "prod") -> int | None:
    """The version the MLflow registry says ``alias`` points at (read straight from MLflow)."""
    base = os.environ["MLFLOW_TRACKING_URI"].rstrip("/")
    response = httpx.get(
        f"{base}/ajax-api/2.0/mlflow/registered-models/alias",
        params={"name": name, "alias": alias},
        timeout=30,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return int(response.json()["model_version"]["version"])


def make_viewer_client(admin: CaliberClient, base_url: str) -> CaliberClient:
    """A client signed in as a read-only account (created if it does not exist yet)."""
    try:
        admin.auth.accounts.create(VIEWER_USER, VIEWER_PASSWORD)
    except CaliberAPIError as exc:
        if "exist" not in str(exc).lower():
            raise
    # A previous run's cleanup may have disabled the account; signing in needs it enabled.
    admin.auth.accounts.update(VIEWER_USER, disabled=False)
    with httpx.Client(base_url=base_url.rstrip("/") + API_PREFIX, timeout=30) as http:
        login = http.post("/auth/login", json={"user_id": VIEWER_USER, "password": VIEWER_PASSWORD})
        login.raise_for_status()
        issued = http.post("/auth/tokens", json={"name": "cookbook-01-viewer"})
        issued.raise_for_status()
        token = issued.json()["data"]["token"]
    return CaliberClient(base_url, token=token)


def status_of(call: Callable[[], Any]) -> tuple[int | None, str]:
    """Run ``call`` expecting an API error: ``(status_code, detail)``; ``(None, '')`` on success."""
    try:
        call()
    except CaliberAPIError as exc:
        return exc.status_code, str(exc)
    return None, ""


def run(
    caliber: CaliberClient,
    *,
    assets: Path | None = None,
    suffix: str | None = None,
    viewer: CaliberClient | None = None,
    registry: Callable[[str], int | None] = registry_alias_version,
    skip_release: bool = False,
    expect_regression: bool = False,
    poll_interval: float = 2.0,
    calibration_timeout: float | None = None,
) -> dict[str, Any]:
    assets = assets or default_assets_dir()
    suffix = suffix or time.strftime("%Y%m%d%H%M%S")
    prompt_name = f"{NAME_PREFIX}intake-classifier-{suffix}"
    results: dict[str, Any] = {"prompt_name": prompt_name}
    checks: dict[str, bool] = {}

    def check(name: str, ok: bool, detail: Any = "") -> None:
        checks[name] = bool(ok)
        print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail != "" else ""))

    me = caliber.me.get().user_id
    results["actor"] = me
    readiness = caliber.raw.get("/readiness")
    simulated_parts = set(readiness.get("simulated") or [])
    llm_simulated = "llm" in simulated_parts
    eval_simulated = "eval" in simulated_parts
    results["llm_simulated"] = llm_simulated
    results["eval_simulated"] = eval_simulated
    if calibration_timeout is None:
        calibration_timeout = float(
            os.environ.get("COOKBOOK_01_CALIBRATION_TIMEOUT", 300 if eval_simulated else 1500)
        )

    # The Cookbooks gallery (feature flag `cookbooks`) lists this recipe with its guided steps.
    recipe = next((item for item in caliber.cookbooks.list() if item.id == "01"), None)
    check(
        "cookbook 01 is listed with guided steps",
        recipe is not None and len(recipe.steps) > 0,
        f"{len(recipe.steps) if recipe else 0} steps",
    )

    # 1. Author v1 (strict JSON) and make it live.
    strong = prompt_body(assets / "prompts/intake-classifier.md")
    weak = prompt_body(assets / "prompts/intake-classifier-weak.md")
    caliber.prompts.create(prompt_name, strong, commit_message="v1 strict-JSON intake classifier")
    caliber.prompts.promote(prompt_name, 1)
    check("v1 is live in the MLflow registry", registry(prompt_name) == 1)

    # 2. Import the labelled test set from the file: one example per JSONL line.
    rows = load_dataset(assets / "dataset/intake-classifier.jsonl")
    dataset = caliber.eval_datasets.create(
        f"{NAME_PREFIX}intake-golden-{suffix}",
        owner=me,
        description="44 labelled support messages (Cookbook 01)",
    )
    for row in rows:
        caliber.eval_datasets.add_example(
            dataset.dataset_id,
            input=row["input"],
            expected=row["expected"],
            tags=row.get("tags", []),
        )
    stored = caliber.eval_datasets.examples(dataset.dataset_id)
    check(
        "every row of the file was imported", len(stored) == len(rows), f"{len(stored)}/{len(rows)}"
    )
    results.update(dataset_id=dataset.dataset_id, examples=len(stored))
    dataset_version = caliber.eval_datasets.get(dataset.dataset_id).version
    results["dataset_version"] = dataset_version

    # 3. The regression: a weaker v2 is registered but never promoted.
    v2 = caliber.prompts.register_version(
        prompt_name, weak, commit_message="v2 dropped the JSON-only rules (intentional regression)"
    )
    check("v2 registered", v2["version"] == 2)
    check("v2 is NOT live (registering is not deploying)", registry(prompt_name) == 1)

    # 4. Evaluate v1 and v2 and catch the regression, or verify the honest refusal.
    expected = [row["expected"] for row in rows]
    if llm_simulated:
        status, detail = status_of(
            lambda: caliber.raw.post(
                "/evaluations",
                json={
                    "dataset_id": dataset.dataset_id,
                    "predict_target": "prompt",
                    "subject_ref": f"{prompt_name}@1",
                    "scorers": EVAL_SCORERS,
                },
            )
        )
        check(
            "evaluation is refused on a simulated model (400)",
            status == 400 and "real LLM" in detail,
            detail[:70],
        )
        results["evaluation"] = "refused: needs a real model"
    else:
        runs = {}
        for version, label in ((1, "baseline v1"), (2, "candidate v2 (weaker)")):
            runs[version] = caliber.evaluations.create(
                dataset.dataset_id,
                predict_target="prompt",
                subject_ref=f"{prompt_name}@{version}",
                scorers=EVAL_SCORERS,
                label=label,
            )
        baseline, weaker = runs[1], runs[2]
        for item in (baseline, weaker):
            failure = evaluation_failure(item)
            check(
                f"evaluation {item.label} completed on every example",
                failure is None
                and item.n_examples == len(rows)
                and not any(row.get("error") for row in item.results or []),
                failure or item.run_id,
            )
            if failure:  # nothing below can be read from a run that did not complete
                _finish(checks)
        n = len(rows)
        needed = int(MIN_CONTRACT_RATE * n)
        report_1 = contract_report([r["prediction"] for r in baseline.results], expected)
        report_2 = contract_report([r["prediction"] for r in weaker.results], expected)
        check(
            "v1 answers are valid JSON (the contract)",
            report_1["valid_json"] >= needed,
            f"{report_1['valid_json']}/{n}, at least {needed} needed",
        )
        check(
            "v1 uses only allowed intents and priorities",
            min(report_1["allowed_intent_values"], report_1["allowed_priority_values"]) >= needed,
            f"{report_1['allowed_intent_values']}/{n} intents, "
            f"{report_1['allowed_priority_values']}/{n} priorities",
        )
        check("v1 has exactly the schema keys", report_1["schema_keys"] >= needed)
        f1_1 = baseline.aggregate.get("token_f1")
        f1_2 = weaker.aggregate.get("token_f1")
        check(
            "the platform scorecard scored both runs (token_f1 in 0..1)",
            all(isinstance(v, int | float) and 0 <= v <= 1 for v in (f1_1, f1_2))
            and all(0 <= (r.pass_rate or 0) <= 1 for r in (baseline, weaker)),
            f"token_f1 {f1_1} -> {f1_2}, pass rate {baseline.pass_rate} -> {weaker.pass_rate}",
        )
        if not checks["the platform scorecard scored both runs (token_f1 in 0..1)"]:
            _finish(checks)
        if not isinstance(f1_1, int | float) or not isinstance(f1_2, int | float):
            raise SystemExit("verification failed: the scorecard has no token_f1")
        drop = (baseline.pass_rate or 0) - (weaker.pass_rate or 0)
        check(
            "the weaker v2 does not beat the baseline on the scorecard",
            f1_2 <= f1_1 + SCORE_NOISE and drop > -SCORE_NOISE,
            f"token_f1 {f1_1:.3f} -> {f1_2:.3f}, pass rate {baseline.pass_rate:.2f} -> "
            f"{weaker.pass_rate:.2f}",
        )
        regressed = [
            row["id"]
            for row, ok_1, ok_2 in zip(
                rows, report_1["contract_ok"], report_2["contract_ok"], strict=True
            )
            if ok_1 and not ok_2
        ]
        # Informational, not asserted: what the weaker prompt did to *this* model. A stand-in model
        # breaks the contract on every row; a real instruction-tuned model usually does not, and
        # then the scorecard drop (wordier answers) is the only signal.
        print(
            f"info v2 broke the contract on {len(regressed)}/{n} answers; "
            f"JSON-valid v1 {report_1['valid_json']}/{n}, v2 {report_2['valid_json']}/{n}; "
            f"labels agreeing v1 {report_1['label_agreement']}/{n}, "
            f"v2 {report_2['label_agreement']}/{n}"
        )
        gate_fires = drop > MAX_PASS_RATE_DROP
        print(
            f"info regression gate (allowed pass-rate drop {MAX_PASS_RATE_DROP}): "
            f"{'FAILS v2' if gate_fires else 'does not fire'} (actual drop {drop:.2f})"
        )
        if expect_regression:
            check(
                "v2 breaks the JSON contract on every answer",
                report_2["valid_json"] == 0,
                f"{report_2['valid_json']}/{n} valid",
            )
            check(
                "every example that met the contract on v1 regressed on v2",
                len(regressed) == report_1["valid_json"] == n,
                len(regressed),
            )
            check(
                "the platform scorecard drops against the baseline",
                f1_2 < f1_1 and gate_fires,
                f"token_f1 {f1_1:.3f} -> {f1_2:.3f}",
            )
            check(
                "the regression gate fails v2",
                gate_fires,
                f"allowed drop {MAX_PASS_RATE_DROP}, actual {drop:.2f}",
            )
        results["evaluation"] = {
            "baseline_run_id": baseline.run_id,
            "candidate_run_id": weaker.run_id,
            "contract_v1": {k: v for k, v in report_1.items() if k != "contract_ok"},
            "contract_v2": {k: v for k, v in report_2.items() if k != "contract_ok"},
            "token_f1": [f1_1, f1_2],
            "pass_rate": [baseline.pass_rate, weaker.pass_rate],
            "regressed_examples": len(regressed),
            "gate_fires": gate_fires,
        }

    # Failure: scoring a test set that does not exist is a 404, not an empty scorecard.
    status, detail = status_of(
        lambda: caliber.raw.post(
            "/evaluations",
            json={
                "dataset_id": "ED-does-not-exist",
                "predict_target": "prompt",
                "subject_ref": f"{prompt_name}@1",
                "scorers": EVAL_SCORERS,
            },
        )
    )
    check("evaluating a missing test set is a 404", status == 404, detail[:70])

    if skip_release:
        results["checks"] = checks
        _finish(checks)
        return results

    # 5. Calibrate the live prompt and wait for the candidate.
    scorers = calibration_scorers(assets)
    gate = None if eval_simulated else gate_from_env()
    request: dict[str, Any] = {
        "agent_id": prompt_name,
        "eval_dataset_id": dataset.dataset_id,
        "optimizer_type": "MetaPrompt",
        "scorers": scorers,
        "prompt_alias": "prod",
    }
    if gate:
        request["gate"] = gate
    calibration = caliber.prompts.create_calibration_run(**request)
    job = caliber.jobs.wait(
        calibration["job"]["job_id"],
        timeout=calibration_timeout,
        interval=poll_interval,
        max_interval=poll_interval,
    )
    if job.status == "failed":
        check("calibration did not fail", False, f"calibration failed: {job.error_message}")
        _finish(checks)
    if job.status == "rejected":
        # A real run can honestly end here: the gate decides, and a small model is a noisy judge.
        status, detail = status_of(lambda: caliber.jobs.apply(job.job_id))
        check("a rejected candidate cannot be applied (409)", status == 409, detail[:70])
        check(
            "calibration reached candidate_ready",
            False,
            f"rejected by the gate: {job.error_message} (loosen it with COOKBOOK_01_MIN_SCORE / "
            "COOKBOOK_01_MAX_REGRESSION, or rerun: the judge varies)",
        )
        _finish(checks)
    check("calibration reached candidate_ready", job.status == "candidate_ready", job.status)
    evidence = job.eval_results or {}
    candidate = job.candidate or {}
    check(
        "the candidate differs from the live prompt",
        bool(candidate.get("content"))
        and candidate["content"] != candidate.get("baseline_content"),
        candidate.get("diff_summary"),
    )
    check(
        "the evidence reports the real test-set size and version",
        evidence.get("n_examples") == len(rows)
        and evidence.get("eval_dataset_version") == dataset_version,
        f"{evidence.get('n_examples')} examples, dataset v{evidence.get('eval_dataset_version')}",
    )
    check("the gate passed", (evidence.get("gate") or {}).get("passed") is True)
    if eval_simulated:
        check(
            "the evidence is marked simulated",
            evidence.get("simulated") is True and evidence.get("provider") == "fake",
            f"provider={evidence.get('provider')} simulated={evidence.get('simulated')}",
        )
    else:
        baseline_score = (evidence.get("baseline") or {}).get("overall")
        candidate_score = (evidence.get("candidate") or {}).get("overall")
        check(
            "the evidence is real: not marked simulated, with measured scores",
            evidence.get("simulated") is False
            and evidence.get("provider") not in (None, "fake")
            and all(
                isinstance(v, int | float) and 0 <= v <= 1
                for v in (baseline_score, candidate_score)
            ),
            f"provider={evidence.get('provider')} simulated={evidence.get('simulated')} "
            f"baseline {baseline_score} -> candidate {candidate_score}",
        )
    results.update(
        job_id=job.job_id,
        candidate_diff=candidate.get("diff_summary"),
        evidence={
            "provider": evidence.get("provider"),
            "simulated": evidence.get("simulated"),
            "n_examples": evidence.get("n_examples"),
            "baseline": (evidence.get("baseline") or {}).get("overall"),
            "candidate": (evidence.get("candidate") or {}).get("overall"),
        },
    )

    # 6. Failure paths. A viewer cannot apply; a failing gate blocks the candidate.
    if viewer is not None:
        status, detail = status_of(lambda: viewer.jobs.apply(job.job_id))
        check("a viewer cannot apply (403)", status == 403, detail[:70])
    if eval_simulated:
        # The simulated evaluator scores every candidate 0.94, so a 0.99 bar always fails. (A real
        # judge could score 1.0, so this check is not made there.)
        strict = caliber.prompts.create_calibration_run(
            **{**request, "gate": {"min_aggregate_score": 0.99}}
        )
        blocked = caliber.jobs.wait(
            strict["job"]["job_id"],
            timeout=calibration_timeout,
            interval=poll_interval,
            max_interval=poll_interval,
        )
        check(
            "a candidate below the gate is rejected", blocked.status == "rejected", blocked.status
        )
        status, detail = status_of(lambda: caliber.jobs.apply(blocked.job_id))
        check("applying a job that is not candidate_ready is a 409", status == 409, detail[:70])
        results["blocked_job_id"] = blocked.job_id

    # 7. Apply, then verify the release everywhere, naming who did it.
    applied = caliber.jobs.apply(job.job_id)
    new_version = int(applied["promotion"]["artifact_ref"].rsplit("/", 1)[1])
    results["applied_version"] = new_version
    check("apply registered a new version", new_version == 3, f"v{new_version}")
    check(
        "the live alias moved in the MLflow registry",
        registry(prompt_name) == new_version,
        f"prod -> v{registry(prompt_name)}",
    )
    versions: dict[int, dict[str, Any]] = {}
    for row in _version_rows(caliber, prompt_name):
        versions[row["version"]] = row
    provenance = versions[new_version].get("provenance") or {}
    check(
        "the registry version carries the job and the evaluation provenance",
        provenance.get("job_id") == job.job_id
        and provenance.get("eval_simulated") == ("true" if eval_simulated else "false"),
        f"job {provenance.get('job_id')}, eval_simulated={provenance.get('eval_simulated')}",
    )
    live = [
        row
        for row in caliber.releases.live()
        if row["artifact_type"] == "prompt" and row["artifact_id"] == prompt_name
    ]
    check(
        "the live board shows the new version, applied by you",
        len(live) == 1 and live[0]["version_id"] == f"v{new_version}" and live[0]["by"] == me,
        f"{live[0]['version_id']} by {live[0]['by']}" if live else None,
    )
    timeline = [
        event
        for event in caliber.releases.timeline(limit=50, entity_type="prompt")
        if event["entity_id"] == prompt_name
    ]
    check(
        "the release timeline shows the promotion by you",
        bool(timeline)
        and timeline[0]["action"] == "promote_prompt"
        and timeline[0]["actor"] == me
        and timeline[0]["details"]["to_version"] == new_version,
        (
            f"v{timeline[0]['details']['from_version']} -> v{timeline[0]['details']['to_version']} "
            f"by {timeline[0]['actor']}"
        )
        if timeline
        else None,
    )
    audit_promote = caliber.audit.list(entity_id=prompt_name, action="promote_prompt")
    audit_apply = caliber.audit.list(entity_id=job.job_id, action="apply_candidate")
    check(
        "the audit log has the promotion and the apply, both by you",
        any(e.actor == me and e.details.get("job_id") == job.job_id for e in audit_promote)
        and any(e.actor == me for e in audit_apply),
    )
    status, detail = status_of(lambda: caliber.jobs.apply(job.job_id))
    check("applying the same job twice is a 409", status == 409, detail[:70])

    # 8. Roll back and verify the alias returned.
    rolled = caliber.prompts.rollback(prompt_name)
    check(
        "rollback restores v1", rolled["version"] == 1 and rolled["rolled_back_from"] == new_version
    )
    check(
        "the live alias returned to v1 in the MLflow registry",
        registry(prompt_name) == 1,
        f"prod -> v{registry(prompt_name)}",
    )
    live = [
        row
        for row in caliber.releases.live()
        if row["artifact_type"] == "prompt" and row["artifact_id"] == prompt_name
    ]
    check("the live board shows v1 again", len(live) == 1 and live[0]["version_id"] == "v1")
    audit_rollback = caliber.audit.list(entity_id=prompt_name, action="rollback_prompt")
    check(
        "the audit log records the rollback by you",
        any(e.actor == me and e.details.get("to_version") == 1 for e in audit_rollback),
    )

    results["checks"] = checks
    _finish(checks)
    return results


def _version_rows(caliber: CaliberClient, name: str) -> list[dict[str, Any]]:
    payload = caliber.prompts.versions(name)
    return list(payload) if isinstance(payload, list) else list(payload.get("data", []))


def _finish(checks: dict[str, bool]) -> None:
    failed = [name for name, ok in checks.items() if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    if failed:
        raise LabVerificationError("failed: " + "; ".join(failed))


def cleanup(caliber: CaliberClient) -> None:
    """Delete what earlier runs created: prompts and archive test sets named ``cookbook-01-*``."""
    for prompt in caliber.prompts.list():
        name: str = getattr(prompt, "name", None) or prompt.agent_id
        if name.startswith(NAME_PREFIX):
            caliber.prompts.delete(name)
            print(f"deleted prompt {name}")
    for dataset in caliber.eval_datasets.list():
        if dataset.name.startswith(NAME_PREFIX):
            caliber.eval_datasets.update(dataset.dataset_id, status="archived")
            print(f"archived test set {dataset.name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--skip-release",
        action="store_true",
        help="stop after evaluation and regression (no calibration, apply or rollback)",
    )
    parser.add_argument(
        "--expect-regression",
        action="store_true",
        help="also assert that the weaker v2 breaks the JSON contract (true of the stand-in model, "
        "not of every real model)",
    )
    parser.add_argument("--cleanup", action="store_true", help="delete what earlier runs created")
    parser.add_argument("--suffix", help="name suffix (default: a timestamp)")
    args = parser.parse_args(argv)
    # A scored run is synchronous: the server answers when every example has been answered, which
    # takes minutes on a real model, so the default 30 s client timeout would cut it off.
    with CaliberClient(
        os.environ["CALIBER_BASE_URL"],
        token=os.environ["CALIBER_TOKEN"],
        timeout=float(os.environ.get("COOKBOOK_01_HTTP_TIMEOUT", "900")),
    ) as caliber:
        if args.cleanup:
            cleanup(caliber)
            return 0
        viewer = (
            None
            if args.skip_release
            else make_viewer_client(caliber, os.environ["CALIBER_BASE_URL"])
        )
        try:
            result = run(
                caliber,
                suffix=args.suffix,
                viewer=viewer,
                skip_release=args.skip_release,
                expect_regression=args.expect_regression,
            )
        except LabVerificationError as exc:
            print(f"\nVERIFICATION FAILED: {exc}", file=sys.stderr)
            return 1
        finally:
            if viewer is not None:
                viewer.close()
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
