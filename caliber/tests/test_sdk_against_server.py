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

from tests.workflow_helpers import make_manifest

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
    capabilities = sdk.capabilities_info.get()  # type: ignore[attr-defined]
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

    dataset = sdk.eval_datasets.create(  # type: ignore[attr-defined]
        "sdk-integration-dataset", owner="@sdk-tests"
    )
    assert dataset.dataset_id
    # Never synced to MLflow in a fixture database, and the property must say
    # so rather than defaulting to True.
    assert not dataset.is_synced

    judge = sdk.judges.create(  # type: ignore[attr-defined]
        "sdk-integration-judge",
        instructions="Given {{ inputs }} and {{ outputs }}, return true if valid JSON.",
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
        sdk.eval_datasets.list,  # type: ignore[attr-defined]
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


def test_the_sdk_reads_the_optimizer_registry_from_the_real_server(sdk: object) -> None:
    """The registry the server actually dispatches on, decoded by the client.

    Worth driving end to end because the block nests two levels and the SDK
    decodes it by hand — a wrong key would produce an empty list rather than an
    error, which is the failure mode a mocked test happily reproduces.
    """
    extensibility = sdk.capabilities_info.get().extensibility  # type: ignore[attr-defined]

    metaprompt = extensibility.optimizer("MetaPrompt")
    assert metaprompt is not None
    assert not metaprompt.is_third_party
    assert metaprompt.can_target("prompt")
    # The scoping that keeps a skill job off a prompt-only optimizer.
    assert not metaprompt.can_target("skill")

    skill_capable = {item.name for item in extensibility.optimizers_for("skill")}
    assert {"SkillMetaPrompt", "GEPA"} <= skill_capable
    assert "MetaPrompt" not in skill_capable

    # Nothing is allowlisted in a test deployment, and that is the secure
    # default rather than a missing feature.
    assert extensibility.plugins == []
    assert extensibility.allowlist_env_var == "CALIBER_PLUGIN_ALLOWLIST"


# --- governance lifecycle (Phase 3 wave 3a/3b, Phase 5.3 CLI verbs) --------
#
# The single biggest surface this plan closed was the release/rollback path
# (wave 3a's rationale: "the release/rollback path is the product's core
# claim and is currently unreachable"). A mocked test proves each method
# sends the right request; only a real deployment/checkpoint stack proves the
# *sequence* -- promote, promote again, roll back -- actually round-trips.


def test_the_sdk_drives_a_workflow_version_through_validate_compile_publish(
    sdk: object,
) -> None:
    """A draft version is not runnable; publish is the step that changes that.

    Exercises all three pre-publish checks the SDK exposes as separate calls
    (``validate``, ``compile``, ``publish``) rather than only the one publish
    needs internally -- each is real SDK surface a caller can reach for
    independently (e.g. a CI job that wants to catch a bad manifest before
    ever publishing it).
    """
    workflow = sdk.workflows.create("sdk-e2e-governance-workflow")  # type: ignore[attr-defined]
    manifest = make_manifest(workflow.workflow_id)

    version = sdk.workflows.versions.create(workflow.workflow_id, manifest)  # type: ignore[attr-defined]
    assert version.workflow_id == workflow.workflow_id
    assert version.status == "draft"

    validation = sdk.workflows.versions.validate(version.version_id)  # type: ignore[attr-defined]
    assert isinstance(validation, dict)

    compiled = sdk.workflows.versions.compile(version.version_id)  # type: ignore[attr-defined]
    assert isinstance(compiled, dict)

    published = sdk.workflows.versions.publish(version.version_id)  # type: ignore[attr-defined]
    assert published.status == "published"
    # Idempotent: publishing an already-published version must not raise.
    assert sdk.workflows.versions.publish(version.version_id).status == "published"  # type: ignore[attr-defined]


def test_the_sdk_drives_deployment_promote_and_rollback_end_to_end(sdk: object) -> None:
    """Promote, promote again, roll back -- the exact sequence
    ``caliberctl workflow promote``/``rollback`` (Phase 5.3) wraps.

    The first promotion on a fresh alias has nothing to checkpoint (the
    server's own rollback-checkpoint stack starts empty); a rollback attempt
    at that point must fail. The *second* promotion is what pushes the first
    version onto the checkpoint stack, and that is what rollback then pops --
    so this drives two versions through two promotions specifically to prove
    the checkpoint stack, not just one call each of promote/rollback in
    isolation.
    """
    workflow = sdk.workflows.create("sdk-e2e-deployment-workflow")  # type: ignore[attr-defined]
    v1 = sdk.workflows.versions.publish(  # type: ignore[attr-defined]
        sdk.workflows.versions.create(  # type: ignore[attr-defined]
            workflow.workflow_id, make_manifest(workflow.workflow_id)
        ).version_id
    )
    v2 = sdk.workflows.versions.publish(  # type: ignore[attr-defined]
        sdk.workflows.versions.create(  # type: ignore[attr-defined]
            workflow.workflow_id, make_manifest(workflow.workflow_id)
        ).version_id
    )

    # Ungated alias: rotation is immediate, no pending promotion created.
    first = sdk.workflows.promote_deployment(  # type: ignore[attr-defined]
        workflow.workflow_id, "staging", version_id=v1.version_id
    )
    assert isinstance(first, dict)

    assert _deployed_version(sdk, workflow.workflow_id, "staging") == v1.version_id

    with pytest.raises(caliber_sdk.CaliberNotFoundError) as no_checkpoint:
        sdk.workflows.rollback_deployment(workflow.workflow_id, "staging")  # type: ignore[attr-defined]
    assert "checkpoint" in (no_checkpoint.value.detail or "").lower()

    sdk.workflows.promote_deployment(  # type: ignore[attr-defined]
        workflow.workflow_id, "staging", version_id=v2.version_id
    )
    assert _deployed_version(sdk, workflow.workflow_id, "staging") == v2.version_id

    sdk.workflows.rollback_deployment(workflow.workflow_id, "staging")  # type: ignore[attr-defined]
    assert _deployed_version(sdk, workflow.workflow_id, "staging") == v1.version_id


def _deployed_version(sdk: object, workflow_id: str, alias: str) -> str | None:
    """``deployments()`` returns a list of ``{"alias", "version_id", ...}``
    rows, one per active alias -- not a dict keyed by alias."""
    deployments = sdk.workflows.deployments(workflow_id)  # type: ignore[attr-defined]
    for row in deployments:
        if row.get("alias") == alias:
            return row.get("version_id")
    return None


def test_the_sdk_records_and_reads_a_gate_verdict_for_a_real_version(sdk: object) -> None:
    """Advisory evidence attached to a version that actually exists.

    ``get`` before any verdict is recorded must answer ``{"state": "none"}``
    rather than 404 -- a version with no verdict yet is a normal state, not a
    missing resource -- so this checks that shape too, not only the
    round-trip after recording one.
    """
    workflow = sdk.workflows.create("sdk-e2e-gate-verdict-workflow")  # type: ignore[attr-defined]
    version = sdk.workflows.versions.create(  # type: ignore[attr-defined]
        workflow.workflow_id, make_manifest(workflow.workflow_id)
    )

    before = sdk.gate_verdicts.get("workflow", version.version_id)  # type: ignore[attr-defined]
    assert before.get("state") == "none"

    recorded = sdk.gate_verdicts.record(  # type: ignore[attr-defined]
        "workflow", version.version_id, state="pass", score=0.95
    )
    assert recorded.get("state") == "pass"

    after = sdk.gate_verdicts.get("workflow", version.version_id)  # type: ignore[attr-defined]
    assert after.get("state") == "pass"


def test_the_sdk_submits_a_run_and_lists_it_scoped_to_its_workflow(
    sdk: object, client: TestClient
) -> None:
    """Submission plus the scoped listing that surfaces it.

    ``WorkflowRunsAPI.list`` is scoped to a workflow because the server has no
    unscoped listing (``POST /workflow-runs`` is submission-only) -- this
    proves the submitted run actually shows up there, not only that
    submission itself returns 2xx.
    """
    # The fixture app disables the run queue by default (a 409 explains why,
    # rather than submission just hanging); this test is specifically about
    # submission and scoped listing, not the queue feature itself.
    client.app.state.config = client.app.state.config.model_copy(
        update={"workflow_run_queue_enabled": True}
    )
    workflow_id, version_id = _publish_workflow(sdk, "sdk-e2e-run-workflow")

    run = sdk.workflows.runs.submit(  # type: ignore[attr-defined]
        workflow_id=workflow_id, workflow_version_id=version_id, input="hello"
    )
    assert run.workflow_run_id
    assert run.status

    fetched = sdk.workflows.runs.get(run.workflow_run_id)  # type: ignore[attr-defined]
    assert fetched.workflow_run_id == run.workflow_run_id

    listed = sdk.workflows.runs.list(workflow_id)  # type: ignore[attr-defined]
    assert run.workflow_run_id in [item.workflow_run_id for item in listed]


def _publish_workflow(sdk: object, name: str) -> tuple[str, str]:
    """Shared arrange-step: a workflow with one published version."""
    workflow = sdk.workflows.create(name)  # type: ignore[attr-defined]
    version = sdk.workflows.versions.publish(  # type: ignore[attr-defined]
        sdk.workflows.versions.create(  # type: ignore[attr-defined]
            workflow.workflow_id, make_manifest(workflow.workflow_id)
        ).version_id
    )
    return workflow.workflow_id, version.version_id


# --- agents (Phase 3 wave 3a/3b) -------------------------------------------


def test_the_sdk_drives_an_agent_through_update_and_rollback(sdk: object) -> None:
    """An agent's mutable config, and the checkpoint stack that lets an
    operator undo a bad update -- the same rollback shape as a deployment,
    on a different asset family."""
    agent = sdk.agents.create(  # type: ignore[attr-defined]
        "sdk-e2e-agent", experiment_id="exp-sdk-e2e-agent", name="SDK E2E Agent"
    )
    assert agent.agent_id == "sdk-e2e-agent"
    assert agent.enabled is not False

    updated = sdk.agents.update(agent.agent_id, name="SDK E2E Agent (renamed)")  # type: ignore[attr-defined]
    assert updated.name == "SDK E2E Agent (renamed)"

    paused = sdk.agents.update(agent.agent_id, enabled=False)  # type: ignore[attr-defined]
    assert paused.enabled is False

    assert sdk.agents.get(agent.agent_id).agent_id == agent.agent_id  # type: ignore[attr-defined]
    assert agent.agent_id in [a.agent_id for a in sdk.agents.list()]  # type: ignore[attr-defined]

    assert sdk.agents.delete(agent.agent_id) is True  # type: ignore[attr-defined]
    with pytest.raises(caliber_sdk.CaliberNotFoundError):
        sdk.agents.get(agent.agent_id)  # type: ignore[attr-defined]


# --- naming-corrections deprecation (AD-6) ---------------------------------


def test_the_deprecated_client_aliases_still_reach_the_real_server(sdk: object) -> None:
    """The whole point of a deprecation alias: old code keeps working.

    A caller who has not migrated off ``capabilities_api``/``datasets`` yet
    must still get real data from a real deployment -- a warning, not a
    breakage. This is the one place that claim is checked against the actual
    server rather than a mock.
    """
    with pytest.warns(DeprecationWarning, match="capabilities_info"):
        via_alias = sdk.capabilities_api.get()  # type: ignore[attr-defined]
    assert via_alias.sdk_stability

    with pytest.warns(DeprecationWarning, match="eval_datasets"):
        via_alias_list = sdk.datasets.list()  # type: ignore[attr-defined]
    assert isinstance(via_alias_list, list)


# --- decode_list strict mode (Phase 4.7) -----------------------------------


def test_decode_list_strict_mode_against_a_genuine_server_response_shape(
    sdk: object,
) -> None:
    """Strict mode is meant to catch a payload that was never a list --
    proved here against a real, non-mocked object response (``/me``), not a
    hand-built fixture standing in for "the wrong shape"."""
    from caliber_sdk.models import PersonalAccessToken, decode_list

    me_payload = sdk.raw.get("/me")  # type: ignore[attr-defined]
    assert isinstance(me_payload, dict)  # sanity: this really is an object, not a list

    # Default: tolerated, degrades to empty.
    assert decode_list(PersonalAccessToken, me_payload) == []

    # Strict: the same real payload now raises instead of silently emptying.
    with pytest.raises(caliber_sdk.CaliberDecodeError) as excinfo:
        decode_list(PersonalAccessToken, me_payload, strict=True)
    assert excinfo.value.payload == me_payload

    # A genuinely empty list from the real server still decodes cleanly under
    # strict mode -- confirmed against a real empty registry, not a stand-in.
    # /judges rather than /prompts deliberately: judges live in CALIBER's own
    # tables (routes/judges.py -- "CALIBER stays the source of truth"),
    # torn down and recreated fresh by the `engine` fixture for every test
    # function. /prompts is served from MLflow's own Prompt Registry
    # (routes/prompts.py's MlflowClient().search_prompts()) -- a store this
    # suite does not reset per test -- so it is not actually guaranteed
    # empty here: it failed exactly this way the one time this test ran as
    # part of the full suite instead of in isolation, because an earlier,
    # unrelated test had already registered a real prompt. (/auth/tokens was
    # tried first and rejected too: its envelope nests the list under a
    # "tokens" key -- {"tokens": [...]} -- that only TokensAPI.list()'s own
    # extra unwrap step handles; /judges' route returns the bare list
    # decode_list expects directly, matching JudgesAPI.list()'s own
    # implementation exactly.)
    from caliber_sdk.models import Judge

    assert decode_list(Judge, sdk.raw.get("/judges"), strict=True) == []  # type: ignore[attr-defined]


# --- CLI + SDK + real server together (Phase 5.3) --------------------------
#
# ``test_cli.py`` (sdk/caliber-cli) proves the CLI sends the right request
# for each new governance-verb command. ``test_the_sdk_drives_deployment_...``
# above proves the SDK methods those commands wrap work against a real
# server. Neither proves the three layers -- parser, SDK, real application --
# agree with each other; this does, driving ``caliber_cli.main`` directly.

caliber_cli_cli = pytest.importorskip("caliber_cli.cli", reason="caliber-cli is not installed")
exits = pytest.importorskip("caliber_cli.exits", reason="caliber-cli is not installed")


@pytest.fixture
def cli_main(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """Patch ``caliber_cli``'s client construction to reuse the in-process
    ``TestClient`` transport, mirroring ``sdk/caliber-cli/tests/conftest.py``'s
    own ``stub`` fixture -- but against the real application instead of a
    mocked one."""

    def patched(*args: object, **kwargs: object) -> object:
        kwargs["http_client"] = client
        return caliber_sdk.CaliberClient(*args, **kwargs)

    monkeypatch.setattr(caliber_cli_cli, "CaliberClient", patched)
    monkeypatch.setenv("CALIBER_BASE_URL", "http://testserver")
    monkeypatch.setenv("CALIBER_TOKEN", "")
    return caliber_cli_cli.main


def test_the_cli_drives_the_governance_verbs_against_the_real_server(
    cli_main: object, sdk: object
) -> None:
    """The exact command sequence Phase 5.3 added, run for real: promote,
    read the deployment back, roll back, record and show a gate verdict --
    each through ``caliberctl``'s own argument parser and exit-code
    mapping, not a direct SDK call."""
    workflow_id, v1 = _publish_workflow(sdk, "sdk-e2e-cli-workflow")
    v2 = sdk.workflows.versions.publish(  # type: ignore[attr-defined]
        sdk.workflows.versions.create(  # type: ignore[attr-defined]
            workflow_id, make_manifest(workflow_id)
        ).version_id
    ).version_id

    assert cli_main(["workflow", "promote", workflow_id, "dev", "--version-id", v1]) == exits.OK
    assert cli_main(["workflow", "deployments", workflow_id]) == exits.OK
    assert _deployed_version(sdk, workflow_id, "dev") == v1

    assert cli_main(["workflow", "promote", workflow_id, "dev", "--version-id", v2]) == exits.OK
    assert cli_main(["workflow", "rollback", workflow_id, "dev", "--yes"]) == exits.OK
    assert _deployed_version(sdk, workflow_id, "dev") == v1

    assert cli_main(["gate-verdict", "record", "workflow", v1, "--state", "pass"]) == exits.OK
    assert cli_main(["gate-verdict", "show", "workflow", v1]) == exits.OK

    assert (
        cli_main(["gate-verdict", "record", "workflow", v2, "--state", "fail"]) == exits.GATE_FAILED
    )


def test_the_cli_reports_awaiting_human_and_gate_failed_against_real_routes(
    cli_main: object, sdk: object
) -> None:
    """The two exit codes that only mean something end to end: a real 400/409
    from a rollback with no checkpoint must become the CLI's own FAILURE
    code, not a crash, and ``--yes`` really is required before anything
    changes -- checked against the live route, not a stub that always
    agrees with whatever the test expects."""
    workflow_id, _v1 = _publish_workflow(sdk, "sdk-e2e-cli-guard-workflow")

    assert cli_main(["workflow", "rollback", workflow_id, "dev"]) == exits.USAGE  # no --yes
    assert (
        cli_main(["workflow", "rollback", workflow_id, "dev", "--yes"]) == exits.FAILURE
    )  # no checkpoint yet on the real server
