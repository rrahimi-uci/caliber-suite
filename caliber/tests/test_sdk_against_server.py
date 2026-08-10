"""The SDK and the server actually agree.

Every other SDK test mocks the transport, which proves the client is
self-consistent and nothing more. This drives the real ``caliber_sdk`` against
the real CALIBER application over an in-process ASGI transport, so a
disagreement between the two — a renamed path, a changed envelope, a field the
SDK decodes from the wrong key — fails here and nowhere else.

Skipped when ``caliber-sdk`` is not installed, so a backend-only checkout does
not depend on the SDK package being present.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

caliber_sdk = pytest.importorskip("caliber_sdk", reason="caliber-sdk is not installed")


@pytest.fixture
def sdk(client: TestClient) -> object:
    """A CaliberClient driving the test application.

    ``TestClient`` is itself an ``httpx.Client`` with a transport that runs the
    ASGI app in-process, so the SDK can use it directly. ``httpx.ASGITransport``
    would be the obvious choice and is the wrong one: it implements
    ``handle_async_request`` only, so a synchronous client cannot drive it.

    The fixture app runs in trusted-header mode, which is how this client
    presents an identity; a real deployment would use a token, and
    :func:`test_a_token_issued_by_the_sdk_authenticates_the_sdk` covers that.
    """
    return caliber_sdk.CaliberClient("http://testserver", http_client=client)


def test_the_sdk_reads_identity_from_the_real_server(sdk: object) -> None:
    identity = sdk.me.get()  # type: ignore[attr-defined]
    assert identity.user_id
    assert not identity.is_anonymous


def test_the_sdk_reads_capabilities_and_stability_tiers(sdk: object) -> None:
    """Proves the nested decoding matches what the server actually sends."""
    capabilities = sdk.capabilities_api.get()  # type: ignore[attr-defined]
    assert capabilities.sdk_stability
    assert capabilities.is_ga("prompts")
    assert capabilities.tier_of("assistant") == "internal"
    # Decoded from the nested block, not the top level.
    assert isinstance(capabilities.workflow_runs.queue_enabled, bool)


def test_the_sdk_drives_the_full_token_lifecycle(sdk: object) -> None:
    """Issue, list, rotate, revoke — against the real routes.

    The flow that spans the most of M0, and the first thing a real SDK user
    does. A mocked version of this would prove nothing about the server.
    """
    issued = sdk.auth.tokens.create("sdk-integration", scopes=["caliber.operator"])  # type: ignore[attr-defined]
    assert issued.token.startswith("calpat_")
    assert issued.token_id

    listed = sdk.auth.tokens.list()  # type: ignore[attr-defined]
    assert issued.token_id in [item.token_id for item in listed]
    # The listed form carries no secret at all, not a null one.
    assert not hasattr(listed[0], "token")

    rotated = sdk.auth.tokens.rotate(issued.token_id)  # type: ignore[attr-defined]
    assert rotated.token != issued.token
    assert rotated.rotated_from == issued.token_id

    assert sdk.auth.tokens.revoke(rotated.token_id) is True  # type: ignore[attr-defined]


def test_a_token_issued_by_the_sdk_authenticates_the_sdk(client: TestClient) -> None:
    """The credential round trip, end to end.

    Issue a token through the SDK, then build a second client that presents
    only that token. If the SDK's auth header or the server's PAT resolution
    disagreed, this is where it would show.
    """
    admin = caliber_sdk.CaliberClient("http://testserver", http_client=client)
    issued = admin.auth.tokens.create("bearer-only")

    # A second client presenting the token and no identity header, so the token
    # is the only credential in play.
    with TestClient(client.app, headers={"X-CALIBER-User": ""}) as anonymous:
        as_token = caliber_sdk.CaliberClient(
            "http://testserver", token=issued.token, http_client=anonymous
        )
        identity = as_token.me.get()

    assert not identity.is_anonymous
    assert identity.user_id == admin.me.get().user_id


def test_the_sdk_round_trips_a_project_and_a_file(sdk: object) -> None:
    """A GA resource flow through the schemas formalized in M0-PR2."""
    project = sdk.projects.create("sdk-integration-project")  # type: ignore[attr-defined]
    assert project.project_id and project.owner

    fetched = sdk.projects.get(project.project_id)  # type: ignore[attr-defined]
    assert fetched.project_id == project.project_id

    record = sdk.projects.files.upload(  # type: ignore[attr-defined]
        project.project_id, filename="a.txt", content=b"hello", path="a.txt"
    )
    # The field a schema silently dropped during M0-PR2; the SDK must see it.
    assert record.project_id == project.project_id
    assert record.file_id

    files, folders = sdk.projects.files.list(project.project_id)  # type: ignore[attr-defined]
    assert [item.file_id for item in files] == [record.file_id]
    assert folders == []

    assert sdk.projects.files.download(project.project_id, record.file_id) == b"hello"  # type: ignore[attr-defined]


def test_the_sdk_lists_the_asset_families_from_the_real_server(sdk: object) -> None:
    """Prompts, skills, and tools decode against the live routes.

    Empty registries are the honest case for a fresh fixture database; what is
    being proved is that the paths resolve and the payloads decode, not that
    seed data exists.
    """
    assert isinstance(sdk.prompts.list(), list)  # type: ignore[attr-defined]
    assert isinstance(sdk.skills.list(), list)  # type: ignore[attr-defined]
    assert isinstance(sdk.tools.list(), list)  # type: ignore[attr-defined]


def test_the_sdk_round_trips_a_skill(sdk: object) -> None:
    """Create, render, and version a skill through the typed surface."""
    skill = sdk.skills.create(  # type: ignore[attr-defined]
        "sdk-integration-skill",
        content="Handle {{topic}} requests carefully.",
        owner="@sdk-tests",
        summary="Integration fixture",
    )
    assert skill.skill_id and skill.name == "sdk-integration-skill"

    rendered = sdk.skills.render(skill.skill_id, variables={"topic": "refund"})  # type: ignore[attr-defined]
    assert "refund" in rendered.rendered_content
    assert rendered.unresolved_variables == []

    unresolved = sdk.skills.render(skill.skill_id, variables={})  # type: ignore[attr-defined]
    assert unresolved.unresolved_variables == ["topic"]

    versions = sdk.skills.versions(skill.skill_id)  # type: ignore[attr-defined]
    assert all(v.skill_id == skill.skill_id for v in versions)


def test_the_sdk_round_trips_a_workflow_and_a_dataset(sdk: object) -> None:
    """Workflow, dataset, and judge creation through the typed surfaces."""
    workflow = sdk.workflows.create("sdk-integration-workflow")  # type: ignore[attr-defined]
    assert workflow.workflow_id and workflow.name == "sdk-integration-workflow"
    assert sdk.workflows.get(workflow.workflow_id).workflow_id == workflow.workflow_id  # type: ignore[attr-defined]
    assert isinstance(sdk.workflows.versions.list(workflow.workflow_id), list)  # type: ignore[attr-defined]

    dataset = sdk.datasets.create(  # type: ignore[attr-defined]
        "sdk-integration-dataset", owner="@sdk-tests"
    )
    assert dataset.dataset_id
    # Never synced to MLflow in a fixture database, and the property must say
    # so rather than defaulting to True.
    assert not dataset.is_synced

    judge = sdk.judges.create(  # type: ignore[attr-defined]
        "sdk-integration-judge", instructions="Given {{ inputs }} and {{ outputs }}, return true if valid JSON."
    )
    assert judge.judge_id
    assert judge.feedback_value_type == "bool"


def test_the_sdk_lists_every_ga_surface_it_models(sdk: object) -> None:
    """A smoke pass over the whole GA tree, against real routes.

    Cheap, and it catches the failure a per-module test cannot: a path that
    was renamed on the server while the SDK kept the old one.
    """
    for call in (
        sdk.workflows.list,  # type: ignore[attr-defined]
        sdk.datasets.list,  # type: ignore[attr-defined]
        sdk.judges.list,  # type: ignore[attr-defined]
        sdk.evaluations.list,  # type: ignore[attr-defined]
        sdk.projects.list,  # type: ignore[attr-defined]
        sdk.auth.tokens.list,  # type: ignore[attr-defined]
    ):
        assert isinstance(call(), list)


def test_the_sdk_raises_typed_errors_from_real_failures(sdk: object) -> None:
    """The exception mapping is only meaningful against real status codes."""
    with pytest.raises(caliber_sdk.CaliberNotFoundError) as caught:
        sdk.projects.get("PRJ-does-not-exist")  # type: ignore[attr-defined]
    assert caught.value.status_code == 404
    assert caught.value.detail


def test_the_sdk_can_read_the_served_openapi_document(sdk: object) -> None:
    """The document is unenveloped; the SDK must not try to unwrap it."""
    document = sdk.openapi()  # type: ignore[attr-defined]
    assert document["openapi"] == "3.0.3"
    assert document["paths"]


# --- beta surfaces --------------------------------------------------------
#
# M3 wires thirteen beta route groups into the client. Their paths were read
# from the served OpenAPI document, which is the right source but not proof: a
# path can exist and still be the wrong method, or return a shape the SDK
# decodes from the wrong key. These drive them.


def test_the_sdk_lists_every_beta_surface_it_models(sdk: object) -> None:
    """A smoke pass over the beta tree against real routes.

    Empty results are the honest answer for a fresh fixture database. What is
    proved is that each path resolves with the method the SDK uses and decodes
    without raising — the failure mode that put ``GET /workflow-runs`` and
    ``GET /services`` in this suite's history.
    """
    for call in (
        sdk.jobs.list,  # type: ignore[attr-defined]
        sdk.review_queues.list,  # type: ignore[attr-defined]
        sdk.mcp_servers.list,  # type: ignore[attr-defined]
        sdk.knowledge_bases.list,  # type: ignore[attr-defined]
        sdk.aria.plans,  # type: ignore[attr-defined]
        sdk.releases.candidates,  # type: ignore[attr-defined]
        sdk.audit.list,  # type: ignore[attr-defined]
        sdk.cookbooks.list,  # type: ignore[attr-defined]
    ):
        assert isinstance(call(), list)


def test_the_sdk_reads_cookbook_readiness_from_the_real_catalog(sdk: object) -> None:
    """The catalog is seeded, so this asserts on content rather than shape.

    Readiness is computed server-side from the live environment, so a recipe
    that is not ready must name what is unmet — that list is the whole value of
    the field, and a client that dropped it would leave a caller with a badge
    and no cause.
    """
    recipes = sdk.cookbooks.list()  # type: ignore[attr-defined]
    assert recipes, "the cookbook catalog ships with the product"
    assert all(recipe.id and recipe.title for recipe in recipes)

    for recipe in recipes:
        if not recipe.is_ready:
            assert recipe.unmet_checks, f"{recipe.id} is not ready but names no unmet check"
            assert all(check.get("label") for check in recipe.unmet_checks)


def test_the_secret_store_fails_closed_rather_than_serving_plaintext(sdk: object) -> None:
    """Two acceptable answers, and neither one is a secret value.

    The fixture app has no ``CALIBER_SECRET_ENCRYPTION_KEY_SOURCE``, so the
    store refuses to serve at all — 503 with an actionable message. That is the
    property worth pinning: an unconfigured store must not degrade into
    unencrypted storage. Where a key *is* configured the listing must still
    carry only names and metadata, so both branches are asserted here and the
    test does not depend on which environment it runs in.

    Checked against the raw payload rather than the decoded model, because a
    decoder that silently dropped ``value`` would hide a real leak.
    """
    try:
        payload = sdk.raw.get("/secrets")  # type: ignore[attr-defined]
    except caliber_sdk.CaliberAPIError as exc:
        assert exc.status_code == 503
        assert "CALIBER_SECRET_ENCRYPTION_KEY_SOURCE" in (exc.detail or "")
        return

    entries = payload.get("secrets") if isinstance(payload, dict) else payload
    for entry in entries or []:
        assert "value" not in entry, "a secret value reached the client"


def test_the_sdk_reads_a_trace_listing_and_an_audit_page(sdk: object) -> None:
    """Observability and audit are read-only surfaces, so listing is the flow."""
    assert isinstance(sdk.observability.traces(), list)  # type: ignore[attr-defined]
    entries = sdk.audit.list(limit=5)  # type: ignore[attr-defined]
    assert len(entries) <= 5
