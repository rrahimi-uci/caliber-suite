"""`scripts/ci-local.sh` must stay in step with `.github/workflows/ci.yml`.

A local CI runner is only worth having if a green local run means something. The way it
stops meaning something is drift: a job is added to the workflow, nobody adds it to the
script, and the script keeps reporting "local CI passed" while silently not running it.
That is the same failure mode as a control that is off by default with no surface — it
looks like coverage and is not.

So the parity is asserted rather than maintained by discipline. If you add a job to CI
that executes code, this test fails until the script knows about it.

Jobs that only *publish* are exempt and named explicitly, because "we deliberately do
not run this locally" is a decision that should be visible in code rather than inferred
from an omission.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML is needed to parse the workflow")

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SCRIPT = REPO_ROOT / "scripts" / "ci-local.sh"

#: Workflow jobs the local runner deliberately does not mirror, with the reason. These
#: publish evidence to GitHub — an artifact, a rendered report, a Pages site — and have
#: no local equivalent. Anything *executing product code* must not be on this list.
PUBLISH_ONLY_JOBS = {
    "allure-report": "renders and publishes the Allure HTML report to GitHub Pages",
}


def _workflow_jobs() -> set[str]:
    data = yaml.safe_load(WORKFLOW.read_text())
    return set(data["jobs"])


def _script_jobs() -> set[str]:
    """The job names the script advertises in ``ALL_JOBS``."""
    text = SCRIPT.read_text()
    match = re.search(r"^ALL_JOBS=\(([^)]*)\)", text, re.M)
    assert match, "ALL_JOBS not found in ci-local.sh"
    return set(match.group(1).split())


def test_the_script_exists_and_is_executable() -> None:
    assert SCRIPT.exists(), f"{SCRIPT} is missing"
    # Documented as `scripts/ci-local.sh`, so it has to be runnable that way rather than
    # only via `bash scripts/ci-local.sh`.
    assert SCRIPT.stat().st_mode & 0o111, "ci-local.sh is not executable (chmod +x)"


def test_every_code_executing_ci_job_has_a_local_counterpart() -> None:
    """The drift guard. A job added to CI and not to the script fails here."""
    missing = _workflow_jobs() - _script_jobs() - set(PUBLISH_ONLY_JOBS)
    assert not missing, (
        f"CI jobs with no local counterpart: {sorted(missing)}. Add them to ALL_JOBS and "
        "give each a job_<name> function in scripts/ci-local.sh, or — if the job only "
        "publishes evidence — add it to PUBLISH_ONLY_JOBS here with the reason."
    )


def test_the_script_does_not_advertise_jobs_ci_does_not_have() -> None:
    """The other direction, so the script cannot imply coverage of something that no
    longer exists in the workflow."""
    extra = _script_jobs() - _workflow_jobs()
    assert not extra, f"ci-local.sh lists jobs absent from the workflow: {sorted(extra)}"


def test_each_advertised_job_has_an_implementation() -> None:
    """``ALL_JOBS`` is a list of strings; nothing stops it naming a function that does
    not exist, and the dispatch would then fall through to 'unknown job'."""
    text = SCRIPT.read_text()
    for job in sorted(_script_jobs()):
        fn = "job_" + job.replace("-", "_")
        assert f"{fn}()" in text, f"{job} is advertised but has no {fn}() implementation"
        # And it must be reachable from the dispatch table, not merely defined.
        assert re.search(rf"^\s*{re.escape(job)}\)\s*run_job", text, re.M), (
            f"{job} has an implementation but is not wired into the case dispatch"
        )


def test_the_extras_list_matches_the_workflow() -> None:
    """The local run must install the same optional dependencies.

    A narrower local set makes an import error look like a local environment problem; a
    wider one hides a genuinely missing CI dependency.
    """
    data = yaml.safe_load(WORKFLOW.read_text())
    workflow_extras = set(str(data["env"]["CALIBER_CI_EXTRAS"]).split(","))
    match = re.search(r'^CI_EXTRAS="([^"]*)"', SCRIPT.read_text(), re.M)
    assert match, "CI_EXTRAS not found in ci-local.sh"
    assert set(match.group(1).split(",")) == workflow_extras, (
        "ci-local.sh CI_EXTRAS has drifted from the workflow's CALIBER_CI_EXTRAS"
    )


def test_publish_only_exemptions_are_still_real_jobs() -> None:
    """An exemption for a job that no longer exists is dead weight that would mask a
    future job of the same name."""
    stale = set(PUBLISH_ONLY_JOBS) - _workflow_jobs()
    assert not stale, f"PUBLISH_ONLY_JOBS names jobs the workflow no longer has: {sorted(stale)}"
