"""`scripts/ci-local.sh` must stay in step with `.github/workflows/ci.yml`.

A local CI runner is only worth having if a green local run means something. The way it
stops meaning something is drift: a job is added to the workflow, nobody adds it to the
script, and the script keeps reporting "local CI passed" while silently not running it.
That is the same failure mode as a control that is off by default with no surface — it
looks like coverage and is not.

So the parity is asserted rather than maintained by discipline. If you add a job to CI
that executes code, this test fails until the script knows about it.

Jobs with no possible local equivalent are exempt and named explicitly with the reason,
because "we deliberately do not run this locally" is a decision that should be visible
in code rather than inferred from an omission. Anything executing product code must not
be on that list.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML is needed to parse the workflow")

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SCRIPT = REPO_ROOT / "scripts" / "ci-local.sh"

#: Workflow jobs the local runner deliberately does not mirror, with the reason. Each
#: one either publishes evidence to GitHub or inspects the GitHub run itself, so there
#: is nothing for a local shell to do. Anything *executing product code* must not be on
#: this list.
UNMIRRORED_JOBS = {
    "allure-report": "renders and publishes the Allure HTML report to GitHub Pages",
    "gate-ledger": (
        "reads this run's job conclusions from the Actions API to catch a required "
        "gate that was skipped; there is no run to inspect locally. Its coherence is "
        "covered by tests/test_ci_gate_ledger_contract.py, which the `test` job runs"
    ),
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
    missing = _workflow_jobs() - _script_jobs() - set(UNMIRRORED_JOBS)
    assert not missing, (
        f"CI jobs with no local counterpart: {sorted(missing)}. Add them to ALL_JOBS and "
        "give each a job_<name> function in scripts/ci-local.sh, or — if the job has no "
        "possible local equivalent — add it to UNMIRRORED_JOBS here with the reason."
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


def test_unmirrored_exemptions_are_still_real_jobs() -> None:
    """An exemption for a job that no longer exists is dead weight that would mask a
    future job of the same name."""
    stale = set(UNMIRRORED_JOBS) - _workflow_jobs()
    assert not stale, f"UNMIRRORED_JOBS names jobs the workflow no longer has: {sorted(stale)}"


def test_remote_and_local_package_gates_require_index_and_assets() -> None:
    """A wheel with only one half of the SPA is not a valid UI package.

    This used to be ``not any(index OR assets)`` in Actions, which passed when *either*
    the HTML shell or its asset directory existed. Keep the two independent requirements
    aligned with the local packaging gate.
    """
    required = 'if "caliber/ui/index.html" not in names or not any('
    assert required in WORKFLOW.read_text()
    assert required in SCRIPT.read_text()


def test_local_package_gate_always_rebuilds_the_spa() -> None:
    """A stale ``dist/`` must never be accepted merely because index.html exists."""
    text = SCRIPT.read_text()
    rebuild = '(cd "$UI_DIR" && CALIBER_DOCS_STRICT=1 npm run build) || return 1'
    stage = "rm -rf src/caliber/ui && mkdir -p src/caliber/ui"
    assert rebuild in text
    assert text.index(rebuild) < text.index(stage)
    assert '[ -f "caliber-ui/dist/index.html" ] ||' not in text


def test_remote_and_local_ui_gates_both_run_eslint() -> None:
    """Job-name parity must not hide a missing check inside a shared UI job."""
    workflow = WORKFLOW.read_text()
    script = SCRIPT.read_text()
    assert "run: npx eslint ." in workflow
    assert "&& npx eslint ." in script


def test_remote_and_local_backend_gates_use_the_grouped_xdist_scheduler() -> None:
    """Large free-function modules stay balanced; sensitive groups stay serialized."""
    workflow = WORKFLOW.read_text()
    script = SCRIPT.read_text()
    assert "pytest -n auto --dist loadgroup" in workflow
    assert "pytest -n auto --dist loadgroup" in script


def test_docs_sources_are_not_excluded_from_pull_request_ci() -> None:
    """The UI prebuild consumes ``docs/**`` and checks generated-copy parity."""
    assert '- "docs/**"' not in WORKFLOW.read_text()


def test_remote_and_local_compose_gates_validate_the_merged_quiet_model() -> None:
    """Both gates must expand every profile without disclosing resolved values."""
    workflow = WORKFLOW.read_text()
    script = SCRIPT.read_text()
    required = (
        "--env-file deploy/.env.example",
        "--env-file .env.example",
        "-f deploy/compose.yaml",
        "--profile app",
        "--profile nats",
        "config --quiet",
    )

    assert 'COMPOSE_DISABLE_ENV_FILE: "1"' in workflow
    assert "COMPOSE_DISABLE_ENV_FILE=1 docker compose" in script
    for fragment in required:
        assert fragment in workflow
        assert fragment in script


def test_the_ruff_pin_in_ci_matches_the_one_developers_install() -> None:
    """A version skew here fails CI on files the author never touched.

    ruff's formatter output shifts between releases, so a developer whose venv
    resolved a newer ruff formats a file the way *their* version wants, and CI's
    pinned version rejects it. The diff then points at unrelated files, which is
    the least useful possible signal -- and it happened: a local ruff 0.16.1
    disagreed with CI's 0.15.20 on three files nobody had edited.

    Both pins are asserted equal so bumping one alone is a test failure rather
    than a confusing CI failure later.
    """
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    ci_pins = re.findall(r'ruff==([0-9][^"\'\s]*)', workflow)
    assert ci_pins, "the CI lint job no longer pins ruff to an exact version"

    pyproject = (REPO_ROOT / "caliber" / "pyproject.toml").read_text(encoding="utf-8")
    dev_pins = re.findall(r'"ruff==([0-9][^"]*)"', pyproject)
    assert dev_pins, (
        "caliber's dev extras no longer pin ruff exactly; a developer venv would "
        "be free to resolve a version whose formatter disagrees with CI's"
    )

    assert set(ci_pins) == set(dev_pins), (
        f"ruff pin drifted: CI installs {sorted(set(ci_pins))}, "
        f"dev extras install {sorted(set(dev_pins))}"
    )
