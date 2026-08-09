"""Contracts for the served management OpenAPI document.

The document's value rests entirely on one property: it describes the server
that is actually running. A specification that drifts from the routes is worse
than none, because clients trust it. These tests pin that property directly --
the document is compared against the live route table, not against a fixture.
"""

from __future__ import annotations

import re

import pytest
from starlette.routing import Route
from starlette.testclient import TestClient

from caliber.routes.openapi import PREFIX, build_openapi_document

OPENAPI_URL = PREFIX + "/openapi.json"


def _live_routes(app) -> set[tuple[str, str]]:
    """(normalized path, lowercase method) for every management route."""
    live: set[tuple[str, str]] = set()
    for route in app.routes:
        if not isinstance(route, Route) or not route.path.startswith(PREFIX):
            continue
        normalized = re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*):[a-zA-Z]+\}", r"{\1}", route.path)
        for method in (route.methods or set()) - {"HEAD", "OPTIONS"}:
            live.add((normalized, method.lower()))
    return live


def test_the_document_is_served_and_well_formed(client: TestClient) -> None:
    response = client.get(OPENAPI_URL)
    assert response.status_code == 200, response.text
    doc = response.json()

    # Not enveloped: an OpenAPI document is consumed by generators and tooling
    # that expect the specification at the document root.
    assert doc["openapi"] == "3.0.3"
    assert doc["info"]["title"] == "CALIBER Management API"
    assert doc["info"]["version"]
    assert doc["servers"][0]["url"] == PREFIX
    assert doc["paths"], "the document described no paths"


def test_the_document_matches_the_live_route_table_exactly(client: TestClient) -> None:
    """The property the whole module exists for.

    Compared as a set in both directions: a route missing from the document is
    an SDK that cannot call it, and a documented operation with no route is an
    SDK that generates a method which 404s.
    """
    doc = client.get(OPENAPI_URL).json()
    documented = {
        (path, method) for path, operations in doc["paths"].items() for method in operations
    }
    live = _live_routes(client.app)

    assert live, "parsed no live routes; the comparison would be vacuous"
    assert documented == live, (
        f"missing from document: {sorted(live - documented)}\n"
        f"documented but not routed: {sorted(documented - live)}"
    )


def test_the_openapi_route_describes_itself() -> None:
    """A document that omits its own endpoint is describing a different server."""
    from caliber.server import create_app

    app = create_app()
    doc = build_openapi_document(app)
    assert "/openapi.json" in "".join(doc["paths"]), "the document does not list itself"
    assert "get" in doc["paths"][OPENAPI_URL]


def test_starlette_converters_are_translated_to_openapi_parameters(client: TestClient) -> None:
    """``{path:path}`` is Starlette syntax, not OpenAPI syntax."""
    doc = client.get(OPENAPI_URL).json()

    assert not any(":" in path for path in doc["paths"]), (
        "a Starlette converter leaked into a documented path: "
        f"{[p for p in doc['paths'] if ':' in p]}"
    )

    for path, operations in doc["paths"].items():
        names = set(re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", path))
        if not names:
            continue
        for method, operation in operations.items():
            declared = {p["name"] for p in operation.get("parameters", [])}
            assert declared == names, (
                f"{method.upper()} {path} declares {sorted(declared)} "
                f"but its template needs {sorted(names)}"
            )
            for parameter in operation.get("parameters", []):
                assert parameter["in"] == "path"
                assert parameter["required"] is True
                assert parameter["schema"].get("type")


def test_operation_ids_are_unique(client: TestClient) -> None:
    """Generators derive method names from operationId; duplicates collide."""
    doc = client.get(OPENAPI_URL).json()
    ids = [
        operation["operationId"]
        for operations in doc["paths"].values()
        for operation in operations.values()
    ]
    duplicates = {value for value in ids if ids.count(value) > 1}
    assert not duplicates, f"duplicate operationIds: {sorted(duplicates)}"


def test_auth_and_scoping_headers_are_part_of_the_contract(client: TestClient) -> None:
    """CSRF and project scoping are the two things an SDK gets wrong first."""
    schemes = client.get(OPENAPI_URL).json()["components"]["securitySchemes"]
    assert schemes["bearerAuth"] == {"type": "http", "scheme": "bearer"}
    assert schemes["csrfToken"]["name"] == "X-CALIBER-CSRF"
    assert schemes["projectScope"]["name"] == "X-CALIBER-Project"


def test_the_envelope_and_error_shapes_are_documented(client: TestClient) -> None:
    schemas = client.get(OPENAPI_URL).json()["components"]["schemas"]
    assert schemas["Envelope"]["required"] == ["data"]
    assert set(schemas["Error"]["required"]) == {"detail", "status_code"}
    assert "errors" in schemas["ValidationError"]["properties"]


def test_unmodelled_bodies_are_declared_rather_than_implied(client: TestClient) -> None:
    """Honesty about coverage.

    The document describes routes, not payloads. Saying so in the document is
    what stops a generator from emitting clients that send empty bodies to
    endpoints which require one.
    """
    doc = client.get(OPENAPI_URL).json()
    coverage = doc["x-caliber-schema-coverage"]
    assert coverage["paths"] == "complete"
    assert coverage["request_bodies"] == "not-yet-modelled"

    writes = [
        operation
        for operations in doc["paths"].values()
        for method, operation in operations.items()
        if method != "get"
    ]
    assert writes, "no mutating operations found; the assertion below would be vacuous"
    assert all(op.get("x-caliber-request-body") == "not-yet-modelled" for op in writes)


def test_the_document_requires_authentication(client: TestClient) -> None:
    """It enumerates the entire management surface, so it is not public.

    The explicit empty ``X-CALIBER-User`` is how this suite expresses "no
    identity" -- the ``client`` fixture sends an admin header by default.
    """
    response = client.get(OPENAPI_URL, headers={"X-CALIBER-User": ""})
    assert response.status_code in (401, 403), response.text


def test_every_ref_resolves_within_the_document(client: TestClient) -> None:
    """A dangling ``$ref`` breaks every code generator that reads the document.

    Shared responses are declared once under ``components.responses`` and
    referenced per operation, so this is the check that keeps that indirection
    honest.
    """
    doc = client.get(OPENAPI_URL).json()

    refs: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "$ref" and isinstance(value, str):
                    refs.add(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(doc)
    assert refs, "no $refs found; the assertion below would be vacuous"

    unresolved = []
    for ref in sorted(refs):
        assert ref.startswith("#/"), f"non-local $ref: {ref}"
        cursor: object = doc
        for part in ref[2:].split("/"):
            cursor = cursor.get(part) if isinstance(cursor, dict) else None
            if cursor is None:
                unresolved.append(ref)
                break
    assert not unresolved, f"unresolved $refs: {unresolved}"


def test_shared_responses_are_referenced_not_inlined(client: TestClient) -> None:
    """Guards the size regression that motivated components.responses.

    Inlining the same five response blocks across 355 operations produced a
    348 KB document, most of it identical bytes.
    """
    doc = client.get(OPENAPI_URL).json()
    assert set(doc["components"]["responses"]) >= {"Success", "Unauthenticated", "NotFound"}
    for path, operations in doc["paths"].items():
        for method, operation in operations.items():
            for status, response in operation["responses"].items():
                assert "$ref" in response, f"{method.upper()} {path} inlines its {status} response"


def test_every_tag_has_a_declared_stability_tier(client: TestClient) -> None:
    """No operation may ship without a tier.

    ``stability_for`` fails closed to ``internal`` so an unclassified route is
    never silently promised as GA, but defaulting is not the same as deciding.
    This is what forces a new route group to be classified deliberately.
    """
    from caliber.routes.openapi import _STABILITY

    doc = client.get(OPENAPI_URL).json()
    tags = {tag for ops in doc["paths"].values() for op in ops.values() for tag in op["tags"]}
    undeclared = sorted(tags - set(_STABILITY))
    assert not undeclared, (
        f"tags with no declared stability tier: {undeclared}. "
        "Add each to _STABILITY in caliber/routes/openapi.py."
    )


def test_stale_tiers_do_not_linger(client: TestClient) -> None:
    """A tier for a tag that no longer exists is a stale promise."""
    from caliber.routes.openapi import _STABILITY

    doc = client.get(OPENAPI_URL).json()
    tags = {tag for ops in doc["paths"].values() for op in ops.values() for tag in op["tags"]}
    orphaned = sorted(set(_STABILITY) - tags)
    assert not orphaned, f"_STABILITY names tags the API no longer serves: {orphaned}"


def test_every_operation_carries_its_tier(client: TestClient) -> None:
    doc = client.get(OPENAPI_URL).json()
    for path, operations in doc["paths"].items():
        for method, operation in operations.items():
            tier = operation.get("x-caliber-stability")
            assert tier in {"ga", "beta", "internal"}, f"{method.upper()} {path} -> {tier!r}"


def test_capabilities_and_openapi_agree_on_stability(client: TestClient) -> None:
    """The checklist requires these two surfaces to be aligned.

    An SDK reads /capabilities to feature-detect cheaply; if it disagreed with
    the document the SDK generates from, the cheap path would be the wrong one.
    """
    doc = client.get(OPENAPI_URL).json()
    capabilities = client.get(PREFIX + "/capabilities").json()["data"]
    summary = capabilities["sdk_stability"]

    assert set(summary) == {"ga", "beta", "internal"}
    from_doc: dict[str, set[str]] = {"ga": set(), "beta": set(), "internal": set()}
    for operations in doc["paths"].values():
        for operation in operations.values():
            from_doc[operation["x-caliber-stability"]].add(operation["tags"][0])

    for tier, tags in from_doc.items():
        assert tags <= set(summary[tier]), (
            f"{tier}: document tags {sorted(tags - set(summary[tier]))} missing from /capabilities"
        )


def test_the_ga_tier_covers_the_core_management_surface(client: TestClient) -> None:
    """Pins the promise. Demoting one of these is a breaking SDK change."""
    ga = set(client.get(PREFIX + "/capabilities").json()["data"]["sdk_stability"]["ga"])
    assert {"prompts", "skills", "tools", "agents", "workflows", "services", "projects"} <= ga


def test_no_settings_path_anywhere_points_at_a_missing_spa_route(client: TestClient) -> None:
    """Generalises the Cookbook fix to every surface that emits a deep link.

    The cookbook readiness check shipped ``/settings/runtime`` -- an API path,
    not a SPA route -- and its test pinned the broken value. ``/capabilities``
    carried the identical defect, latent only because nothing renders it yet.
    Checking the whole API surface is what stops a third one.
    """
    import json
    import re
    from pathlib import Path

    app_tsx = Path(__file__).resolve().parents[2] / "caliber" / "caliber-ui" / "src" / "App.tsx"
    registered = set(re.findall(r'path="([^"]+)"', app_tsx.read_text(encoding="utf-8")))
    assert registered, "parsed no SPA routes; the assertion below would be vacuous"

    advertised: set[str] = set()
    for url in (PREFIX + "/capabilities", PREFIX + "/cookbooks"):
        found = re.findall(r'"settings_path":\s*"([^"]+)"', json.dumps(client.get(url).json()))
        advertised.update(found)

    assert advertised, "no settings_path advertised; the assertion below would be vacuous"
    assert advertised <= registered, (
        f"settings_path values naming routes the SPA does not register: "
        f"{sorted(advertised - registered)}"
    )


@pytest.mark.parametrize("tagged", ["prompts", "workflows", "cookbooks"])
def test_operations_are_tagged_by_url_segment(client: TestClient, tagged: str) -> None:
    """Tags drive a generated client's module layout, so they must be present."""
    doc = client.get(OPENAPI_URL).json()
    tags = {tag for ops in doc["paths"].values() for op in ops.values() for tag in op["tags"]}
    assert tagged in tags
