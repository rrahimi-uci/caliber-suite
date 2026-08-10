"""SDK documentation snippets come from executable code.

The SDK plan requires published examples to be extracted from tested sources
rather than hand-written, so a snippet cannot quietly stop working. That only
holds if two things are true: the docs use the extraction mechanism, and the
sources they point at are the ones the SDK test suite runs.

Both are checked here, because a page that drifted back to a fenced ```python
block would still render perfectly and prove nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SDK_DOCS = REPO_ROOT / "docs" / "sdk"
EXAMPLES = REPO_ROOT / "sdk" / "caliber-sdk" / "examples"
EXAMPLE_TESTS = REPO_ROOT / "sdk" / "caliber-sdk" / "tests" / "test_examples.py"

#: ```python-example\npath#symbol\n```
EXAMPLE_FENCE = re.compile(r"```python-example\s*\n\s*([^\s`]+)\s*\n```", re.MULTILINE)


def _referenced() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for page in sorted(SDK_DOCS.glob("*.md")):
        for spec in EXAMPLE_FENCE.findall(page.read_text(encoding="utf-8")):
            path, _, symbol = spec.partition("#")
            found.append((path, symbol))
    return found


def test_the_guide_embeds_examples_rather_than_inlining_them() -> None:
    referenced = _referenced()
    # A ratchet, not a floor with slack: raise it when a page adds an example,
    # so "the docs stopped embedding one" is a failure rather than a margin.
    assert len(referenced) >= 7, (
        f"only {len(referenced)} embedded examples; a page has probably reverted "
        "to a hand-written ```python block, which cannot be kept honest"
    )


def test_every_referenced_example_exists() -> None:
    """A stale reference fails the docs build; this names it sooner."""
    missing: list[str] = []
    for path, symbol in _referenced():
        source = REPO_ROOT / path
        if not source.is_file():
            missing.append(f"{path} (file)")
            continue
        if not re.search(rf"^def {re.escape(symbol)}\b", source.read_text(encoding="utf-8"), re.M):
            missing.append(f"{path}#{symbol} (symbol)")
    assert not missing, f"SDK docs reference examples that do not exist: {missing}"


def test_every_referenced_example_is_executed_by_the_test_suite() -> None:
    """The claim the docs make about their own snippets.

    Embedding a function proves it is real code. Only the test suite proves it
    is *working* code, so each embedded symbol must be imported there.
    """
    assert EXAMPLE_TESTS.is_file(), "the SDK example tests are missing"
    executed = EXAMPLE_TESTS.read_text(encoding="utf-8")
    unexercised = [f"{path}#{symbol}" for path, symbol in _referenced() if symbol not in executed]
    assert not unexercised, f"SDK docs publish examples the test suite never runs: {unexercised}"


def test_the_examples_directory_is_not_dead_code() -> None:
    """Every example function is published somewhere, or it is unused."""
    published = {symbol for _, symbol in _referenced()}
    defined: set[str] = set()
    for source in EXAMPLES.glob("*.py"):
        if source.name == "__init__.py":
            continue
        defined.update(
            re.findall(r"^def ([a-z_][a-z0-9_]*)\b", source.read_text(encoding="utf-8"), re.M)
        )
    orphaned = sorted(defined - published)
    assert not orphaned, (
        f"example functions nothing publishes: {orphaned}. Reference them from "
        "docs/sdk/ or delete them."
    )
