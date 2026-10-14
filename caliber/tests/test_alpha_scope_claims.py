"""The alpha scope page's factual claims are checked against the code (alpha gap P1-15).

``docs/start/alpha-scope.md`` tells testers what the alpha does not do. A limitation that
stops being true, or a default that changed, would make the page lie in the other direction,
so the claims that can be checked mechanically are checked here. The backup page is held to
the standard that failed before: it must name the store where prompt versions live.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from caliber.config import CaliberConfig
from caliber.routes import observability, verification

REPO = Path(__file__).resolve().parents[2]
SCOPE = REPO / "docs" / "start" / "alpha-scope.md"
BACKUP = REPO / "docs" / "operate" / "backup-and-recovery.md"
README = REPO / "README.md"

pytestmark = pytest.mark.skipif(not SCOPE.exists(), reason="repository docs not present")


def _text(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_the_page_states_every_known_limitation() -> None:
    page = _text(SCOPE)
    for phrase in (
        "No trace-to-refinement automation",
        "visible to every signed-in user",
        "no two-person rule",
        "CSRF protection is off by default",
        "Simulated by default",
        "SQLite needs a separate MLflow store",
        "process boundary, not isolation",
        "Knowledge bases need an S3-compatible bucket",
        "not rehearsed",
        "No upgrade guarantees",
    ):
        assert phrase.lower() in page.lower(), phrase


def test_csrf_really_is_off_by_default() -> None:
    assert CaliberConfig().csrf_enabled is False
    assert "CALIBER_CSRF_ENABLED" in _text(SCOPE)


def test_providers_really_default_to_simulated() -> None:
    config = CaliberConfig()
    assert config.llm_provider == "fake"
    assert config.eval_provider == "fake"


def test_verifying_an_item_really_starts_no_job() -> None:
    """The route's own source states it; the page repeats it."""
    source = Path(verification.__file__).read_text(encoding="utf-8")
    assert "No CaliberRefinementJob is created here" in source
    assert "does not start a job" in _text(SCOPE)


def test_the_feedback_route_no_longer_claims_polling() -> None:
    doc = observability.post_feedback.__doc__ or ""
    assert "feeds CALIBER's assessment-polling" not in doc
    assert "does **not** start a refinement" in doc


def test_the_readme_does_not_describe_a_trace_watcher() -> None:
    readme = _text(README)
    assert "nothing watches production traces" in readme.lower()
    assert not re.search(r"production trace\s*→\s*flagged assessment\s*→\s*verify", readme)


def test_the_backup_page_names_the_store_that_holds_prompt_versions() -> None:
    page = _text(BACKUP)
    assert "MLflow backend store" in page
    assert "Prompt versions and the `prod` alias live there" in page
    for command in (
        "pg_dump",
        "pg_restore",
        'sqlite3 caliber.db ".backup',
        "rsync -a",
        "mc mirror",
    ):
        assert command in page, command
    assert "Not rehearsed" in page
    assert "CALIBER_SECRET_ENCRYPTION_KEY_SOURCE" in page


def test_the_backup_page_links_no_missing_disaster_recovery_plan() -> None:
    page = BACKUP.read_text(encoding="utf-8")
    assert "disaster-recovery plan" not in page.lower()
    for target in re.findall(r"\]\(([^)#]+\.md)", page):
        assert (BACKUP.parent / target).resolve().exists(), target
