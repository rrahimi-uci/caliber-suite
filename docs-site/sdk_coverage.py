#!/usr/bin/env python3
"""Shared measurement of which management-API operations ``caliber-sdk`` reaches.

Two independent callers must never compute this differently, so it lives in
exactly one place:

* ``docs-site/generate_rest_api_docs.py`` — renders the published per-tag
  coverage table from it, replacing the hand-maintained ``SDK_SURFACE_MAP``
  that could silently overstate coverage.
* ``caliber/tests/test_sdk_api_coverage.py`` — the CI gate that fails when a
  server operation has neither a typed SDK method nor an allowlist entry
  naming the work that will close it.

Stdlib-only, matching the constraint ``generate_rest_api_docs.py`` already
has, so the Node docs build can shell out to it without a Python virtualenv of
its own. It never imports ``caliber_sdk`` as an installed package — it reads
the resource modules' source text directly, which is what lets it run in the
docs build (no SDK install there) and in the backend test suite (SDK
installed) identically.

**What counts as "covered".** Calls through the sync resource layer
(``resources/*.py``) that name a literal path — an f-string with the request
method inlined -- via any of four call shapes: ``self._get/_post/_put/
_patch/_delete("literal")`` (the ``Resource`` base's per-verb helpers),
``self._transport.request("METHOD", "literal", ...)`` (the multipart-upload
shape, method and path as the first two positional arguments),
``self._transport.download("literal")`` (always a ``GET``), and
``self._transport.stream_lines("literal", ...)`` (also always a ``GET`` --
an SSE stream has no other verb). All four appear in this SDK today; missing
any of the latter three undercounts real coverage — exactly the failure mode
that let ``AuditAPI.export``, ``ProjectFilesAPI.upload``, and
``EventsAPI.stream`` each read as "uncovered" despite being implemented,
tested, and shipped since the coverage gate's first version.

Two things are deliberately excluded:

* ``resources/raw.py`` — its methods forward a caller-supplied ``path``
  variable, never a literal, so it produces no matches and needs no special
  case; named here so a reader does not have to rediscover that.
* ``aio/`` — the async client's narrower surface is a separate, documented
  decision (``test_async_parity.py`` pins it), not something this gate should
  paper over by counting a sync-only operation as "covered" twice.
"""

from __future__ import annotations

import re
from pathlib import Path

#: Matches ``self._get("/x")``, ``self._post(f"/x/{y}")``,
#: ``self._transport.get(...)`` — every verb the ``Resource`` base and the
#: transport itself expose, whether called directly or through ``self.raw``'s
#: pass-through (which never supplies a literal, so it never matches).
_CALL = re.compile(r"self\.(?:_transport\.)?(_?(?:get|post|put|patch|delete))\(\s*f?\"([^\"]+)\"")

#: ``self._transport.request("POST", f"/x/{y}", files=..., data=...)`` — the
#: multipart-upload shape. ``\s*`` matches the newline in the two-line form
#: every current call site uses (``self._transport.request(\n    "POST",
#: f"/path", ...\n)``), so this is not a line-break issue -- ``_CALL`` above
#: simply never looks for a method named as a *string argument* rather than
#: an attribute.
_REQUEST_CALL = re.compile(r"self\._transport\.request\(\s*\"([A-Z]+)\"\s*,\s*f?\"([^\"]+)\"")

#: ``self._transport.download(f"/x/{y}", ...)`` — always a GET; there is no
#: method argument to read.
_DOWNLOAD_CALL = re.compile(r"self\._transport\.download\(\s*f?\"([^\"]+)\"")

#: ``self._transport.stream_lines(f"/x/{y}", ...)`` — an SSE stream, always a
#: GET (there is no other verb an event source could use).
_STREAM_CALL = re.compile(r"self\._transport\.stream_lines\(\s*f?\"([^\"]+)\"")

#: Method-name alias -> canonical HTTP verb. The leading underscore is the
#: ``Resource`` base's private helper name; the bare form is ``Transport``'s
#: public one (used by a handful of resources that reach ``self._transport``
#: directly for a verb the base class does not wrap, e.g. streaming).
_METHOD_BY_ALIAS: dict[str, str] = {
    "_get": "GET",
    "_post": "POST",
    "_put": "PUT",
    "_patch": "PATCH",
    "_delete": "DELETE",
    "get": "GET",
    "post": "POST",
    "put": "PUT",
    "patch": "PATCH",
    "delete": "DELETE",
}

#: Any path-parameter segment, regardless of the name a caller's f-string
#: interpolated (``{skill_id}``) or the server's own converter syntax
#: (``{id:path}`` already normalized to ``{id}`` upstream). Collapsing both
#: to the same placeholder is what lets differently-named parameters at the
#: same position compare equal.
_PLACEHOLDER = re.compile(r"\{[^}]*\}")

#: Resource-layer files that must not contribute matches. ``raw.py`` needs no
#: entry (see module docstring); ``_base.py`` and ``__init__.py`` carry no
#: endpoint calls at all and are skipped as a matter of not walking dead
#: weight.
_SKIP_FILES = frozenset({"_base.py", "__init__.py"})


def normalize_path(path: str) -> str:
    """Collapse every path-parameter segment to ``{}``.

    ``/skills/{skill_id}`` and ``/skills/{id}`` must compare equal — the SDK
    method's f-string variable name and the server's path-parameter name are
    independent choices, and neither should matter to whether the operation
    is covered.
    """
    return _PLACEHOLDER.sub("{}", path)


def sdk_root_dir(repo_root: Path) -> Path:
    return repo_root / "sdk" / "caliber-sdk" / "src" / "caliber_sdk"


def sdk_resources_dir(repo_root: Path) -> Path:
    return sdk_root_dir(repo_root) / "resources"


def _covered_in_file(path: Path) -> set[tuple[str, str]]:
    covered: set[tuple[str, str]] = set()
    text = path.read_text(encoding="utf-8")
    for match in _CALL.finditer(text):
        method = _METHOD_BY_ALIAS.get(match.group(1))
        if method is None:
            continue
        covered.add((method, normalize_path(match.group(2))))
    for match in _REQUEST_CALL.finditer(text):
        covered.add((match.group(1), normalize_path(match.group(2))))
    for match in _DOWNLOAD_CALL.finditer(text):
        covered.add(("GET", normalize_path(match.group(1))))
    for match in _STREAM_CALL.finditer(text):
        covered.add(("GET", normalize_path(match.group(1))))
    return covered


def covered_operations(repo_root: Path) -> set[tuple[str, str]]:
    """``{(METHOD, normalized_path), ...}`` for every call the sync client
    surface makes with a literal path.

    Scans two places, both scored equally as "typed coverage":

    * ``resources/*.py`` — the typed façades (``client.prompts``, ...).
    * ``client.py`` — a handful of root-level convenience methods
      (``whoami()``, ``capabilities()``, ``openapi()``, ``health()``) call
      the transport directly rather than through a ``Resource`` subclass.
      They are just as real and just as typed; excluding them would make the
      gate demand a second, redundant resource method for an endpoint the
      SDK already reaches.

    Returns the empty set (never raises) when the SDK checkout is absent, so
    a caller that runs before it exists degrades to "nothing is covered"
    rather than crashing — the coverage gate then fails loudly on every
    operation instead of silently passing.
    """
    covered: set[tuple[str, str]] = set()
    resources_dir = sdk_resources_dir(repo_root)
    if resources_dir.is_dir():
        for path in sorted(resources_dir.glob("*.py")):
            if path.name in _SKIP_FILES:
                continue
            covered |= _covered_in_file(path)
    client_py = sdk_root_dir(repo_root) / "client.py"
    if client_py.is_file():
        covered |= _covered_in_file(client_py)
    return covered


def is_covered(method: str, path: str, covered: set[tuple[str, str]]) -> bool:
    return (method.upper(), normalize_path(path)) in covered


__all__ = [
    "covered_operations",
    "is_covered",
    "normalize_path",
    "sdk_resources_dir",
]
