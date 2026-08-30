"""The SDK<->API parity gate.

``test_sdk_against_server.py`` proves the SDK is *correct* on the paths it
already models -- it never asserts anything about paths it does not model.
Coverage therefore drifted from parity to roughly half the API with no test
ever failing (see ``sdk-completeness-plan.md``). This module is the fix: it
enumerates every operation the live server actually serves and requires each
one to be accounted for, in one of exactly three ways:

1. Reachable through a typed ``caliber-sdk`` method (measured by
   ``docs-site/sdk_coverage.py``, the one shared implementation this gate and
   the published REST-inventory coverage table both use -- so the published
   docs can never claim more than this test verifies).
2. Listed in ``coverage_allowlist.toml`` under ``[[exclusion]]`` -- permanent,
   never SDK scope (a static asset, a browser-cookie-only auth flow, ...).
3. Listed under ``[[gap]]`` -- temporary, tracked debt naming the
   ``sdk-completeness-plan.md`` wave that closes it.

The gate fails in **both** directions: a live operation with no SDK method and
no allowlist entry is new, untracked drift; a ``[[gap]]`` entry the SDK now
covers, or one naming a path the server no longer serves, is stale
bookkeeping. Closing a gap therefore means deleting its allowlist entry in the
same PR that adds the SDK method -- the allowlist's size is the literal
countdown to SDK completeness.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from caliber.routes.openapi import PREFIX, build_openapi_document

REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST_PATH = REPO_ROOT / "sdk" / "caliber-sdk" / "coverage_allowlist.toml"

# ``docs-site/sdk_coverage.py`` is the shared, stdlib-only measurement both
# this gate and ``generate_rest_api_docs.py`` import -- see that module's
# docstring for why it lives outside both packages rather than being
# duplicated into one of them.
sys.path.insert(0, str(REPO_ROOT / "docs-site"))
import sdk_coverage  # noqa: E402


def _live_operations(app: Any) -> dict[tuple[str, str], dict[str, str]]:
    """``{(METHOD, prefix-relative path): {"tag": ..., "stability": ...}}``
    for every operation the live route table serves.

    Built from :func:`build_openapi_document` rather than the raw route
    table directly, so this gate and the served document can never disagree
    about what a "tag" or a "stability tier" is -- it is the same function
    ``test_routes_openapi.py`` proves matches the routes exactly.
    """
    doc = build_openapi_document(app)
    operations: dict[tuple[str, str], dict[str, str]] = {}
    for path, methods in doc["paths"].items():
        assert path.startswith(PREFIX), f"undocumented path escaped the prefix: {path!r}"
        relative = path[len(PREFIX) :] or "/"
        normalized = sdk_coverage.normalize_path(relative)
        for method, operation in methods.items():
            operations[(method.upper(), normalized)] = {
                "tag": operation["tags"][0],
                "stability": operation["x-caliber-stability"],
            }
    return operations


def _load_allowlist() -> tuple[
    dict[tuple[str, str], str], dict[tuple[str, str], dict[str, str]]
]:
    """Parse ``coverage_allowlist.toml`` into ``(exclusions, gaps)`` keyed by
    ``(METHOD, path)``, each value carrying the entry's own fields for the
    staleness checks below.
    """
    payload = tomllib.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
    exclusions = {
        (str(entry["method"]).upper(), str(entry["path"])): str(entry["reason"])
        for entry in payload.get("exclusion", [])
    }
    gaps = {
        (str(entry["method"]).upper(), str(entry["path"])): {
            "tag": str(entry["tag"]),
            "stability": str(entry["stability"]),
            "wave": str(entry["wave"]),
        }
        for entry in payload.get("gap", [])
    }
    return exclusions, gaps


@pytest.fixture(scope="module")
def allowlist() -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], dict[str, str]]]:
    return _load_allowlist()


@pytest.fixture(scope="module")
def covered() -> set[tuple[str, str]]:
    return sdk_coverage.covered_operations(REPO_ROOT)


def test_the_allowlist_parses_and_has_no_duplicate_entries() -> None:
    """A duplicate key would silently let one shadow the other in the dict
    build above; catch it as its own failure rather than a confusing count
    mismatch downstream."""
    payload = tomllib.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
    for section in ("exclusion", "gap"):
        seen: set[tuple[str, str]] = set()
        for entry in payload.get(section, []):
            key = (str(entry["method"]).upper(), str(entry["path"]))
            assert key not in seen, f"duplicate [[{section}]] entry for {key}"
            seen.add(key)


def test_every_gap_entry_names_a_real_wave(
    allowlist: tuple[dict[tuple[str, str], str], dict[tuple[str, str], dict[str, str]]],
) -> None:
    """A typo'd wave (``"3A"``, ``"3f"``) would silently vanish from every
    per-wave report; this is the one place that catches it."""
    _, gaps = allowlist
    valid_waves = {"3a", "3b", "3c", "3d", "3e"}
    bad = {key: entry["wave"] for key, entry in gaps.items() if entry["wave"] not in valid_waves}
    assert not bad, f"gap entries with an unrecognized wave: {bad}"


def test_no_allowlist_entry_names_an_operation_the_server_no_longer_serves(
    client: TestClient,
    allowlist: tuple[dict[tuple[str, str], str], dict[tuple[str, str], dict[str, str]]],
) -> None:
    """A route rename or removal must be reflected here in the same PR --
    otherwise the allowlist quietly stops meaning anything for that entry."""
    live = _live_operations(client.app)
    exclusions, gaps = allowlist
    stale_exclusions = sorted(set(exclusions) - set(live))
    stale_gaps = sorted(set(gaps) - set(live))
    assert not stale_exclusions, (
        "coverage_allowlist.toml [[exclusion]] entries for operations the server "
        f"no longer serves (delete or fix the path): {stale_exclusions}"
    )
    assert not stale_gaps, (
        "coverage_allowlist.toml [[gap]] entries for operations the server no "
        f"longer serves (delete or fix the path): {stale_gaps}"
    )


def test_no_gap_entry_overlaps_an_exclusion(
    allowlist: tuple[dict[tuple[str, str], str], dict[tuple[str, str], dict[str, str]]],
) -> None:
    """An operation is either permanently out of scope or temporarily
    uncovered, never both -- an overlap means one of the two entries is
    wrong and the file has stopped being self-consistent."""
    exclusions, gaps = allowlist
    overlap = sorted(set(exclusions) & set(gaps))
    assert not overlap, f"operations listed as both excluded and a tracked gap: {overlap}"


def test_no_gap_entry_is_actually_covered_by_the_sdk_already(
    allowlist: tuple[dict[tuple[str, str], str], dict[tuple[str, str], dict[str, str]]],
    covered: set[tuple[str, str]],
) -> None:
    """The countdown only means something if closing a gap requires deleting
    its entry. Left in place, a stale ``[[gap]]`` silently overstates the
    remaining work and never gets caught by anything else."""
    _, gaps = allowlist
    now_covered = sorted(set(gaps) & covered)
    assert not now_covered, (
        "coverage_allowlist.toml [[gap]] entries the SDK already covers -- "
        f"delete these entries: {now_covered}"
    )


def test_every_live_operation_is_covered_or_allowlisted(
    client: TestClient,
    allowlist: tuple[dict[tuple[str, str], str], dict[tuple[str, str], dict[str, str]]],
    covered: set[tuple[str, str]],
) -> None:
    """The gate itself.

    Fails on any operation the live server serves that has neither a typed
    SDK method nor an allowlist entry -- which is exactly what "the SDK
    quietly fell behind a new route" looks like. The failure message names
    the tag and stability tier so a contributor can tell at a glance whether
    it belongs in this PR or is genuinely new debt for a future wave.
    """
    live = _live_operations(client.app)
    exclusions, gaps = allowlist
    accounted_for = covered | set(exclusions) | set(gaps)
    untracked = sorted(set(live) - accounted_for)
    if untracked:
        detail = "\n".join(
            f"  {method} {path}  (tag={live[(method, path)]['tag']!r}, "
            f"stability={live[(method, path)]['stability']!r})"
            for method, path in untracked
        )
        pytest.fail(
            "Server operations with no typed SDK method and no "
            "coverage_allowlist.toml entry -- either add SDK coverage, or add "
            "a [[gap]] (naming the wave that will close it) or [[exclusion]] "
            f"(with a reason) entry:\n{detail}"
        )


def test_the_coverage_gap_is_shrinking_not_growing(
    allowlist: tuple[dict[tuple[str, str], str], dict[tuple[str, str], dict[str, str]]],
    covered: set[tuple[str, str]],
) -> None:
    """A soft, human-readable summary rather than a hard threshold -- the
    ratchet is enforced by the two tests above (nothing can be added to the
    allowlist without also being live-and-uncovered, and nothing already
    covered can remain on it), so this test cannot itself be gamed by
    padding. It exists to make the current state legible in CI output."""
    _, gaps = allowlist
    total_addressable = len(covered) + len(gaps)
    if total_addressable == 0:
        pytest.skip("no addressable operations measured")
    pct = 100 * len(covered) / total_addressable
    print(
        f"\nSDK addressable coverage: {len(covered)}/{total_addressable} ({pct:.1f}%); "
        f"{len(gaps)} operations remain in coverage_allowlist.toml"
    )
