"""The published-site gate must stay wired up and must stay honest.

``.github/scripts/verify_published_site.py`` fetches the deployed Pages site and
fails the run when a path the workflow promises to publish is not being served.
It exists because ``deploy`` reporting success only means the deployment API
accepted an artifact: the composed docs-plus-report layout was correct in
``pages.yml`` from the commit that introduced it and absent from the web for 59
hours, with every run involved reporting green.

A gate like that has two ways to rot quietly. It can be disconnected -- left in
the repo but not run, which is how ``package`` came to be skipped on twelve
consecutive runs -- or it can promise a path the build never produces, which
turns a real failure into noise that gets the check deleted. These tests close
both, and drive the script's decision logic directly rather than trusting that
it fails when it should.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pages.yml"
VERIFIER = REPO_ROOT / ".github" / "scripts" / "verify_published_site.py"
DOCS_SITE = REPO_ROOT / "docs-site"

yaml = pytest.importorskip("yaml", reason="PyYAML is needed to parse the workflow")


def _verifier_module():
    spec = importlib.util.spec_from_file_location("verify_published_site", VERIFIER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _needs(job: dict) -> set[str]:
    """``needs:`` accepts a scalar or a sequence; treat both as a set.

    Worth normalising rather than assuming: a scalar ``needs: deploy`` iterates
    as characters, so a naive ``set(...)`` produces ``{'d', 'e', ...}`` and the
    membership check quietly answers the wrong question.
    """
    declared = job.get("needs") or []
    return {declared} if isinstance(declared, str) else set(declared)


# --- wiring -------------------------------------------------------------------


def test_the_verify_job_runs_after_every_deployment() -> None:
    """The gate has to be downstream of deploy, or it checks the previous site."""
    jobs = _workflow()["jobs"]
    assert "verify" in jobs, (
        "the published-site gate is gone from pages.yml. A deployment that "
        "reports success while serving nothing is the defect this job exists for."
    )
    assert "deploy" in _needs(jobs["verify"]), (
        "verify no longer waits for deploy, so it would probe the site as it was "
        "before this run published."
    )


def test_the_deploy_job_is_bounded() -> None:
    """An unbounded deploy is what wedged the queue for 59 hours.

    The concurrency group is deliberately not cancel-in-progress, so one run that
    never finishes holds it and every later run queues behind it or is cancelled
    -- none of them reporting failure. A timeout is what turns that silence red.
    """
    deploy = _workflow()["jobs"]["deploy"]
    assert deploy.get("timeout-minutes"), (
        "the deploy job has no timeout-minutes. Without one a deployment stuck in "
        "`waiting` blocks the pages concurrency group indefinitely and no run "
        "reports a failure."
    )


def test_the_gate_actually_runs_the_verifier() -> None:
    """A job that exists but no longer invokes the script is the same as no job."""
    steps = _workflow()["jobs"]["verify"]["steps"]
    commands = " ".join(str(step.get("run", "")) for step in steps)
    assert "verify_published_site.py" in commands


# --- the promise matches the build --------------------------------------------


def test_every_required_page_is_one_the_docs_build_produces() -> None:
    """A promised path the build cannot emit would fail forever and be deleted.

    ``tests/`` is exempt because it is staged at publish time from CI's Allure
    artifact and never exists in the tree -- which is precisely why it is worth
    asserting on the live site instead.
    """
    module = _verifier_module()
    for path, _why in module.REQUIRED_PATHS:
        if path in ("", "tests/"):
            continue
        assert (DOCS_SITE / path).is_file(), (
            f"verify_published_site.py requires {path!r} but docs-site/ does not "
            "contain it, so the gate would fail every run for a reason that has "
            "nothing to do with publication."
        )


def test_the_required_paths_cover_the_two_independently_produced_halves() -> None:
    """Root and /tests/ come from different producers and both must be checked.

    The 59-hour outage was exactly one half going missing while the other served,
    so a gate that checked only the root would have stayed green throughout.
    """
    module = _verifier_module()
    paths = {path for path, _ in module.REQUIRED_PATHS}
    assert "" in paths, "the site root is unchecked"
    assert "tests/" in paths, (
        "the Allure report path is unchecked -- the half that actually vanished"
    )


# --- decision logic -----------------------------------------------------------


def _stub_probe(module, monkeypatch, statuses: dict[str, int]) -> None:
    """Answer each probed URL from ``statuses``, keyed by the trailing path."""

    def probe(url: str) -> tuple[int, str]:
        for path in sorted(statuses, key=len, reverse=True):
            if url.endswith(path) or (path == "" and url.endswith("/")):
                return statuses[path], ""
        return 404, ""

    monkeypatch.setattr(module, "probe", probe)


def test_a_fully_served_site_passes(monkeypatch) -> None:
    module = _verifier_module()
    _stub_probe(module, monkeypatch, {path: 200 for path, _ in module.REQUIRED_PATHS})
    assert module.main(["verify", "https://example.test/site/"]) == 0


def test_a_missing_tests_tree_fails_the_run(monkeypatch) -> None:
    """The exact shape of the incident: docs serve, the report is gone.

    Before this gate existed this state reported success on every run for two
    days. If this test stops failing, that silence is back.
    """
    module = _verifier_module()
    statuses = {path: 200 for path, _ in module.REQUIRED_PATHS}
    statuses["tests/"] = 404
    _stub_probe(module, monkeypatch, statuses)
    assert module.main(["verify", "https://example.test/site/"]) == 1


def test_an_unreachable_site_fails_the_run(monkeypatch) -> None:
    """Unreachable is not evidence of a good publish."""
    module = _verifier_module()
    _stub_probe(module, monkeypatch, {path: 0 for path, _ in module.REQUIRED_PATHS})
    assert module.main(["verify", "https://example.test/site/"]) == 1


def test_a_missing_base_url_is_refused() -> None:
    """An empty PAGE_URL must not silently probe nothing and pass."""
    module = _verifier_module()
    assert module.main(["verify", "   "]) == 1
    assert module.main(["verify"]) == 1
