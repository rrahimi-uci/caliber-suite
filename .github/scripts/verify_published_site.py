#!/usr/bin/env python3
"""Check that the published Pages site actually serves what it claims to.

A deploy job reporting success means the deployment API accepted an artifact.
It does not mean a reader can fetch the site, and the two came apart badly: the
composed docs-plus-report layout was correct in ``pages.yml`` from the commit
that introduced it, and for 59 hours it was absent from the web while every run
involved reported green. The workflow file and the live site were two copies of
one fact with nothing comparing them.

This is the comparison. It reads the deployed URL over the network and asserts
the paths the workflow promises to publish, so a publish that silently drops
half the site fails the run that produced it.

Deliberately *not* a link crawler. The failure this exists to catch is a whole
missing tree -- ``/tests/`` gone because the report was never staged, a page gone
because a deployment was stale -- not an individual broken anchor. A crawler
would be slower, flakier, and would fail on the report's ~9k generated files for
reasons that have nothing to do with whether publication worked.
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request

#: Paths the workflow promises to publish, and why each one is here.
#:
#: Each entry is (path, why). "Required" means a 200; anything else fails the
#: run. Keep this list short and load-bearing -- one representative per
#: independently-produced part of the site, because the defect being caught is
#: an entire part going missing.
REQUIRED_PATHS: tuple[tuple[str, str], ...] = (
    ("", "the docs site root; absent means the deployment did not land at all"),
    (
        "m-00-layered-architecture.html",
        "a generated module page; absent means build-docs output was not uploaded",
    ),
    (
        "m-19-runbook.html",
        "the operations runbook; it was announced as published while 404ing for two days",
    ),
    (
        "overview-video.html",
        "the narrated overview player linked from the README",
    ),
    (
        "media/caliber-overview.mp4",
        "the audio-bearing MP4 used by the narrated overview player",
    ),
    (
        "tests/",
        "the Allure report staged from CI; the part that silently vanished when "
        "two workflows raced for the same URL",
    ),
)

TIMEOUT_SECONDS = 30


def probe(url: str) -> tuple[int, str]:
    """Return ``(status, detail)`` for ``url``, never raising.

    A network error is reported as status 0 rather than propagating: the caller
    prints every result before failing, and one unreachable path should not hide
    the status of the others.
    """
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "caliber-ci"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, ""
    except urllib.error.HTTPError as exc:
        return exc.code, exc.reason or ""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, str(exc)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].strip():
        print("usage: verify_published_site.py <base-url>", file=sys.stderr)
        return 1
    base = argv[1].strip().rstrip("/") + "/"

    width = max(len(path) or 1 for path, _ in REQUIRED_PATHS)
    print(f"\nPublished-site verification\n{'=' * (width + 40)}\n")
    print(f"  base: {base}\n")

    failures: list[tuple[str, int, str]] = []
    for path, why in REQUIRED_PATHS:
        status, detail = probe(base + path)
        ok = status == 200
        mark = "." if ok else "!"
        shown = path or "(root)"
        print(f" {mark} {shown:<{width}}  {status or 'unreachable'}")
        if not ok:
            failures.append((shown, status, f"{why}{f' [{detail}]' if detail else ''}"))

    if not failures:
        print(f"\nall {len(REQUIRED_PATHS)} published paths served\n")
        return 0

    print(
        f"\nERROR: the deployment reported success but {len(failures)} promised "
        f"path(s) are not being served:",
        file=sys.stderr,
    )
    for shown, status, why in failures:
        print(f"  {shown}  ->  {status or 'unreachable'}\n      {why}", file=sys.stderr)
    print(
        "\nA publisher with no reader is indistinguishable from a publisher that "
        "works. Either fix the staging step that should have produced this path, "
        "or remove it from REQUIRED_PATHS so the promise matches what is shipped.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
