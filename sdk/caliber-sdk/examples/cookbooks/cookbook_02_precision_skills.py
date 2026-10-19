"""Cookbook 02 (Precision Skills), run end to end against a real server.

The lab's whole API path, from the dataset files to a verified outcome:

    author the skill -> render preview -> trigger round 1 (misses) -> tighten the trigger
    phrases -> trigger round 2 (clean) -> record + pin the baseline -> calibrate -> review
    the candidate -> apply -> roll back -> export and re-import the package -> failure
    paths (a viewer, invalid content, a calibration with no baseline) -> archive.

It exits 0 only when every documented outcome is verified. The assets it reads are the
lab's own files under ``docs-site/cookbooks/02-skill-trigger-packaging-lab/assets/``.

Run it from the repository root, against a server that has a ``caliber.admin`` and
``caliber.operator`` token (the ``scripts/cookbook-env.sh`` stack signs in as ``admin``)::

    export CALIBER_BASE_URL=http://127.0.0.1:5001
    export CALIBER_TOKEN=calpat_...
    PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python \
        -m examples.cookbooks.cookbook_02_precision_skills

Two modes, detected from ``GET /readiness`` (``simulated`` empty means a real provider):

* Simulated stack (the default): the candidate text and the scores are canned, and a skill
  calibration needs no test set. The script asserts the wiring (a candidate is produced, flagged
  simulated, with ``n_examples`` 0, and it applies and rolls back as versions), never a score.
* Real provider (``scripts/cookbook-env.sh up NAME --flags cookbooks --real-llm``): the script
  loads the 8-row skill test set, calibrates against it with a note about what to fix, and
  asserts invariants only: ``simulated`` is false, ``n_examples`` is the size of the test set, the
  scores are in range, the candidate differs from the skill and applies and rolls back. A
  calibration with no test set is asserted to fail with the evaluator's own error. It never
  asserts generated text or an exact score.

Trigger selection is deterministic and needs no model in either mode.
"""

from __future__ import annotations

import io
import json
import os
import secrets
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from caliber_sdk import CaliberClient
from caliber_sdk.errors import CaliberAPIError
from caliber_sdk.transport import API_PREFIX
from examples.cookbooks._helpers import env_client

SKILL_FILE = "skills/support-tone-and-citation.md"
TIGHTENED_FILE = "skills/trigger-phrases.json"
INVALID_FILE = "skills/invalid-skills.json"
CASES_FILE = "dataset/trigger-cases.jsonl"
RENDER_FILE = "dataset/render-vars.json"
TEST_SET_FILE = "dataset/skill-eval-cases.jsonl"
NOTES_FILE = "skills/calibration-notes.txt"

#: ``trigger_accuracy_min`` in the lab's ``verification.yaml``.
TRIGGER_ACCURACY_MIN = 0.95

VIEWER_USER = "lab02-viewer"
VIEWER_PASSWORD = "lab02-viewer-pw-1"


def assets_dir() -> Path:
    override = os.environ.get("COOKBOOK_02_ASSETS")
    if override:
        return Path(override)
    repo_root = Path(__file__).resolve().parents[4]
    return repo_root / "docs-site/cookbooks/02-skill-trigger-packaging-lab/assets"


def load_skill(assets: Path) -> tuple[dict[str, Any], str]:
    """The skill asset: its frontmatter (name, summary, tags, ...) and the body below it."""
    try:
        import yaml  # type: ignore[import-untyped,unused-ignore]
    except ImportError as exc:  # pragma: no cover - environment
        raise SystemExit("this example reads YAML frontmatter: pip install pyyaml") from exc
    text = (assets / SKILL_FILE).read_text(encoding="utf-8")
    _, frontmatter, body = text.split("---\n", 2)
    return yaml.safe_load(frontmatter), body.strip()


def load_cases(assets: Path) -> list[dict[str, Any]]:
    lines = (assets / CASES_FILE).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def load_test_set(assets: Path) -> list[dict[str, Any]]:
    """The skill's test set: reply cases, each ``{id, tags, input, expected}``."""
    lines = (assets / TEST_SET_FILE).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def failure_text(job: Any) -> str:
    """The server's reason a job stopped (``error_message``; older payloads say ``error``)."""
    return str(job.error_message or job.error or "")


def is_real_stack(caliber: CaliberClient) -> bool:
    """True when the server runs real providers (``/readiness`` lists nothing as simulated)."""
    return not (caliber.readiness() or {}).get("simulated")


def create_test_set(
    caliber: CaliberClient, name: str, owner: str, rows: list[dict[str, Any]]
) -> str:
    """Import the test-set file as an eval dataset and return its id."""
    dataset = caliber.eval_datasets.create(name, owner=owner, description="Support reply cases")
    for row in rows:
        caliber.eval_datasets.add_example(
            dataset.dataset_id, input=row["input"], expected=row["expected"], tags=row["tags"]
        )
    return str(dataset.dataset_id)


#: Provider errors worth one more attempt: the model refused, was rate limited or timed out.
TRANSIENT_MARKERS = ("429", "rate limit", "timed out", "timeout", "temporarily", "overloaded")
#: A real calibration is model-dependent: up to two retries when the gate rejects the candidate
#: (eight test rows are coarse; a single flipped row can trip it) or the provider hiccups. More
#: than that is a real failure and is reported.
REAL_CALIBRATION_ATTEMPTS = 3


def calibrate_for_real(
    caliber: CaliberClient, skill_id: str, test_set_id: str, notes: str
) -> tuple[Any, int]:
    """Calibrate against the test set until a candidate is ready; returns ``(job, attempts)``."""
    last: Any = None
    for attempt in range(1, REAL_CALIBRATION_ATTEMPTS + 1):
        queued = caliber.skills.calibrate(skill_id, eval_dataset_id=test_set_id, notes=notes)
        last = caliber.jobs.wait(queued["job"]["job_id"], timeout=600)
        if last.status == "candidate_ready":
            return last, attempt
        error = failure_text(last).lower()
        retryable = last.status == "rejected" or any(m in error for m in TRANSIENT_MARKERS)
        if not retryable:
            break
    raise AssertionError(
        f"{last.job_id} ended as {last.status!r} after {attempt} attempt(s): {failure_text(last)}"
    )


def real_evidence(job: Any, test_set_id: str, n_rows: int) -> dict[str, Any]:
    """Assert the invariants of real calibration evidence and return what the report shows."""
    evidence = job.eval_results or {}
    assert evidence.get("simulated") is False, evidence
    assert evidence.get("provider") not in (None, "fake"), evidence
    assert evidence.get("n_examples") == n_rows, evidence
    assert evidence.get("eval_dataset_id") == test_set_id, evidence
    candidate = evidence["candidate"]["overall"]
    baseline = evidence["baseline"]["overall"]
    assert 0.0 <= candidate <= 1.0 and 0.0 <= baseline <= 1.0, evidence
    assert evidence["gate"]["passed"] is True, evidence
    return {
        "candidate_overall": candidate,
        "baseline_overall": baseline,
        "dimensions": sorted(evidence["candidate"]["dimensions"]),
        "diff_summary": (job.candidate or {}).get("diff_summary"),
    }


def run_cases(
    caliber: CaliberClient, skill_id: str, cases: list[dict[str, Any]], *, context: bool = True
) -> list[dict[str, Any]]:
    """Check every case with the real selector and judge it against its expectation.

    ``context=False`` sends the message alone, which is what the Skills page's Scenario Sets
    runner does; ``True`` also sends the case's ``artifact_type`` and ``session_goal``.
    """
    rows = []
    for case in cases:
        inputs = case["inputs"]
        selection = caliber.skills.test_selection(
            skill_id,
            inputs["user_message"],
            artifact_type=inputs.get("artifact_type") if context else None,
            session_goal=inputs.get("session_goal") if context else None,
        )
        expected = case["expectations"]["is_selected"]
        rows.append(
            {
                "id": case["id"],
                "expected": expected,
                "selected": selection.is_selected,
                "score": selection.selection_score,
                "reason": selection.selection_reason,
                "ok": selection.is_selected == expected,
                "inputs": inputs,
            }
        )
    return rows


def tally(rows: list[dict[str, Any]]) -> dict[str, Any]:
    wrong = [row for row in rows if not row["ok"]]
    return {
        "cases": len(rows),
        "correct": len(rows) - len(wrong),
        "accuracy": round((len(rows) - len(wrong)) / len(rows), 3),
        "false_positives": sorted(row["id"] for row in wrong if row["selected"]),
        "false_negatives": sorted(row["id"] for row in wrong if not row["selected"]),
    }


def save_run(
    caliber: CaliberClient, skill_id: str, version: int, rows: list[dict[str, Any]]
) -> str:
    """Persist a round as a durable ``selection`` run, the way the Trigger Tests page does."""
    results = [
        {
            "name": row["id"],
            "input": {"user_message": row["inputs"]["user_message"]},
            "output": {"is_selected": row["selected"], "selection_score": row["score"]},
            "verdict": "pass" if row["ok"] else "fail",
            "score": 1.0 if row["ok"] else 0.0,
            "reasoning": row["reason"],
        }
        for row in rows
    ]
    run = caliber.skills.create_test_run(
        skill_id=skill_id, kind="selection", skill_version=version, results=results
    )
    return str(run["test_run_id"])


def reason_of(exc: CaliberAPIError) -> str:
    """The server's reason, without the request line and id the exception also carries."""
    errors = getattr(exc, "errors", None)
    if errors:
        return "; ".join(
            f"{'.'.join(str(part) for part in item.get('loc', []))}: {item.get('msg')}"
            for item in errors
        )
    return exc.detail or str(exc)


def expect_status(label: str, status: int, call: Callable[[], Any]) -> str:
    """Run ``call`` and require it to fail with HTTP ``status``; return the server's reason."""
    try:
        call()
    except CaliberAPIError as exc:
        assert exc.status_code == status, f"{label}: expected {status}, got {exc.status_code}"
        return f"{status} {reason_of(exc)}"
    raise AssertionError(f"{label}: expected HTTP {status}, but the call succeeded")


def wait_for_candidate(caliber: CaliberClient, job_id: str) -> Any:
    job = caliber.jobs.wait(job_id, timeout=120)
    assert job.status == "candidate_ready", f"{job_id} ended as {job.status!r}: {failure_text(job)}"
    return job


def make_viewer_client(admin: CaliberClient) -> CaliberClient:
    """A client signed in as a plain account: ``caliber.viewer`` scope only.

    The account is created (or its password reset and re-enabled) through the admin client, it
    signs in once with its password, and the script keeps only a personal access token.
    """
    base_url = os.environ["CALIBER_BASE_URL"]
    try:
        admin.auth.accounts.create(VIEWER_USER, VIEWER_PASSWORD)
    except CaliberAPIError:
        admin.auth.accounts.update(VIEWER_USER, password=VIEWER_PASSWORD, disabled=False)
    with httpx.Client(base_url=base_url.rstrip("/") + API_PREFIX, timeout=30) as http:
        login = http.post("/auth/login", json={"user_id": VIEWER_USER, "password": VIEWER_PASSWORD})
        login.raise_for_status()
        issued = http.post("/auth/tokens", json={"name": "lab02-viewer"})
        issued.raise_for_status()
        token = issued.json()["data"]["token"]
    return CaliberClient(base_url, token=token)


def run(
    caliber: CaliberClient,
    *,
    viewer: CaliberClient | None = None,
    skill_name: str | None = None,
) -> dict[str, Any]:
    assets = assets_dir()
    spec, content = load_skill(assets)
    cases = load_cases(assets)
    tightened = json.loads((assets / TIGHTENED_FILE).read_text(encoding="utf-8"))["test_triggers"]
    render = json.loads((assets / RENDER_FILE).read_text(encoding="utf-8"))
    suffix = secrets.token_hex(3)
    name = skill_name or f"{spec['name']}-{suffix}"
    owner = caliber.me.get().user_id
    created: list[str] = []
    report: dict[str, Any] = {"skill_name": name, "cases": len(cases)}

    # 1. Author. The skill starts with only the six starter exclusion phrases from the asset.
    skill = caliber.skills.create(
        name,
        owner=owner,
        content=content,
        summary=spec["summary"],
        category=spec["category"],
        tags=spec["tags"],
        skill_metadata={"test_triggers": {"should_not_trigger": spec["should_not_trigger"]}},
    )
    created.append(skill.skill_id)
    skill_id = skill.skill_id
    assert skill.version == 1
    report["skill_id"] = skill_id

    # 2. Render preview: every variable supplied, then only one.
    full = caliber.skills.render(skill_id, variables=render["variables"])
    assert full.unresolved_variables == render["expected"]["unresolved_variables"] == []
    assert (
        "[KB-1042]" in full.rendered_content and "non-technical customer" in full.rendered_content
    )
    partial = caliber.skills.render(skill_id, variables=render["partial"]["variables"])
    assert partial.unresolved_variables == render["partial"]["expected"]["unresolved_variables"]
    report["render"] = {
        "detected_variables": full.detected_variables,
        "unresolved": full.unresolved_variables,
        "word_count": full.word_count,
        "partial_unresolved": partial.unresolved_variables,
    }

    # 3. Trigger round 1, with the starter phrases only.
    round1 = run_cases(caliber, skill_id, cases)
    report["round1"] = tally(round1)
    assert report["round1"]["accuracy"] < TRIGGER_ACCURACY_MIN, (
        "round 1 was expected to miss cases: the starter phrases are deliberately incomplete"
    )
    round1_run = save_run(caliber, skill_id, skill.version or 1, round1)

    # 4. Tighten: replace the trigger phrases with the tightened set (summary and tags stay).
    updated = caliber.skills.update(
        skill_id, skill_metadata={**skill.skill_metadata, "test_triggers": tightened}
    )
    assert updated.version == 1, "trigger phrases are metadata: they must not bump the version"

    # 5. Trigger round 2, then record it and pin it as the baseline.
    round2 = run_cases(caliber, skill_id, cases)
    report["round2"] = tally(round2)
    assert report["round2"]["accuracy"] >= TRIGGER_ACCURACY_MIN, report["round2"]
    message_only = tally(run_cases(caliber, skill_id, cases, context=False))
    assert message_only["accuracy"] >= TRIGGER_ACCURACY_MIN, message_only
    report["round2_message_only"] = message_only
    baseline_run = save_run(caliber, skill_id, 1, round2)
    caliber.skills.set_baseline(skill_id, test_run_id=baseline_run)
    workspace = caliber.skills.workspace(skill_id)
    assert workspace["baseline_run_id"] == baseline_run
    report["runs"] = {"round1": round1_run, "baseline": baseline_run}

    # 6. Calibrate (a refinement job) and review the candidate. On a simulated stack the
    # evidence is canned and needs no test set; on a real one the candidate is scored against
    # the skill's test set, so the script imports it first.
    real = is_real_stack(caliber)
    report["mode"] = "real" if real else "simulated"
    test_rows = load_test_set(assets)
    attempts = 1
    if real:
        test_set_id = create_test_set(caliber, f"{name}-replies", owner, test_rows)
        notes = (assets / NOTES_FILE).read_text(encoding="utf-8").strip()
        job, attempts = calibrate_for_real(caliber, skill_id, test_set_id, notes)
        job_id = job.job_id
    else:
        queued = caliber.skills.calibrate(skill_id)
        job_id = queued["job"]["job_id"]
        job = wait_for_candidate(caliber, job_id)
    assert job.candidate and job.candidate["content"] != content
    assert job.candidate["baseline_content"].strip() == content
    evidence = job.eval_results or {}
    calibration: dict[str, Any] = {
        "job_id": job_id,
        "status": job.status,
        "simulated": evidence.get("simulated"),
        "provider": evidence.get("provider"),
        "n_examples": evidence.get("n_examples"),
        "gate_passed": (evidence.get("gate") or {}).get("passed"),
    }
    if real:
        calibration.update(real_evidence(job, test_set_id, len(test_rows)))
        calibration["attempts"] = attempts
    else:
        assert evidence.get("n_examples") == 0, "a skill calibration has no test set"
    report["calibration"] = calibration

    # 7. Apply, 8. roll back. Versions only ever move forward.
    applied = caliber.jobs.apply(job_id)
    assert applied["status"] == "applied"
    after_apply = caliber.skills.get(skill_id)
    assert after_apply.version == 2 and after_apply.content != content
    rolled = caliber.skills.rollback(skill_id)
    after_rollback = caliber.skills.get(skill_id)
    assert after_rollback.version == 3
    assert (after_rollback.content or "").strip() == content
    assert rolled["version"] == 3
    history = caliber.skills.versions(skill_id)
    assert [v.version_number for v in history] == [3, 2, 1]
    assert history[0].content == history[2].content
    report["versions"] = {"after_apply": 2, "after_rollback": 3, "history": [3, 2, 1]}

    # 9. Package: export, then import under a new name and compare behaviour. (A third strategy,
    # ``merge``, updates the same-named skill in place and is admin-only; the README shows it.)
    archive = caliber.skills.package_zip(skill_id)
    members = sorted(zipfile.ZipFile(io.BytesIO(archive)).namelist())
    assert members == [f"{name}/SKILL.md", f"{name}/agents/openai.yaml"], members
    conflict = expect_status(
        "re-import under the same name",
        409,
        lambda: caliber.skills.import_package_zip(f"{name}.zip", archive),
    )
    copy_name = f"{name}-copy"
    copy_row = caliber.skills.import_package_zip(
        f"{name}.zip", archive, conflict_strategy="rename", rename_to=copy_name
    )
    copy_id = copy_row["skill_id"]
    created.append(copy_id)
    copy = caliber.skills.get(copy_id)
    assert copy.content == (after_rollback.content or "").strip()
    assert caliber.skills.render(
        copy_id, variables=render["variables"]
    ).rendered_content.strip() == (
        caliber.skills.render(skill_id, variables=render["variables"]).rendered_content.strip()
    )
    # The package carries name, description, content and the OpenAI interface. It does not carry
    # tags, category or trigger phrases, and the summary is shortened to the 64-character
    # interface line, so the copy decides some cases differently until those are put back.
    assert copy.tags == [] and copy.category == "custom"
    assert "test_triggers" not in copy.skill_metadata
    before = run_cases(caliber, copy_id, cases)
    source_decisions = {row["id"]: row["selected"] for row in round2}
    differing_before = sorted(
        row["id"] for row in before if row["selected"] != source_decisions[row["id"]]
    )
    caliber.skills.update(
        copy_id,
        tags=spec["tags"],
        category=spec["category"],
        summary=spec["summary"],
        skill_metadata={**copy.skill_metadata, "test_triggers": tightened},
    )
    after = run_cases(caliber, copy_id, cases)
    differing_after = sorted(
        row["id"] for row in after if row["selected"] != source_decisions[row["id"]]
    )
    assert differing_after == [], differing_after
    report["package"] = {
        "members": members,
        "reimport_same_name": conflict,
        "copy_name": copy_name,
        "content_identical": True,
        "decisions_differing_before_metadata_restored": differing_before,
        "decisions_differing_after_metadata_restored": differing_after,
    }

    # 10a. A viewer cannot change anything (the 403 names the missing scope).
    failures: dict[str, Any] = {}
    if viewer is not None:
        failures["viewer"] = {
            "apply": expect_status("viewer apply", 403, lambda: viewer.jobs.apply(job_id)),
            "calibrate": expect_status(
                "viewer calibrate", 403, lambda: viewer.skills.calibrate(skill_id)
            ),
            "rollback": expect_status(
                "viewer rollback", 403, lambda: viewer.skills.rollback(skill_id)
            ),
            "create": expect_status(
                "viewer create",
                403,
                lambda: viewer.skills.create("viewer-skill", owner="x", content="x"),
            ),
        }
    # 10b. Invalid skill content is rejected before anything is stored.
    invalid = json.loads((assets / INVALID_FILE).read_text(encoding="utf-8"))
    failures["invalid"] = {}
    for item in invalid:
        reason = expect_status(
            item["label"],
            item["status"],
            lambda payload=item["payload"]: caliber.raw.post(  # type: ignore[misc]
                "/skills", json=payload
            ),
        )
        assert item["expect"] in reason, (item["label"], reason)
        failures["invalid"][item["label"]] = reason
    duplicate = expect_status(
        "duplicate name", 409, lambda: caliber.skills.create(name, owner=owner, content="x")
    )
    failures["invalid"]["name already in use"] = duplicate
    # 10c. Calibrating with no baseline pinned: allowed, nothing to compare. Simulated: the same
    # canned evidence. Real: it still scores against the test set, and with no test set at all
    # the run fails with the evaluator's own error.
    # The real evaluator gates on an absolute score, so the real run uses the lab's own
    # instructions (a one-line skill scores below the gate on the support cases).
    bare = caliber.skills.create(
        f"{name}-bare",
        owner=owner,
        content=content if real else "Reply kindly to {{ who }}.",
        summary=spec["summary"] if real else None,
    )
    created.append(bare.skill_id)
    assert caliber.skills.workspace(bare.skill_id)["baseline_run_id"] is None
    if real:
        bare_job, _ = calibrate_for_real(caliber, bare.skill_id, test_set_id, notes)
        assert bare_job.eval_results and bare_job.eval_results.get("simulated") is False
        no_test_set = caliber.jobs.wait(
            caliber.skills.calibrate(bare.skill_id)["job"]["job_id"], timeout=600
        )
        assert no_test_set.status == "failed", no_test_set.status
        assert "no active examples" in failure_text(no_test_set), failure_text(no_test_set)
        failures["no_test_set"] = {"job_id": no_test_set.job_id, "status": no_test_set.status}
    else:
        bare_job = wait_for_candidate(
            caliber, caliber.skills.calibrate(bare.skill_id)["job"]["job_id"]
        )
        assert bare_job.eval_results and bare_job.eval_results.get("simulated") is True
    failures["no_baseline"] = {
        "job_id": bare_job.job_id,
        "status": bare_job.status,
        "baseline_run_id": caliber.skills.workspace(bare.skill_id)["baseline_run_id"],
    }
    report["failures"] = failures

    # 11. Archive everything this run created; an archived skill leaves the default list.
    for skill_id_to_archive in created:
        caliber.skills.update(skill_id_to_archive, status="archived")
    active = {s.skill_id for s in caliber.skills.list()}
    assert not active & set(created), "an archived skill must leave the default list"
    assert all(caliber.skills.get(sid).status == "archived" for sid in created)
    report["archived"] = len(created)
    report["verified"] = True
    return report


def main() -> None:
    with env_client() as caliber:
        viewer = make_viewer_client(caliber)
        with viewer:
            report = run(
                caliber, viewer=viewer, skill_name=os.environ.get("COOKBOOK_02_SKILL_NAME")
            )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
