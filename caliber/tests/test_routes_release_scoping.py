"""C3: a known id must not cross a project boundary in the release control plane.

The review's finding, narrowed to its most consequential instance:

    workflow-version lookup is also a bare primary-key read, and version list/create/
    update/publish/preview/run/restore paths do not establish workflow/project
    ownership. Deployment list/promote/rollback use bare workflow IDs, while promotion
    approve/reject fetch by promotion ID without project scoping. Known IDs can
    therefore cross project boundaries in the core release control plane.

These are the tests that make that closure real, because the fix is invisible to a
positive test: every existing suite passes either way. The property under test is what
happens to a caller who *should not* be able to reach a row and knows its id anyway.

Two conventions worth stating, because both are deliberate:

* **The expected status is 404, not 403.** "Exists but forbidden" confirms the id is
  real, which is itself a disclosure. A forbidden row and a missing row must be
  indistinguishable.
* **Scoping is asserted per-verb, not once.** A read guard that a mutation path skips
  is not a boundary, and the version family funnels ~15 routes through one helper --
  so the risk of a missed verb is precisely what needs covering.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import CaliberWorkflow, CaliberWorkflowPromotion, CaliberWorkflowVersion
from tests.workflow_helpers import PREFIX, make_support_manifest

# A **second developer**: operator scope (so no scope check short-circuits the request)
# but *not* admin, in a *different* project.
#
# Both halves are load-bearing, and getting them wrong makes the test meaningless:
#
# * an under-privileged user would be refused by ``require_scopes`` with a 403 before
#   any row was resolved, proving scope enforcement rather than project isolation; and
# * an **admin** deliberately bypasses the visibility filter entirely
#   (``db/scoping.py`` short-circuits on ``SCOPE_ADMIN``), which is correct for the
#   single-organization target -- so an admin can never demonstrate this boundary.
#
# ``@dev2`` is therefore granted operator scope by the fixture below. That combination
# is exactly the deployment C3 describes: several developers in one organization, each
# able to operate their own project and none entitled to another's.
OTHER = {"X-CALIBER-User": "@dev2", "X-CALIBER-Project": "PRJ-OTHER"}


@pytest.fixture(autouse=True)
def _second_developer(client: TestClient) -> None:
    """Grant ``@dev2`` operator scope without making it an admin.

    The suite's default config puts every test identity in the admin list, which would
    bypass visibility. Narrowing it here is what lets these tests observe the predicate
    at all.
    """
    client.app.state.config = client.app.state.config.model_copy(
        update={"operator_users": "@dev2", "approver_users": "@dev2"}
    )


@pytest.fixture
def owned(db_session: Session) -> tuple[str, str]:
    """A project-scoped workflow with a published version. Returns (wid, vid)."""
    db_session.add(
        CaliberWorkflow(
            workflow_id="WF-OWNED",
            name="Owned",
            owner="@owner",
            project_id="PRJ-MINE",
            visibility="project",
        )
    )
    db_session.add(
        CaliberWorkflowVersion(
            version_id="WFV-OWNED",
            workflow_id="WF-OWNED",
            version_number=1,
            status="published",
            manifest=make_support_manifest("WF-OWNED"),
            manifest_hash="hash-owned",
        )
    )
    db_session.commit()
    return "WF-OWNED", "WFV-OWNED"


# ---------------------------------------------------------------------------
# Workflow versions — one chokepoint, many verbs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("get", "", None),
        ("get", "/manifest", None),
        ("post", "/publish", {}),
        ("post", "/validate", {}),
        ("post", "/compile", {}),
        # A *schema-valid* body: request-body validation runs before the row is
        # resolved, so an incomplete body returns 400 and would test parsing rather than
        # scoping. A 400 there discloses nothing about whether the row exists.
        (
            "patch",
            "",
            {"manifest": make_support_manifest("WF-OWNED"), "manifest_hash": "hash-owned"},
        ),
    ],
)
def test_a_foreign_version_is_404_for_every_verb(
    client: TestClient, owned: tuple[str, str], method: str, suffix: str, body: object
) -> None:
    """Every version route resolves through one helper, so all of them are covered by
    scoping it -- and all of them are asserted, because a single unscoped verb would
    reopen the finding for the whole family."""
    _wid, vid = owned
    call = getattr(client, method)
    kwargs = {"headers": OTHER}
    if body is not None:
        kwargs["json"] = body

    response = call(f"{PREFIX}/workflow-versions/{vid}{suffix}", **kwargs)

    assert response.status_code == 404, f"{method.upper()} {suffix or '/'} -> {response.text}"
    # The message must not confirm the row exists.
    assert "forbidden" not in response.text.lower()


def test_the_owner_still_reaches_their_own_version(
    client: TestClient, owned: tuple[str, str]
) -> None:
    """The guard has to stay narrow: scoping that also blocks the legitimate owner is a
    regression, not a fix."""
    _wid, vid = owned
    response = client.get(
        f"{PREFIX}/workflow-versions/{vid}", headers={"X-CALIBER-Project": "PRJ-MINE"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["version_id"] == vid


# ---------------------------------------------------------------------------
# Deployment control plane
# ---------------------------------------------------------------------------


def test_a_foreign_workflow_cannot_be_listed_or_promoted(
    client: TestClient, owned: tuple[str, str]
) -> None:
    """Promotion is the highest-consequence path in the product: it moves a live alias.
    A bare workflow id previously let a caller in another project rotate it."""
    wid, vid = owned

    listed = client.get(f"{PREFIX}/workflows/{wid}/deployments", headers=OTHER)
    assert listed.status_code == 404, listed.text

    promoted = client.post(
        f"{PREFIX}/workflows/{wid}/deployments/dev/promote",
        json={"version_id": vid},
        headers=OTHER,
    )
    assert promoted.status_code == 404, promoted.text


def test_a_foreign_workflow_cannot_be_rolled_back(
    client: TestClient, owned: tuple[str, str]
) -> None:
    wid, _vid = owned
    response = client.post(f"{PREFIX}/workflows/{wid}/deployments/dev/rollback", headers=OTHER)
    assert response.status_code == 404, response.text


def test_a_foreign_promotion_cannot_be_approved_or_rejected(
    client: TestClient, db_session: Session, owned: tuple[str, str]
) -> None:
    """Approve/reject fetched by promotion id with no project scoping at all, so a
    known id let someone with no access to a workflow sign off on releasing it."""
    wid, vid = owned
    db_session.add(
        CaliberWorkflowPromotion(
            promotion_id="WP-OWNED",
            workflow_id=wid,
            alias="prod",
            version_id=vid,
            status="pending",
            gate_result={},
            requested_by="@owner",
        )
    )
    db_session.commit()

    approved = client.post(f"{PREFIX}/workflow-promotions/WP-OWNED/approve", headers=OTHER)
    assert approved.status_code == 404, approved.text

    rejected = client.post(
        f"{PREFIX}/workflow-promotions/WP-OWNED/reject",
        json={"reason": "no"},
        headers=OTHER,
    )
    assert rejected.status_code == 404, rejected.text

    # And the promotion is untouched — a refused decision must not half-apply.
    db_session.expire_all()
    assert db_session.get(CaliberWorkflowPromotion, "WP-OWNED").status == "pending"


def test_an_unknown_id_and_a_forbidden_id_are_indistinguishable(
    client: TestClient, owned: tuple[str, str]
) -> None:
    """The disclosure property, asserted directly rather than implied: the response for
    a real-but-forbidden version must match the response for one that does not exist."""
    _wid, vid = owned

    forbidden = client.get(f"{PREFIX}/workflow-versions/{vid}", headers=OTHER)
    missing = client.get(f"{PREFIX}/workflow-versions/WFV-DOES-NOT-EXIST", headers=OTHER)

    assert forbidden.status_code == missing.status_code == 404
    # Same shape of message; only the echoed id differs.
    assert forbidden.json()["detail"].replace(vid, "X") == missing.json()["detail"].replace(
        "WFV-DOES-NOT-EXIST", "X"
    )


def test_an_orphaned_child_fails_closed(client: TestClient, db_session: Session) -> None:
    """A version whose parent workflow is gone must 404 rather than being treated as
    unscoped. Failing open on a dangling foreign key would turn data corruption into an
    access-control bypass."""
    db_session.add(
        CaliberWorkflowVersion(
            version_id="WFV-ORPHAN",
            workflow_id="WF-VANISHED",
            version_number=1,
            status="published",
            manifest=make_support_manifest("WF-VANISHED"),
            manifest_hash="hash-orphan",
        )
    )
    db_session.commit()

    response = client.get(f"{PREFIX}/workflow-versions/WFV-ORPHAN")
    assert response.status_code == 404, response.text


def test_a_foreign_workflows_versions_cannot_be_listed_or_created(
    client: TestClient, owned: tuple[str, str]
) -> None:
    """Scoping the version-*detail* chokepoint was not enough, and an independent probe
    caught it: the by-parent families resolved the workflow id directly, so a foreign
    operator was refused a specific version (404) yet still got **200** listing every
    version of that workflow and **201** creating a new one under it.

    That asymmetry is worse than a uniformly open route, because the 404 on detail
    reads as evidence the boundary works.
    """
    wid, _vid = owned

    listed = client.get(f"{PREFIX}/workflows/{wid}/versions", headers=OTHER)
    assert listed.status_code == 404, listed.text

    created = client.post(
        f"{PREFIX}/workflows/{wid}/versions",
        json={"manifest": make_support_manifest(wid)},
        headers=OTHER,
    )
    assert created.status_code == 404, created.text


def test_the_owner_can_still_list_and_create_versions(
    client: TestClient, owned: tuple[str, str]
) -> None:
    """The guard must not lock out the legitimate owner."""
    wid, _vid = owned
    mine = {"X-CALIBER-Project": "PRJ-MINE"}

    listed = client.get(f"{PREFIX}/workflows/{wid}/versions", headers=mine)
    assert listed.status_code == 200, listed.text
    assert any(v["version_id"] == "WFV-OWNED" for v in listed.json()["data"])
