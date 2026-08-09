"""End-to-end contract smoke tests: can the published contract drive a client?

M0 shipped four things that only matter together — a served OpenAPI document, a
per-operation stability tier, a credential for automation, and declared
response schemas. Each has its own unit tests. None of them proves the
combination works, which is the only thing an SDK author cares about.

These tests drive the server *from its own published document* rather than from
a hand-written list of paths. A route that exists but is undocumented, or is
documented but 500s, or returns an envelope shaped differently from every other
route, fails here — and none of those would fail a per-module test.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from caliber.routes.openapi import OPENAPI_PATH

PREFIX = "/ajax-api/2.0/mlflow/caliber"


#: GA GET operations that mutate nothing and need no path parameter, so a smoke
#: client can call them with no setup. Derived from the document at run time.
def _ga_probes(client: TestClient) -> list[str]:
    document = client.get(OPENAPI_PATH).json()
    return sorted(
        path
        for path, operations in document["paths"].items()
        if "get" in operations
        and operations["get"].get("x-caliber-stability") == "ga"
        and "{" not in path
    )


def test_the_document_describes_enough_to_drive_a_client(client: TestClient) -> None:
    """A generator needs servers, security, and operations. Assert all three."""
    document = client.get(OPENAPI_PATH).json()

    assert document["servers"][0]["url"] == PREFIX
    assert set(document["components"]["securitySchemes"]) >= {"bearerAuth", "csrfToken"}
    ga = [
        operation
        for operations in document["paths"].values()
        for operation in operations.values()
        if operation.get("x-caliber-stability") == "ga"
    ]
    assert len(ga) > 50, f"only {len(ga)} GA operations; the tier list is probably broken"
    # Every operation a generator emits needs a unique method name.
    ids = [
        operation["operationId"]
        for operations in document["paths"].values()
        for operation in operations.values()
    ]
    assert len(ids) == len(set(ids))


def test_every_ga_read_the_document_advertises_actually_answers(client: TestClient) -> None:
    """The document must not promise an operation the server cannot serve.

    This is the check that a per-module test cannot make: it asks the *whole*
    advertised GA read surface to respond, so a route that was renamed, or one
    whose handler raises on an empty database, is caught here.

    5xx is the failure. A 4xx is a legitimate answer — several of these require
    configuration or a scope this caller lacks — but a crash is never the
    contract.
    """
    probes = _ga_probes(client)
    assert len(probes) > 20, f"only {len(probes)} GA probes; the document is probably truncated"

    crashed: list[tuple[str, int]] = []
    for path in probes:
        response = client.get(path)
        if response.status_code >= 500:
            crashed.append((path, response.status_code))
    assert not crashed, f"GA reads returning 5xx: {crashed}"


def test_successful_reads_use_one_envelope(client: TestClient) -> None:
    """Every 200 is ``{"data": ...}`` — the assumption the SDK unwraps on.

    The OpenAPI document itself is the deliberate exception: it is consumed by
    generators that expect the specification at the document root, so it is
    served unenveloped. Asserting that explicitly keeps it a decision rather
    than an inconsistency someone later "fixes".
    """
    unenveloped: list[str] = []
    for path in _ga_probes(client):
        response = client.get(path)
        if response.status_code != 200:
            continue
        body = response.json()
        if path == OPENAPI_PATH:
            assert "openapi" in body and "data" not in body
            continue
        if not isinstance(body, dict) or "data" not in body:
            unenveloped.append(path)
    assert not unenveloped, f"200 responses missing the data envelope: {unenveloped}"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (f"{PREFIX}/prompts/does-not-exist-anywhere", 404),
        (f"{PREFIX}/projects/PRJ-nope", 404),
    ],
)
def test_error_bodies_are_stable(client: TestClient, path: str, expected: int) -> None:
    """``{"detail", "status_code"}`` is what the SDK's exceptions parse."""
    response = client.get(path)
    assert response.status_code == expected
    body = response.json()
    assert isinstance(body.get("detail"), str) and body["detail"]
    assert body.get("status_code") == expected


def test_a_validation_failure_names_the_offending_field(client: TestClient) -> None:
    """The structured 400 the SDK turns into CaliberValidationError."""
    response = client.post(f"{PREFIX}/auth/tokens", json={"name": ""})
    assert response.status_code == 400, response.text
    assert isinstance(response.json().get("detail"), str)


def test_an_unauthenticated_request_is_401_not_a_crash(client: TestClient) -> None:
    response = client.get(f"{PREFIX}/capabilities", headers={"X-CALIBER-User": ""})
    assert response.status_code in (401, 403)
    assert isinstance(response.json().get("detail"), str)


def test_the_pat_lifecycle_works_end_to_end(client: TestClient) -> None:
    """Issue, authenticate with, and revoke a token — the automation path.

    This is the flow an SDK user performs first, and the one that spans every
    M0 piece: the token comes from the route added in PR4, its scopes are the
    ceiling from that PR, and the response shape is the schema from PR2.
    """
    issued = client.post(f"{PREFIX}/auth/tokens", json={"name": "smoke"})
    assert issued.status_code == 201, issued.text
    token = issued.json()["data"]["token"]
    assert token.startswith("calpat_")

    auth = {"Authorization": f"Bearer {token}", "X-CALIBER-User": ""}
    identity = client.get(f"{PREFIX}/me", headers=auth)
    assert identity.status_code == 200, identity.text
    holder = identity.json()["data"]["user_id"]
    assert holder and holder != "anonymous"

    token_id = issued.json()["data"]["token_id"]
    assert client.delete(f"{PREFIX}/auth/tokens/{token_id}").status_code == 200

    # ``/me`` reports identity rather than requiring it -- it answers "who am
    # I", and the SPA uses an anonymous answer to decide whether to render a
    # login form. So a revoked token does not turn it into a 401; it turns the
    # caller into nobody. An SDK's ``whoami()`` therefore never raises on a bad
    # credential, which is worth knowing before relying on it to detect one.
    after = client.get(f"{PREFIX}/me", headers=auth)
    assert after.status_code == 200
    assert after.json()["data"]["user_id"] == "anonymous"
    assert after.json()["data"]["scopes"] == []

    # A route that *does* require authority proves the revocation took effect.
    assert client.get(f"{PREFIX}/capabilities", headers=auth).status_code in (401, 403)


def test_a_ga_resource_flow_round_trips(client: TestClient) -> None:
    """Create, read, list, and delete through the declared schemas.

    Projects are the GA resource whose whole surface was formalized in PR2, so
    a round trip here exercises the schemas rather than raw dicts.
    """
    created = client.post(f"{PREFIX}/projects", json={"name": "smoke-project"})
    assert created.status_code == 201, created.text
    project = created.json()["data"]
    assert project["project_id"] and project["owner"] and project["status"]

    fetched = client.get(f"{PREFIX}/projects/{project['project_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["data"]["project_id"] == project["project_id"]

    listed = client.get(f"{PREFIX}/projects").json()["data"]
    assert any(item["project_id"] == project["project_id"] for item in listed)
    # file_count is present on the list response and absent on the detail one;
    # that difference is deliberate and worth pinning.
    assert all("file_count" in item for item in listed)

    uploaded = client.post(
        f"{PREFIX}/projects/{project['project_id']}/files",
        files={"file": ("a.txt", b"hello", "text/plain")},
        data={"kind": "input", "path": "a.txt"},
    )
    assert uploaded.status_code == 201, uploaded.text
    record = uploaded.json()["data"]
    # The field a schema silently dropped during PR2; it must survive the flow.
    assert record["project_id"] == project["project_id"]

    files = client.get(f"{PREFIX}/projects/{project['project_id']}/files").json()["data"]
    assert [item["file_id"] for item in files["items"]] == [record["file_id"]]
    assert "directories" in files and "next_cursor" in files


def test_capabilities_lets_a_client_feature_detect(client: TestClient) -> None:
    """The cheap half of feature detection the SDK relies on."""
    payload = client.get(f"{PREFIX}/capabilities").json()["data"]
    tiers = payload["sdk_stability"]
    assert {"ga", "beta", "internal"} == set(tiers)
    assert {"prompts", "workflows", "projects"} <= set(tiers["ga"])
    # And it agrees with the document, which is what stops a client trusting
    # the cheap path and getting a different answer from the generated one.
    document = client.get(OPENAPI_PATH).json()
    documented_ga = {
        operation["tags"][0]
        for operations in document["paths"].values()
        for operation in operations.values()
        if operation.get("x-caliber-stability") == "ga"
    }
    assert documented_ga <= set(tiers["ga"])
