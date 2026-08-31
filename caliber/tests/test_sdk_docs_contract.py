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
import subprocess
import sys
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


# --- the generated API reference -------------------------------------------
#
# The reference exists so a developer never has to open the SDK source. That
# claim fails quietly: a broken cross-link still renders, a phantom type still
# looks plausible, and a missing "Raises" reads as "cannot fail". Each test below
# pins a defect the published page actually shipped with.


def _generated_reference() -> str:
    """Run the generator the docs build runs, and return its markdown."""
    script = REPO_ROOT / "docs-site" / "generate_sdk_docs.py"
    assert script.is_file(), "the SDK docs generator is missing"
    result = subprocess.run(
        [sys.executable, str(script), "reference"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode == 0, f"generator failed: {result.stderr[-2000:]}"
    return result.stdout


def test_type_cross_links_are_links_and_not_literal_markdown() -> None:
    """``**Returns:** `[X](#x)``` renders the brackets, not a link.

    Markdown does not process links inside a code span, so wrapping the
    linkifier's output in backticks published 82 return types as the literal
    text ``[CaliberClient](#caliberclient)``. The goal of cross-linked types was
    defeated by the wrapping, and it looked fine in the source.
    """
    reference = _generated_reference()
    broken = re.findall(r"`\[[A-Za-z0-9_]+\]\(#[a-z0-9-]+\)`", reference)
    assert not broken, f"{len(broken)} cross-links are inside code spans: {broken[:5]}"


def test_the_reference_advertises_no_type_the_sdk_does_not_have() -> None:
    """``_List`` is an internal alias for ``list``, not a type anyone can use.

    It exists because a resource class that defines its own ``list()`` method
    shadows the builtin for annotations written after it. That is a real
    constraint on the SDK and none of a reader's business -- but it leaked into
    48 published signatures, where it reads as a type to look up and find
    nothing about.
    """
    reference = _generated_reference()
    phantom = sorted(set(re.findall(r"\b_[A-Z][A-Za-z0-9_]*\b", reference)))
    assert not phantom, f"internal aliases leaked into the reference: {phantom}"


def test_no_private_attribute_is_documented_as_public_api() -> None:
    """Listing ``_transport`` invites callers to reach for it."""
    reference = _generated_reference()
    rows = re.findall(r"^\| `(_[a-z][A-Za-z0-9_]*)` \| ", reference, re.M)
    # Parameter tables legitimately carry ``_``-named parameters (``__exit__``
    # takes ``*_``), so only attribute and field rows are checked -- those are
    # the ones that claim to be public API.
    leaked = [name for name in rows if name not in {"_", "_csrf_retry"}]
    assert not leaked, f"private attributes documented as public: {sorted(set(leaked))}"


def test_every_method_that_performs_a_request_documents_its_exceptions() -> None:
    """The exceptions a caller must handle propagate; they are not in the body.

    ``CaliberClient.whoami()`` has no ``raise`` statement anywhere in it, so a
    body scan reported that it raises nothing -- along with ``Transport.get()``
    and seventeen others. Every one of them performs a request and every one can
    fail with a typed error, which is the single most important thing to document
    about them.
    """
    reference = _generated_reference()
    missing: list[str] = []
    for section in re.split(r"\n##### `", reference)[1:]:
        class_name = section.split("`")[0]
        if class_name not in {"CaliberClient", "Transport", "AsyncTransport"}:
            continue
        for member in re.split(r"\n###### `", section)[1:]:
            name = member.split("(")[0]
            if name.startswith("__") or "**Raises" in member:
                continue
            # Genuinely cannot raise: local accessors, a factory that *returns*
            # an exception, and ``bootstrap_csrf``, which catches and returns
            # None because a deployment with CSRF disabled serves no token.
            # ``capabilities_api``/``datasets`` are deprecated aliases (AD-6):
            # pure attribute access plus a warning, delegating to an
            # already-documented resource rather than performing a request
            # themselves.
            if name in {
                "close",
                "aclose",
                "url_for",
                "error_for_response",
                "bootstrap_csrf",
                "stability",
                "capabilities_api",
                "datasets",
            }:
                continue
            missing.append(f"{class_name}.{name}")
    assert not missing, f"request methods with no documented exceptions: {missing}"


def test_the_reference_covers_the_whole_public_package() -> None:
    """A reference that silently skipped a module would read as complete.

    Compared against the package tree rather than a hardcoded count, so a new
    module is covered without this test being edited.
    """
    reference = _generated_reference()
    package = REPO_ROOT / "sdk" / "caliber-sdk" / "src" / "caliber_sdk"
    expected: set[str] = set()
    for path in package.rglob("*.py"):
        if path.name.startswith("_") and path.name != "__init__.py":
            continue
        parts = path.relative_to(package.parent).with_suffix("").parts
        name = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
        expected.add(name)

    documented = set(re.findall(r"^### Module `([\w.]+)`", reference, re.M))
    assert expected <= documented, f"undocumented modules: {sorted(expected - documented)}"


def test_documented_error_payloads_match_what_the_server_actually_sends() -> None:
    """A wrong example is worse than no example: it compiles and then fails.

    The guide published an error body with ``error_code``, ``message``, and
    ``fields`` — a shape ``caliber.routes._errors`` has never produced. A reader
    writing ``error.payload["fields"]`` against it would get a KeyError against a
    live deployment, having done exactly what the reference told them to.

    Checked by key, not by exact text, so prose around the example stays free to
    change.
    """
    import json

    guide = (SDK_DOCS / "guide.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```json\n(.*?)```", guide, re.S)
    assert blocks, "the guide no longer shows the JSON error envelope"

    envelopes = [
        json.loads(block)
        for block in blocks
        if isinstance(json.loads(block), dict) and "detail" in json.loads(block)
    ]
    assert envelopes, "no documented error envelope carries the server's `detail` key"

    for envelope in envelopes:
        assert set(envelope) <= {"detail", "status_code", "errors"}, (
            f"documented error envelope has keys the server does not send: "
            f"{sorted(set(envelope) - {'detail', 'status_code', 'errors'})}"
        )
        for item in envelope.get("errors", []):
            assert set(item) == {"loc", "msg", "type"}, (
                f"documented field error should carry exactly loc/msg/type, got {sorted(item)}"
            )
            assert isinstance(item["loc"], list), "`loc` is a path, so it is a list"


def test_every_documented_class_is_reachable_from_the_symbol_index() -> None:
    """A reader arrives knowing a name, not the module that defines it.

    Without a flat index, finding ``WorkflowRun`` meant already knowing which of
    29 modules declares it — the source-diving the reference exists to replace.
    Asserted as coverage rather than a count, so a new class is indexed without
    this test being edited.
    """
    reference = _generated_reference()
    assert "## Symbol index" in reference, "the reference lost its symbol index"

    index_block = reference.split("## Symbol index", 1)[1].split("\n## ", 1)[0]
    indexed = set(re.findall(r"^\| \[`([A-Za-z0-9_]+)`\]", index_block, re.M))
    documented = set(re.findall(r"^##### `([A-Za-z0-9_]+)`", reference, re.M))

    assert documented <= indexed, (
        f"classes missing from the symbol index: {sorted(documented - indexed)}"
    )
