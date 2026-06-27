"""Cross-endpoint RBAC enforcement.

Every write endpoint should:

* Return ``401`` when the request has no identity header (anonymous).
* Return ``403`` when the request comes from a signed-in user who lacks
  the required scope.
* Succeed (or fail on its own merits, like a 404 for a missing row)
  when the user holds the required scope.

This file walks the full write surface to pin those contracts in one
place. When a future write endpoint is added, the table at the top of
this file should grow with it — otherwise the scope check is easy to
forget on a new route.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberApprovalRequest,
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberRefinementJob,
    CaliberRollbackCheckpoint,
    CaliberSkill,
    CaliberVerificationItem,
)
from caliber.server import create_app
from tests.workflow_helpers import make_tool_payload

# Each row: (label, method, path, body, required-scope).
# The label is the failure message — pytest prints it on a parametrize
# failure so you immediately know which endpoint regressed.
_WRITE_ENDPOINTS: list[tuple[str, str, str, dict[str, object], str]] = [
    (
        "jobs:apply",
        "POST",
        "/ajax-api/2.0/mlflow/caliber/jobs/RFN-EXIST/apply",
        {},
        "operator",
    ),
    (
        "agents:register",
        "POST",
        "/ajax-api/2.0/mlflow/caliber/agents",
        {
            "agent_id": "new-agent",
            "experiment_id": "exp-new",
            "name": "x",
            "owner": "@x",
        },
        "admin",
    ),
    (
        "agents:update",
        "PATCH",
        "/ajax-api/2.0/mlflow/caliber/agents/agent",
        {"enabled": False},
        "admin",
    ),
    (
        "rollback:execute",
        "POST",
        "/ajax-api/2.0/mlflow/caliber/agents/agent/rollback",
        {},
        "operator",
    ),
    (
        "skills:create",
        "POST",
        "/ajax-api/2.0/mlflow/caliber/skills",
        {"name": "rbac-test", "content": "x", "owner": "@x"},
        "operator",
    ),
    (
        "skills:import-package",
        "POST",
        "/ajax-api/2.0/mlflow/caliber/skills/import-package",
        {
            "owner": "@x",
            "files": [
                {
                    "path": "rbac-import/SKILL.md",
                    "content": "---\nname: rbac-import\ndescription: Import.\n---\nBody",
                }
            ],
        },
        "operator",
    ),
    (
        "skills:update",
        "PATCH",
        "/ajax-api/2.0/mlflow/caliber/skills/SK-EXIST",
        {"content": "y"},
        "admin",
    ),
    (
        "eval-datasets:create",
        "POST",
        "/ajax-api/2.0/mlflow/caliber/eval-datasets",
        {"name": "rbac-test", "owner": "@x"},
        "operator",
    ),
    (
        "eval-datasets:update",
        "PATCH",
        "/ajax-api/2.0/mlflow/caliber/eval-datasets/ED-EXIST",
        {"status": "archived"},
        "admin",
    ),
    (
        "eval-datasets:append-example",
        "POST",
        "/ajax-api/2.0/mlflow/caliber/eval-datasets/ED-EXIST/examples",
        {"input": {}},
        "operator",
    ),
    (
        "eval-datasets:supersede-example",
        "POST",
        "/ajax-api/2.0/mlflow/caliber/eval-datasets/ED-EXIST/examples/EX-EXIST/supersede",
        {},
        "admin",
    ),
]

_ENDPOINT_IDS = [row[0] for row in _WRITE_ENDPOINTS]


# Users we use for scope-negative paths:
#  - ``@viewer`` is a signed-in user not in any scope list → 403 on any write
#  - ``@operator-only``/``@approver-only`` carry exactly one scope each
_VIEWER_USER = "@viewer"
_OPERATOR_USER = "@operator-only"
_APPROVER_USER = "@approver-only"
_ADMIN_USER = "@admin-only"


@pytest.fixture
def rbac_client(
    tmp_path: Path,
    engine: Engine,
    session_factory: sessionmaker[Session],
) -> Iterator[TestClient]:
    """A client wired with three distinct test users — one per scope
    (besides admin) — so the parametrized table can validate every
    rejection branch."""
    db_path = tmp_path / "caliber-rbac.db"
    config = CaliberConfig.load(
        environ={
            "CALIBER_DATABASE_URL": f"sqlite+pysqlite:///{db_path}",
            "CALIBER_ADMIN_USERS": _ADMIN_USER,
            "CALIBER_OPERATOR_USERS": _OPERATOR_USER,
            "CALIBER_APPROVER_USERS": _APPROVER_USER,
        }
    )
    app = create_app(config=config)
    app.state.engine = engine
    app.state.session_factory = session_factory
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def seeded_db(db_session: Session) -> Session:
    """Seed enough rows that endpoints reach their scope check before
    hitting a 404. The point is to verify the *scope* gate, not the
    body validation that comes after it."""
    db_session.add(
        CaliberAgentConfig(
            agent_id="agent",
            experiment_id="exp",
            name="A",
            owner="@x",
            artifact_types=["prompt"],
            eval_thresholds={},
            optimizer_config={},
            approval_policy={},
        )
    )
    db_session.flush()
    db_session.add_all(
        [
            CaliberVerificationItem(
                item_id="FB-EXIST",
                agent_id="agent",
                category="hallucination",
                free_text="...",
                severity="critical",
                status="pending",
            ),
            CaliberVerificationItem(
                item_id="FB-OTHER",
                agent_id="agent",
                category="hallucination",
                free_text="...",
                severity="critical",
                status="pending",
            ),
        ]
    )
    db_session.flush()
    db_session.add(
        CaliberRefinementJob(
            job_id="RFN-EXIST",
            agent_id="agent",
            primary_item_id="FB-EXIST",
            artifact_type="prompt",
            status="candidate_ready",
            current_stage="done",
            bundle_targets=[],
            candidate={"content": "x", "artifact_type": "prompt"},
        )
    )
    db_session.flush()
    db_session.add(
        CaliberApprovalRequest(
            approval_id="AP-EXIST",
            job_id="RFN-EXIST",
            agent_id="agent",
            status="pending",
            eval_results={},
            candidate_snapshot={"content": "x", "artifact_type": "prompt"},
            diagnosis_snapshot=None,
        )
    )
    db_session.flush()
    db_session.add(
        CaliberRollbackCheckpoint(
            checkpoint_id="CK-EXIST",
            approval_id="AP-EXIST",
            agent_id="agent",
            artifact_type="prompt",
            artifact_name="agent",
            artifact_ref_before="prompts:/agent/1",
            artifact_ref_after="prompts:/agent/2",
            version_before=1,
            version_after=2,
        )
    )
    db_session.add(
        CaliberSkill(
            skill_id="SK-EXIST",
            name="rbac-existing-skill",
            description="",
            content="x",
            owner="@x",
            tags=[],
            status="active",
            version=1,
        )
    )
    db_session.add(
        CaliberEvalDataset(
            dataset_id="ED-EXIST",
            name="rbac-existing-dataset",
            description="",
            owner="@x",
            tags=[],
            status="active",
            version=1,
        )
    )
    db_session.flush()
    db_session.add(
        CaliberEvalDatasetExample(
            example_id="EX-EXIST",
            dataset_id="ED-EXIST",
            dataset_version=1,
            input={},
            expected={},
            weight=1.0,
            tags=[],
        )
    )
    db_session.commit()
    return db_session


# Read endpoints that should reject anonymous callers. Health, metrics,
# and the CSRF issuance endpoint are intentionally omitted — health is an
# operator-controlled liveness probe, metrics is gated by network
# (Prometheus scrape), and the CSRF endpoint already enforces auth
# internally when CSRF is enabled.
_READ_ENDPOINTS: list[tuple[str, str]] = [
    ("agents:list", "/ajax-api/2.0/mlflow/caliber/agents"),
    ("agents:get", "/ajax-api/2.0/mlflow/caliber/agents/agent"),
    ("agents:skills", "/ajax-api/2.0/mlflow/caliber/agents/agent/skills"),
    ("jobs:list", "/ajax-api/2.0/mlflow/caliber/jobs"),
    ("jobs:get", "/ajax-api/2.0/mlflow/caliber/jobs/RFN-EXIST"),
    ("jobs:targets", "/ajax-api/2.0/mlflow/caliber/jobs/RFN-EXIST/targets"),
    ("skills:list", "/ajax-api/2.0/mlflow/caliber/skills"),
    ("skills:get", "/ajax-api/2.0/mlflow/caliber/skills/SK-EXIST"),
    ("skills:package", "/ajax-api/2.0/mlflow/caliber/skills/SK-EXIST/package"),
    ("skills:package-zip", "/ajax-api/2.0/mlflow/caliber/skills/SK-EXIST/package.zip"),
    ("eval-datasets:list", "/ajax-api/2.0/mlflow/caliber/eval-datasets"),
    ("eval-datasets:get", "/ajax-api/2.0/mlflow/caliber/eval-datasets/ED-EXIST"),
    (
        "eval-datasets:examples",
        "/ajax-api/2.0/mlflow/caliber/eval-datasets/ED-EXIST/examples",
    ),
    ("dashboard:summary", "/ajax-api/2.0/mlflow/caliber/dashboard/summary"),
    (
        "rollback:list-checkpoints",
        "/ajax-api/2.0/mlflow/caliber/agents/agent/checkpoints",
    ),
]
_READ_LABELS = [row[0] for row in _READ_ENDPOINTS]


@pytest.mark.parametrize(("label", "path"), _READ_ENDPOINTS, ids=_READ_LABELS)
def test_anonymous_gets_401_on_every_read(
    rbac_client: TestClient,
    seeded_db: Session,
    label: str,
    path: str,
) -> None:
    """Every read endpoint rejects anonymous with 401 (deep-review Finding 5).

    The CHANGELOG and ``caliber.auth`` docstrings claim reads are
    "open to every authenticated user"; this test pins the matching
    enforcement so an anonymous caller (no ``X-CALIBER-User`` header)
    can't read operational data when an upstream proxy is missing
    or misconfigured.
    """
    _ = label, seeded_db
    response = rbac_client.get(path, headers={"X-CALIBER-User": ""})
    assert response.status_code == 401, response.text


@pytest.mark.parametrize(("label", "path"), _READ_ENDPOINTS, ids=_READ_LABELS)
def test_signed_in_viewer_passes_read_auth(
    rbac_client: TestClient,
    seeded_db: Session,
    label: str,
    path: str,
) -> None:
    """A signed-in user without any write scope still gets through
    the auth gate on reads — viewer is the implicit scope every
    authenticated user carries."""
    _ = label, seeded_db
    response = rbac_client.get(path, headers={"X-CALIBER-User": _VIEWER_USER})
    # 200/204 = happy path; 404 is acceptable when the seeded fixture
    # doesn't cover that path's resource (the auth gate is what we're
    # testing, not the resource-lookup logic).
    assert response.status_code in (200, 204, 404), response.text


@pytest.mark.parametrize(
    ("label", "method", "path", "body", "scope"),
    _WRITE_ENDPOINTS,
    ids=_ENDPOINT_IDS,
)
def test_anonymous_gets_401_on_every_write(
    rbac_client: TestClient,
    seeded_db: Session,
    label: str,
    method: str,
    path: str,
    body: dict[str, object],
    scope: str,
) -> None:
    """Every write endpoint rejects anonymous with 401."""
    _ = label, seeded_db, scope  # only used by pytest for the test ID
    response = rbac_client.request(method, path, json=body, headers={"X-CALIBER-User": ""})
    assert response.status_code == 401, response.text


@pytest.mark.parametrize(
    ("label", "method", "path", "body", "scope"),
    _WRITE_ENDPOINTS,
    ids=_ENDPOINT_IDS,
)
def test_viewer_only_user_gets_403_on_every_write(
    rbac_client: TestClient,
    seeded_db: Session,
    label: str,
    method: str,
    path: str,
    body: dict[str, object],
    scope: str,
) -> None:
    """Any signed-in user without the required write scope gets 403."""
    _ = label, seeded_db, scope
    response = rbac_client.request(
        method, path, json=body, headers={"X-CALIBER-User": _VIEWER_USER}
    )
    assert response.status_code == 403, response.text


@pytest.mark.parametrize(
    ("label", "method", "path", "body", "scope"),
    _WRITE_ENDPOINTS,
    ids=_ENDPOINT_IDS,
)
def test_admin_user_passes_every_write_auth_gate(
    rbac_client: TestClient,
    seeded_db: Session,
    label: str,
    method: str,
    path: str,
    body: dict[str, object],
    scope: str,
) -> None:
    """Admin should never be rejected by scope checks on write endpoints."""
    _ = label, seeded_db, scope
    response = rbac_client.request(method, path, json=body, headers={"X-CALIBER-User": _ADMIN_USER})
    assert response.status_code not in (401, 403), response.text


def test_operator_can_apply_but_cannot_register_agents(
    rbac_client: TestClient, seeded_db: Session
) -> None:
    """An operator clears an operator-scoped write (Apply) but not an
    admin-scoped one (agent CRUD).

    Spot-check that the cross-scope boundary is real — the parametrized
    table above covers anonymous + viewer; this pins the operator/admin
    split too.
    """
    _ = seeded_db
    # Operator applying a candidate_ready job (operator-scoped write) → 200.
    response = rbac_client.post(
        "/ajax-api/2.0/mlflow/caliber/jobs/RFN-EXIST/apply",
        json={},
        headers={"X-CALIBER-User": _OPERATOR_USER},
    )
    assert response.status_code == 200

    # Same operator on an admin-scoped write (agent register) → 403.
    response = rbac_client.post(
        "/ajax-api/2.0/mlflow/caliber/agents",
        json={
            "agent_id": "new-agent",
            "experiment_id": "exp-new",
            "name": "x",
            "owner": "@x",
        },
        headers={"X-CALIBER-User": _OPERATOR_USER},
    )
    assert response.status_code == 403


def test_operator_can_sandbox_test_run_but_viewer_cannot(rbac_client: TestClient) -> None:
    """The non-persisting tool sandbox preview is operator-gated, not admin-gated.

    Demanding ADMIN for a throwaway sandbox run while the persisting create is
    only OPERATOR-gated was backwards; this pins the corrected boundary — an
    operator can preview, a viewer still cannot.
    """
    # Tool create is admin-scoped, so register the fixture tool as the admin.
    create = rbac_client.post(
        "/ajax-api/2.0/mlflow/caliber/tools",
        json=make_tool_payload("lookup_policy", allow_in_preview=True),
        headers={"X-CALIBER-User": _ADMIN_USER},
    )
    assert create.status_code == 201, create.text
    tool_id = create.json()["data"]["tool_id"]

    # Operator (not admin) clears the scope gate on the sandbox preview.
    operator = rbac_client.post(
        f"/ajax-api/2.0/mlflow/caliber/tools/{tool_id}/test-run",
        json={"input": {"query": "refund"}},
        headers={"X-CALIBER-User": _OPERATOR_USER},
    )
    assert operator.status_code == 200, operator.text

    # A viewer still cannot run it.
    viewer = rbac_client.post(
        f"/ajax-api/2.0/mlflow/caliber/tools/{tool_id}/test-run",
        json={"input": {"query": "refund"}},
        headers={"X-CALIBER-User": _VIEWER_USER},
    )
    assert viewer.status_code == 403, viewer.text


def test_read_endpoints_still_work_without_scope(rbac_client: TestClient) -> None:
    """Reads stay open — any authenticated request lands on viewer."""
    response = rbac_client.get(
        "/ajax-api/2.0/mlflow/caliber/agents",
        headers={"X-CALIBER-User": _VIEWER_USER},
    )
    assert response.status_code == 200
    response = rbac_client.get(
        "/ajax-api/2.0/mlflow/caliber/dashboard/summary",
        headers={"X-CALIBER-User": _VIEWER_USER},
    )
    assert response.status_code == 200
