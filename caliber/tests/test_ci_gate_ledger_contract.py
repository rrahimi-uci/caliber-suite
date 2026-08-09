"""The gate ledger must name gates that exist.

``.github/scripts/gate_ledger.py`` fails a run that would otherwise report success
while a required gate produced no evidence. It identifies gates by job name, which
means a renamed job silently becomes a gate that "did not run" -- loud, but only
after a push, and loud in a way that invites deleting the entry rather than fixing
it.

These tests close that loop locally: the ledger's list and the workflow's job names
are checked against each other, and the matrix legs are expanded the way GitHub
expands them. The list stays hand-written on purpose -- discovering it from the
workflow would make it agree with any workflow, including one that lost a gate.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
LEDGER = REPO_ROOT / ".github" / "scripts" / "gate_ledger.py"

yaml = pytest.importorskip("yaml", reason="PyYAML is needed to parse the workflow")


def _ledger_module():
    spec = importlib.util.spec_from_file_location("gate_ledger", LEDGER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _job_names() -> dict[str, str]:
    """Every job name the workflow can produce, mapped back to its job key.

    Matrix legs are expanded the way GitHub expands them, so
    ``Compatibility (Python ${{ matrix.python-version }})`` becomes one entry per
    declared version -- which is how the names appear in the API the ledger reads.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    names: dict[str, str] = {}
    for key, job in workflow["jobs"].items():
        template = job.get("name", key)
        matrix = (job.get("strategy") or {}).get("matrix") or {}
        placeholders = re.findall(r"\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}", template)
        if not placeholders:
            names[template] = key
            continue
        for placeholder in placeholders:
            for value in matrix.get(placeholder, []):
                expanded = re.sub(
                    rf"\$\{{\{{\s*matrix\.{placeholder}\s*\}}\}}", str(value), template
                )
                names[expanded] = key
    return names


def test_every_required_gate_names_a_job_the_workflow_defines() -> None:
    required = set(_ledger_module().REQUIRED_GATES)
    unknown = required - set(_job_names())

    assert not unknown, (
        "gate_ledger.py requires gates no job produces, so every run would report "
        f"them as missing evidence: {sorted(unknown)}. Rename them here, or drop "
        "them if the gate is genuinely gone."
    )


def test_the_ledger_waits_for_every_gate_it_reports_on() -> None:
    """Reading a conclusion before the job has one yields ``None``.

    The ledger reads ``None`` as "did not run", so a gate it reports on but does
    not wait for would produce a false alarm on every run -- which trains readers
    to ignore the job, recreating the silence it exists to break.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    waited_for = set(workflow["jobs"]["gate-ledger"].get("needs") or [])
    by_name = _job_names()

    unwaited = {
        gate for gate in _ledger_module().REQUIRED_GATES if by_name.get(gate) not in waited_for
    }

    assert not unwaited, (
        f"gate-ledger reports on gates it does not wait for: {sorted(unwaited)}. "
        "Add their job keys to its `needs:`."
    )


def test_the_package_gate_is_not_sequenced_behind_unrelated_jobs() -> None:
    """The wheel/SPA check must not be reachable only when everything else passes.

    This job asserts the wheel contains a bundled SPA. It was skipped on twelve
    consecutive runs because it sat behind six unrelated jobs, and the first time
    it ran it failed. A wheel with no SPA is broken whether or not the tests pass,
    so sequencing it behind them removes evidence exactly when it is most wanted.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # ``needs:`` accepts a scalar or a sequence. A bare string iterates as
    # characters, so normalise before comparing or the assertion below answers a
    # question about letters.
    declared = workflow["jobs"]["package"].get("needs") or []
    needs = {declared} if isinstance(declared, str) else set(declared)

    assert needs <= {"ui"}, (
        f"the wheel build now waits on {sorted(needs)}. Every entry beyond 'ui' "
        "(whose dist artifact it consumes) is a job whose failure silently skips "
        "this gate rather than failing it."
    )


# --- the decision logic itself ------------------------------------------------
#
# The tests above check the ledger's *list* against the workflow. They never run
# it. That left the branch the whole control exists for -- "this run would have
# reported success while a required gate produced no evidence" -- with no
# coverage at all, and it has never executed on a real run either: every run so
# far either passed everything or had a genuine failure that took the cascade
# exemption instead. A never-executed detector is the exact defect the ledger was
# built to find, so it is driven directly here.


def _run_ledger(monkeypatch, tmp_path, jobs, *, self_name="Gate execution ledger"):
    """Drive ``main()`` against a fabricated job list."""
    module = _ledger_module()
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "1")
    monkeypatch.setenv("GH_TOKEN", "token")
    monkeypatch.setenv("GITHUB_JOB_NAME", self_name)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setattr(module, "fetch_jobs", lambda *a, **k: jobs)
    return module, module.main()


def _all_gates(conclusion="success"):
    module = _ledger_module()
    return [{"name": name, "conclusion": conclusion} for name in module.REQUIRED_GATES]


def test_a_run_with_every_gate_green_passes(monkeypatch, tmp_path) -> None:
    _, exit_code = _run_ledger(monkeypatch, tmp_path, _all_gates())
    assert exit_code == 0


def test_a_skipped_gate_with_nothing_failing_fails_the_run(monkeypatch, tmp_path) -> None:
    """The branch the ledger exists for, and the one that had never run.

    A skip with no upstream failure is the dangerous shape: every other job is
    green, so the run reports success while one gate said nothing about this
    commit. That is `package` on twelve consecutive runs, which is the incident
    this control was built after.
    """
    jobs = _all_gates()
    jobs[0]["conclusion"] = "skipped"
    _, exit_code = _run_ledger(monkeypatch, tmp_path, jobs)
    assert exit_code == 1


def test_a_gate_missing_from_the_run_entirely_fails_the_run(monkeypatch, tmp_path) -> None:
    """A renamed or deleted job is absent, not skipped, and must not read as fine."""
    jobs = _all_gates()[1:]
    _, exit_code = _run_ledger(monkeypatch, tmp_path, jobs)
    assert exit_code == 1


def test_a_skip_that_follows_a_real_failure_is_reported_not_failed(monkeypatch, tmp_path) -> None:
    """The deliberate exemption: a `needs:` cascade is already red.

    Failing here would re-report an existing failure and train readers to ignore
    the job, so it must stay a 0. Pinned because it is the exemption that makes
    the strict branch above safe to keep.
    """
    jobs = _all_gates()
    jobs[0]["conclusion"] = "failure"
    jobs[1]["conclusion"] = "skipped"
    _, exit_code = _run_ledger(monkeypatch, tmp_path, jobs)
    assert exit_code == 0


def test_an_unreadable_jobs_api_fails_closed(monkeypatch, tmp_path) -> None:
    """An unreachable API must not be reported as "every gate ran"."""
    module = _ledger_module()
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "1")
    monkeypatch.setenv("GH_TOKEN", "token")

    def _boom(*_a, **_k):
        raise TimeoutError("api unreachable")

    monkeypatch.setattr(module, "fetch_jobs", _boom)
    assert module.main() == 1


def test_missing_credentials_fail_closed(monkeypatch, tmp_path) -> None:
    """No token means no evidence, which is not the same as no problem."""
    module = _ledger_module()
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "1")
    assert module.main() == 1


def test_the_ledger_does_not_count_itself_as_a_gate(monkeypatch, tmp_path) -> None:
    """It is still in progress while it reads the list, so it must exclude itself.

    If it counted its own null conclusion it would fail every run, which is the
    failure mode that gets a control deleted rather than fixed.
    """
    jobs = [*_all_gates(), {"name": "Gate execution ledger", "conclusion": None}]
    _, exit_code = _run_ledger(monkeypatch, tmp_path, jobs)
    assert exit_code == 0
