"""Route-level proof for `P2-A`'s named follow-up (PR #394's own comment):
KB/OpenAPI's *child*-mutation routes -- operations that act on an existing
knowledge base/OpenAPI integration rather than the resource's own root
create/update/delete -- were left visibility-only when that PR wired
`resource.write.runtime`/`resource.execute` project-role checks onto the
root routes. `get_visible`/`_visible_integration_or_404` already refused an
*invisible* resource correctly, but nothing checked the caller's actual
*role* in a visible one, so a project **viewer** (visible via membership,
not by role) could still create a KB build version, activate/roll back a
version, sync a version into Apache AGE, run calibration, pin a baseline,
import/reimport an OpenAPI spec, review a dependency, generate/update a tool
draft, or fire a live preview call -- exactly the gap this file closes.

Follows `test_p2_resource_write_action_wiring.py`'s established deny/allow
pattern: a genuine project **viewer** member is visible (membership grants
visibility) but denied by role; a project **editor** succeeds. The
`resource.write.runtime`/`resource.execute` role sets and the
`require_project_access_if_scoped` call shape are already fully proven
(deny/allow *and* admin-no-membership/unscoped) by that file and by
`test_aria_plans.py`, so every route here only needs the deny/allow pair --
what's new at each call site is that the check is *present*, not the role
set or the no-op-when-unscoped shape.

Deny tests seed the minimum row directly via `db_session` (no object
store/embedding/LLM work needed: the new authorization check runs before
any of that). Allow tests exercise the real route end to end wherever the
service's own downstream logic requires it (creating a version, syncing to
Apache AGE, running calibration, importing/reimporting a spec, generating a
tool draft, previewing one) -- reusing this suite's established moto/
dummy-embedder/fake-judge/loopback-spec-server recipes -- and use a direct
row insert wherever the service does not need one (activating/rolling back
a version, pinning a baseline).
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

import caliber.knowledge.service as knowledge_service
from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberKnowledgeBase,
    CaliberKnowledgeBaseTestRun,
    CaliberKnowledgeBaseVersion,
    CaliberOpenApiIntegration,
    CaliberProject,
    CaliberProjectMember,
)
from caliber.knowledge import calibration
from caliber.knowledge.age import AgeSyncResult
from caliber.resource_access import ROLE_EDITOR, ROLE_VIEWER
from caliber.routes.knowledge_bases import (
    ACTIVATE_PATH,
    BASELINE_PATH,
    CALIBRATE_PATH,
    ROLLBACK_PATH,
    VERSION_AGE_SYNC_PATH,
    VERSIONS_PATH,
)
from caliber.routes.knowledge_bases import LIST_PATH as KB_LIST_PATH
from caliber.routes.openapi_integrations import (
    DEPENDENCIES_PATH,
    DEPENDENCY_DETAIL_PATH,
    IMPORT_PATH,
    OPERATIONS_PATH,
    REIMPORT_PATH,
    TOOL_DRAFT_DETAIL_PATH,
    TOOL_DRAFT_GENERATE_PATH,
    TOOL_DRAFT_PREVIEW_PATH,
)
from caliber.server import create_app

from .test_knowledge_calibration import _seed_dataset
from .test_routes_knowledge_bases import _DummyEmbedder, _put_text, _wire_moto, mock_aws
from .test_routes_openapi_integrations import (
    OPENAPI_SPEC,
    _create_integration,
    _import_version,
    _mock_http,
)

PROJECT_ID = "P-p2a-child-mutation"
EDITOR_HEADERS = {"X-CALIBER-User": "@editor-user", "X-CALIBER-Project": PROJECT_ID}
VIEWER_HEADERS = {"X-CALIBER-User": "@viewer-user", "X-CALIBER-Project": PROJECT_ID}


# `preview_openapi_tool_draft`'s allow test fires a real (mocked-transport) call
# to ``https://tickets.example.com`` -- allowlist it the same way
# `test_routes_openapi_integrations.py::app_config` does, so egress policy stays
# genuinely *on* rather than stubbed away. Applies to this whole module; every
# other test here either never calls the executor or only ever talks to
# 127.0.0.1, which is already allowed by the base test config.
@pytest.fixture
def app_config(app_config: CaliberConfig) -> CaliberConfig:
    return app_config.model_copy(
        update={
            "egress_allowed_hosts": "tickets.example.com,127.0.0.1",
            "egress_allow_unresolvable_hosts": True,
        }
    )


def _seed_project(session: Session, *, owner: str = "@test") -> None:
    session.add(
        CaliberProject(project_id=PROJECT_ID, name="P2-A child-route wiring project", owner=owner)
    )
    session.commit()


def _add_member(session: Session, user_id: str, role: str) -> None:
    session.add(
        CaliberProjectMember(
            member_id=f"M-{user_id.lstrip('@')}",
            project_id=PROJECT_ID,
            user_id=user_id,
            role=role,
            created_by="@test",
        )
    )
    session.commit()


def _grant_operator(client: TestClient, *users: str) -> None:
    config = client.app.state.config
    existing = {u for u in config.operator_users.split(",") if u}
    existing.update(users)
    client.app.state.config = config.model_copy(
        update={"operator_users": ",".join(sorted(existing))}
    )


def _seed_kb(
    session: Session,
    *,
    kb_id: str,
    active_version_id: str | None = None,
    bucket: str = "p2a-child-bucket",
) -> None:
    session.add(
        CaliberKnowledgeBase(
            knowledge_base_id=kb_id,
            name=kb_id,
            owner="@test",
            project_id=PROJECT_ID,
            visibility="project",
            status="active",
            source_bucket=bucket,
            active_version_id=active_version_id,
        )
    )
    session.commit()


def _seed_kb_version(
    session: Session,
    *,
    version_id: str,
    kb_id: str,
    version_number: int,
    status: str = "completed",
) -> None:
    session.add(
        CaliberKnowledgeBaseVersion(
            knowledge_base_version_id=version_id,
            knowledge_base_id=kb_id,
            version_number=version_number,
            status=status,
            chunking_strategy="recursive",
            chunking_config={"chunk_size": 120, "chunk_overlap": 20},
            embedding_model="sentence-transformers/all-MiniLM-L6-v2",
            source_manifest=[],
            output_bucket="p2a-child-bucket",
            output_prefix=f"{kb_id}/{version_id}",
            created_by="@test",
        )
    )
    session.commit()


def _seed_openapi_integration(session: Session, *, integration_id: str) -> None:
    session.add(
        CaliberOpenApiIntegration(
            integration_id=integration_id,
            name=integration_id,
            owner="@test",
            status="draft",
            project_id=PROJECT_ID,
            visibility="project",
        )
    )
    session.commit()


def _build_scoped_kb(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    *,
    headers: dict[str, str],
    name: str,
    bucket: str,
) -> dict[str, object]:
    """Build a real, synchronous, project-scoped KB build version.

    The moto S3 + dummy-embedder recipe `test_routes_knowledge_bases.py::
    _wire_kb_create` established, with ``headers`` added so the KB lands on
    `PROJECT_ID` (a project viewer/editor's visibility and role both key off
    the row's own `project_id`) rather than unscoped. Callers decorate their
    test with ``@mock_aws``.
    """
    monkeypatch.setattr(
        knowledge_service,
        "build_embedding_backend",
        lambda model_id: _DummyEmbedder(model_id),
    )
    s3 = _wire_moto(client)
    s3.create_bucket(Bucket=bucket)
    _put_text(
        s3,
        bucket,
        "docs/guide.md",
        "# Product Guide\n\nDark mode applies consistently across linked tools.\n",
        content_type="text/markdown",
    )
    created = client.post(
        KB_LIST_PATH,
        json={
            "name": name,
            "description": "P2-A child-mutation wiring fixture",
            "source_bucket": bucket,
            "sources": [{"kind": "folder", "path": "docs/"}],
            "chunking_strategy": "recursive",
            "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
            "chunking_config": {"chunk_size": 120, "chunk_overlap": 20},
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    return created.json()["data"]


class _FakeFeedback:
    """Minimal stand-in for an mlflow ``Feedback`` (carries ``.value``)."""

    def __init__(self, value: object) -> None:
        self.value = value
        self.rationale = None


@pytest.fixture
def _fake_kb_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic fake calibration judge -- see
    `test_knowledge_calibration.py::_fake_judge`, duplicated locally (rather
    than imported as a fixture across test modules, which is not an
    established pattern in this suite) since only `calibrate_knowledge_base`'s
    allow test here needs it."""

    fake = calibration.KbJudge(
        faithfulness_judge=lambda **_kw: _FakeFeedback(0.9),
        correctness_judge=lambda **_kw: _FakeFeedback(0.5),
    )
    monkeypatch.setattr(calibration, "build_kb_judge", lambda model=None: fake)


class _FakeAgeStore:
    """Just enough of `ApacheAgeKnowledgeStore` for `sync_version_to_age` to
    succeed without a real PostgreSQL+AGE deployment."""

    graph_name = "knowledge_graph"

    def sync_version(self, **_kwargs: object) -> AgeSyncResult:
        return AgeSyncResult(
            status="synced", graph_name=self.graph_name, node_count=1, edge_count=1
        )


class _SpecHandler(BaseHTTPRequestHandler):
    """Serves whatever ``server.body`` currently holds, over real loopback HTTP."""

    def do_GET(self) -> None:
        payload = self.server.body.encode("utf-8")  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/yaml")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        """Keep test output clean."""


class _SpecServer(HTTPServer):
    body: str = ""


def _start_spec_server(body: str) -> tuple[_SpecServer, str]:
    server = _SpecServer(("127.0.0.1", 0), _SpecHandler)
    server.body = body
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    return server, f"http://{host}:{port}/openapi.yaml"


_KB_VERSION_BODY = {
    "chunking_strategy": "recursive",
    "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
    "chunking_config": {"chunk_size": 120, "chunk_overlap": 20},
}


# ---------------------------------------------------------------------------
# knowledge_bases.py::create_version -- `resource.write.runtime`
# ---------------------------------------------------------------------------


def test_create_version_denies_a_project_viewer(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    _seed_kb(db_session, kb_id="KB-p2a-create-version")
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.post(
        VERSIONS_PATH.replace("{knowledge_base_id}", "KB-p2a-create-version"),
        json=_KB_VERSION_BODY,
        headers=VIEWER_HEADERS,
    )
    assert resp.status_code == 403, resp.text


@mock_aws
def test_create_version_allows_a_project_editor(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_project(db_session)
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")
    created = _build_scoped_kb(
        client,
        monkeypatch,
        headers=EDITOR_HEADERS,
        name="p2a-create-version-kb",
        bucket="p2a-cv-bucket",
    )
    kb_id = created["knowledge_base"]["knowledge_base_id"]

    resp = client.post(
        VERSIONS_PATH.replace("{knowledge_base_id}", kb_id),
        json=_KB_VERSION_BODY,
        headers=EDITOR_HEADERS,
    )
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# knowledge_bases.py::activate_version -- `resource.write.runtime`
# ---------------------------------------------------------------------------


def test_activate_version_denies_a_project_viewer(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    kb_id = "KB-p2a-activate"
    _seed_kb(db_session, kb_id=kb_id, active_version_id=f"{kb_id}-v2")
    _seed_kb_version(db_session, version_id=f"{kb_id}-v1", kb_id=kb_id, version_number=1)
    _seed_kb_version(db_session, version_id=f"{kb_id}-v2", kb_id=kb_id, version_number=2)
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.post(
        ACTIVATE_PATH.replace("{knowledge_base_id}", kb_id).replace("{version_id}", f"{kb_id}-v1"),
        json={},
        headers=VIEWER_HEADERS,
    )
    assert resp.status_code == 403, resp.text


def test_activate_version_allows_a_project_editor(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    kb_id = "KB-p2a-activate2"
    _seed_kb(db_session, kb_id=kb_id, active_version_id=f"{kb_id}-v2")
    _seed_kb_version(db_session, version_id=f"{kb_id}-v1", kb_id=kb_id, version_number=1)
    _seed_kb_version(db_session, version_id=f"{kb_id}-v2", kb_id=kb_id, version_number=2)
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")

    resp = client.post(
        ACTIVATE_PATH.replace("{knowledge_base_id}", kb_id).replace("{version_id}", f"{kb_id}-v1"),
        json={},
        headers=EDITOR_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["active_version_id"] == f"{kb_id}-v1"


# ---------------------------------------------------------------------------
# knowledge_bases.py::rollback_version -- `resource.write.runtime`
# ---------------------------------------------------------------------------


def test_rollback_version_denies_a_project_viewer(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    kb_id = "KB-p2a-rollback"
    _seed_kb(db_session, kb_id=kb_id, active_version_id=f"{kb_id}-v1")
    _seed_kb_version(db_session, version_id=f"{kb_id}-v1", kb_id=kb_id, version_number=1)
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.post(
        ROLLBACK_PATH.replace("{knowledge_base_id}", kb_id),
        json={},
        headers=VIEWER_HEADERS,
    )
    assert resp.status_code == 403, resp.text


def test_rollback_version_allows_a_project_editor(client: TestClient, db_session: Session) -> None:
    _seed_project(db_session)
    kb_id = "KB-p2a-rollback2"
    _seed_kb(db_session, kb_id=kb_id, active_version_id=f"{kb_id}-v2")
    _seed_kb_version(db_session, version_id=f"{kb_id}-v1", kb_id=kb_id, version_number=1)
    _seed_kb_version(db_session, version_id=f"{kb_id}-v2", kb_id=kb_id, version_number=2)
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")

    # Build a real "v2 active -> activate v1" audit row for `rollback_version`'s
    # own audit-trail walk to restore -- also itself proof of `activate_version`'s
    # allow path, already asserted in detail above.
    activated = client.post(
        ACTIVATE_PATH.replace("{knowledge_base_id}", kb_id).replace("{version_id}", f"{kb_id}-v1"),
        json={},
        headers=EDITOR_HEADERS,
    )
    assert activated.status_code == 200, activated.text

    resp = client.post(
        ROLLBACK_PATH.replace("{knowledge_base_id}", kb_id),
        json={},
        headers=EDITOR_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["active_version_id"] == f"{kb_id}-v2"


# ---------------------------------------------------------------------------
# knowledge_bases.py::sync_version_to_age -- `resource.write.runtime`
# ---------------------------------------------------------------------------


def test_sync_version_to_age_denies_a_project_viewer(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    kb_id = "KB-p2a-sync-age"
    _seed_kb(db_session, kb_id=kb_id)
    _seed_kb_version(db_session, version_id=f"{kb_id}-v1", kb_id=kb_id, version_number=1)
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.post(
        VERSION_AGE_SYNC_PATH.replace("{version_id}", f"{kb_id}-v1"),
        json={},
        headers=VIEWER_HEADERS,
    )
    assert resp.status_code == 403, resp.text


@mock_aws
def test_sync_version_to_age_allows_a_project_editor(
    app_config: CaliberConfig,
    engine: object,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apache AGE availability requires a real PostgreSQL+AGE deployment
    (`ApacheAgeKnowledgeStore.available` checks the bound engine's dialect),
    so -- matching `test_routes_knowledge_bases.py::
    test_knowledge_base_age_graph_sync_and_retrieval` -- this fakes
    `build_age_store` and stands up its own `knowledge_age_enabled=True`
    app/client rather than using the shared `client` fixture.
    """
    monkeypatch.setattr(
        knowledge_service,
        "build_embedding_backend",
        lambda model_id: _DummyEmbedder(model_id),
    )
    monkeypatch.setattr(knowledge_service, "build_age_store", lambda **_kwargs: _FakeAgeStore())

    age_config = app_config.model_copy(update={"knowledge_age_enabled": True})
    app = create_app(config=age_config)
    app.state.engine = engine
    app.state.session_factory = session_factory

    with TestClient(app, headers={"X-CALIBER-User": "@test"}) as age_client:
        with session_factory() as session:
            _seed_project(session)
            _add_member(session, "@editor-user", ROLE_EDITOR)
        _grant_operator(age_client, "@editor-user")

        created = _build_scoped_kb(
            age_client,
            monkeypatch,
            headers=EDITOR_HEADERS,
            name="p2a-age-kb",
            bucket="p2a-age-bucket",
        )
        version_id = created["version"]["knowledge_base_version_id"]

        resp = age_client.post(
            VERSION_AGE_SYNC_PATH.replace("{version_id}", version_id),
            json={},
            headers=EDITOR_HEADERS,
        )
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# knowledge_bases.py::calibrate_knowledge_base -- `resource.execute`
# ---------------------------------------------------------------------------


def test_calibrate_knowledge_base_denies_a_project_viewer(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _seed_kb(db_session, kb_id="KB-p2a-calibrate")
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.post(
        CALIBRATE_PATH.replace("{knowledge_base_id}", "KB-p2a-calibrate"),
        json={"version_id": "dummy-version", "eval_dataset_id": "dummy-dataset"},
        headers=VIEWER_HEADERS,
    )
    assert resp.status_code == 403, resp.text


@mock_aws
def test_calibrate_knowledge_base_allows_a_project_editor(
    client: TestClient,
    db_session: Session,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    _fake_kb_judge: None,
) -> None:
    _seed_project(db_session)
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")
    created = _build_scoped_kb(
        client,
        monkeypatch,
        headers=EDITOR_HEADERS,
        name="p2a-calibrate-kb",
        bucket="p2a-cal-bucket",
    )
    kb_id = created["knowledge_base"]["knowledge_base_id"]
    version_id = created["version"]["knowledge_base_version_id"]

    with session_factory() as session:
        _seed_dataset(
            session,
            dataset_id="ED-p2a-cal",
            examples=[
                {
                    "input": {"question": "Does dark mode apply consistently?"},
                    "expected": {"sources": ["docs/guide.md"], "answer": "Yes."},
                }
            ],
        )

    resp = client.post(
        CALIBRATE_PATH.replace("{knowledge_base_id}", kb_id),
        json={"version_id": version_id, "eval_dataset_id": "ED-p2a-cal"},
        headers=EDITOR_HEADERS,
    )
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# knowledge_bases.py::set_knowledge_base_baseline -- `resource.write.runtime`
# ---------------------------------------------------------------------------


def test_set_knowledge_base_baseline_denies_a_project_viewer(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _seed_kb(db_session, kb_id="KB-p2a-baseline")
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.post(
        BASELINE_PATH.replace("{knowledge_base_id}", "KB-p2a-baseline"),
        json={"test_run_id": "KBTR-dummy"},
        headers=VIEWER_HEADERS,
    )
    assert resp.status_code == 403, resp.text


def test_set_knowledge_base_baseline_allows_a_project_editor(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    kb_id = "KB-p2a-baseline2"
    _seed_kb(db_session, kb_id=kb_id)
    db_session.add(
        CaliberKnowledgeBaseTestRun(
            test_run_id="KBTR-p2a-baseline2",
            knowledge_base_id=kb_id,
            knowledge_base_version_id=f"{kb_id}-v1",
        )
    )
    db_session.commit()
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")

    resp = client.post(
        BASELINE_PATH.replace("{knowledge_base_id}", kb_id),
        json={"test_run_id": "KBTR-p2a-baseline2"},
        headers=EDITOR_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["baseline_run_id"] == "KBTR-p2a-baseline2"


# ---------------------------------------------------------------------------
# openapi_integrations.py::import_openapi_version -- `resource.write.runtime`
# ---------------------------------------------------------------------------


def test_import_openapi_version_denies_a_project_viewer(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _seed_openapi_integration(db_session, integration_id="OAI-p2a-import")
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.post(
        IMPORT_PATH.replace("{integration_id}", "OAI-p2a-import"),
        json={
            "source_kind": "inline_text",
            "spec_text": OPENAPI_SPEC,
            "source_ref": "inline://viewer-attempt",
        },
        headers=VIEWER_HEADERS,
    )
    assert resp.status_code == 403, resp.text


def test_import_openapi_version_allows_a_project_editor(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")
    integration = _create_integration(client, headers=EDITOR_HEADERS, name="p2a-import-integration")
    integration_id = str(integration["integration_id"])

    resp = client.post(
        IMPORT_PATH.replace("{integration_id}", integration_id),
        json={
            "source_kind": "inline_text",
            "spec_text": OPENAPI_SPEC,
            "source_ref": "inline://editor-attempt",
        },
        headers=EDITOR_HEADERS,
    )
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# openapi_integrations.py::reimport_openapi_version -- `resource.write.runtime`
# ---------------------------------------------------------------------------


def test_reimport_openapi_version_denies_a_project_viewer(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _seed_openapi_integration(db_session, integration_id="OAI-p2a-reimport")
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.post(
        REIMPORT_PATH.replace("{integration_id}", "OAI-p2a-reimport"),
        headers=VIEWER_HEADERS,
    )
    assert resp.status_code == 403, resp.text


def test_reimport_openapi_version_allows_a_project_editor(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")

    server, url = _start_spec_server(OPENAPI_SPEC)
    try:
        integration = _create_integration(
            client, headers=EDITOR_HEADERS, name="p2a-reimport-integration"
        )
        integration_id = str(integration["integration_id"])
        imported = client.post(
            IMPORT_PATH.replace("{integration_id}", integration_id),
            json={"source_kind": "url", "spec_url": url},
            headers=EDITOR_HEADERS,
        )
        assert imported.status_code == 201, imported.text

        # A reimport of byte-identical content 409s (`_do_import`'s own
        # sha256-based dedup) -- change the served body first, matching
        # `test_reimport_refetches_and_diffs_against_the_previous_url_version`.
        server.body = OPENAPI_SPEC + (
            "  /tickets/{ticket_id}/archive:\n"
            "    post:\n"
            "      operationId: archiveTicket\n"
            "      responses:\n"
            '        "200":\n'
            "          description: ok\n"
        )

        resp = client.post(
            REIMPORT_PATH.replace("{integration_id}", integration_id),
            headers=EDITOR_HEADERS,
        )
        assert resp.status_code == 201, resp.text
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# openapi_integrations.py::review_openapi_dependency -- `resource.write.runtime`
# ---------------------------------------------------------------------------


def test_review_openapi_dependency_denies_a_project_viewer(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _seed_openapi_integration(db_session, integration_id="OAI-p2a-review")
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.patch(
        DEPENDENCY_DETAIL_PATH.replace("{integration_id}", "OAI-p2a-review").replace(
            "{dependency_id}", "OAD-dummy"
        ),
        json={"status": "confirmed"},
        headers=VIEWER_HEADERS,
    )
    assert resp.status_code == 403, resp.text


def test_review_openapi_dependency_allows_a_project_editor(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")
    integration = _create_integration(client, headers=EDITOR_HEADERS, name="p2a-review-integration")
    integration_id = str(integration["integration_id"])
    _import_version(client, integration_id, headers=EDITOR_HEADERS)

    suggested = client.get(
        DEPENDENCIES_PATH.replace("{integration_id}", integration_id),
        params={"status": "suggested"},
        headers=EDITOR_HEADERS,
    ).json()["data"]
    assert suggested, "OPENAPI_SPEC fixture expected to produce a suggested dependency"
    dependency_id = suggested[0]["dependency_id"]

    resp = client.patch(
        DEPENDENCY_DETAIL_PATH.replace("{integration_id}", integration_id).replace(
            "{dependency_id}", dependency_id
        ),
        json={"status": "confirmed"},
        headers=EDITOR_HEADERS,
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# openapi_integrations.py::generate_openapi_tool_drafts -- `resource.write.runtime`
# ---------------------------------------------------------------------------


def test_generate_openapi_tool_drafts_denies_a_project_viewer(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _seed_openapi_integration(db_session, integration_id="OAI-p2a-generate")
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.post(
        TOOL_DRAFT_GENERATE_PATH.replace("{integration_id}", "OAI-p2a-generate"),
        json={"operation_ids": ["dummy-operation"]},
        headers=VIEWER_HEADERS,
    )
    assert resp.status_code == 403, resp.text


def test_generate_openapi_tool_drafts_allows_a_project_editor(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")
    integration = _create_integration(
        client, headers=EDITOR_HEADERS, name="p2a-generate-integration"
    )
    integration_id = str(integration["integration_id"])
    _import_version(client, integration_id, headers=EDITOR_HEADERS)

    resp = client.post(
        TOOL_DRAFT_GENERATE_PATH.replace("{integration_id}", integration_id),
        json={"tags": ["tickets"], "methods": ["GET"]},
        headers=EDITOR_HEADERS,
    )
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# openapi_integrations.py::update_openapi_tool_draft -- `resource.write.runtime`
# ---------------------------------------------------------------------------


def test_update_openapi_tool_draft_denies_a_project_viewer(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _seed_openapi_integration(db_session, integration_id="OAI-p2a-update-draft")
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.patch(
        TOOL_DRAFT_DETAIL_PATH.replace("{integration_id}", "OAI-p2a-update-draft").replace(
            "{draft_id}", "OATD-dummy"
        ),
        json={"description": "denied attempt"},
        headers=VIEWER_HEADERS,
    )
    assert resp.status_code == 403, resp.text


def test_update_openapi_tool_draft_allows_a_project_editor(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")
    integration = _create_integration(
        client, headers=EDITOR_HEADERS, name="p2a-update-draft-integration"
    )
    integration_id = str(integration["integration_id"])
    _import_version(client, integration_id, headers=EDITOR_HEADERS)
    generated = client.post(
        TOOL_DRAFT_GENERATE_PATH.replace("{integration_id}", integration_id),
        json={"tags": ["tickets"], "methods": ["GET"]},
        headers=EDITOR_HEADERS,
    )
    assert generated.status_code == 201, generated.text
    draft_id = generated.json()["data"][0]["draft_id"]

    resp = client.patch(
        TOOL_DRAFT_DETAIL_PATH.replace("{integration_id}", integration_id).replace(
            "{draft_id}", draft_id
        ),
        json={"description": "edited by a project editor"},
        headers=EDITOR_HEADERS,
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# openapi_integrations.py::preview_openapi_tool_draft -- `resource.execute`
# ---------------------------------------------------------------------------


def test_preview_openapi_tool_draft_denies_a_project_viewer(
    client: TestClient, db_session: Session
) -> None:
    _seed_project(db_session)
    _seed_openapi_integration(db_session, integration_id="OAI-p2a-preview")
    _add_member(db_session, "@viewer-user", ROLE_VIEWER)
    _grant_operator(client, "@viewer-user")

    resp = client.post(
        TOOL_DRAFT_PREVIEW_PATH.replace("{integration_id}", "OAI-p2a-preview").replace(
            "{draft_id}", "OATD-dummy"
        ),
        json={"input": {}},
        headers=VIEWER_HEADERS,
    )
    assert resp.status_code == 403, resp.text


def test_preview_openapi_tool_draft_allows_a_project_editor(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_project(db_session)
    _add_member(db_session, "@editor-user", ROLE_EDITOR)
    _grant_operator(client, "@editor-user")
    integration = _create_integration(
        client, headers=EDITOR_HEADERS, name="p2a-preview-integration"
    )
    integration_id = str(integration["integration_id"])
    _import_version(client, integration_id, headers=EDITOR_HEADERS)
    operations = client.get(
        OPERATIONS_PATH.replace("{integration_id}", integration_id), headers=EDITOR_HEADERS
    ).json()["data"]
    target = next(
        item for item in operations if item["operation_key"] == "GET /tickets/{ticket_id}"
    )

    monkeypatch.setenv("OPENAPI_TICKET_TOKEN_P2A", "ticket-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ticket_id": "T-1", "status": "open"})

    _mock_http(monkeypatch, handler)

    generated = client.post(
        TOOL_DRAFT_GENERATE_PATH.replace("{integration_id}", integration_id),
        json={
            "operation_ids": [target["operation_id"]],
            "auth_binding": {"kind": "bearer", "secret_ref": "env://OPENAPI_TICKET_TOKEN_P2A"},
            "allow_in_preview": True,
        },
        headers=EDITOR_HEADERS,
    )
    assert generated.status_code == 201, generated.text
    draft_id = generated.json()["data"][0]["draft_id"]

    resp = client.post(
        TOOL_DRAFT_PREVIEW_PATH.replace("{integration_id}", integration_id).replace(
            "{draft_id}", draft_id
        ),
        json={"input": {"path_params": {"ticket_id": "T-1"}}},
        headers=EDITOR_HEADERS,
    )
    assert resp.status_code == 200, resp.text
