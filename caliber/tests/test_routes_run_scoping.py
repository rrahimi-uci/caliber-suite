"""C3: a known run id must not cross a project boundary.

The remaining half of the C3 finding. The release control plane was scoped in
``test_routes_release_scoping.py``; run *detail* was not. ``_get_run_or_404`` was a bare
``session.get``, and twelve routes funnelled through it, so a caller who knew a run id
could read another project's:

* run detail, lineage, and manifest;
* execution **trace** and event stream — the richest disclosure here, since a trace
  carries node inputs and outputs, which is where customer data actually sits;
* checkpoints and approval records;

and could *mutate* it: cancel, retry, resume, approve, reject.

Why these tests are the closure rather than the fix being the closure: the fix is
invisible to a positive test. Every pre-existing suite passes with or without it, because
they all read runs the caller owns. The property under test is what happens to a caller
who should not reach a row and knows its id anyway.

Conventions, both deliberate and both mirroring the release-scoping suite:

* **404, not 403.** "Exists but forbidden" confirms the id is real, which is itself the
  disclosure. A forbidden row and a missing row must be indistinguishable.
* **Per-verb, not once.** A read guard a mutation path skips is not a boundary. Twelve
  routes share one helper, so a single missed verb reopens the finding for the family —
  which is exactly the risk worth covering.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberWorkflow,
    CaliberWorkflowDeployment,
    CaliberWorkflowRun,
    CaliberWorkflowVersion,
)
from tests.workflow_helpers import PREFIX, make_support_manifest

# A second developer: operator scope so no scope check short-circuits the request, but
# *not* admin — ``db/scoping.py`` short-circuits on SCOPE_ADMIN, so an admin can never
# demonstrate this boundary. In a different project. See the release-scoping suite for the
# full rationale; the same reasoning applies verbatim.
OTHER = {"X-CALIBER-User": "@dev2", "X-CALIBER-Project": "PRJ-OTHER"}
#: A developer who *is* in the owning project, and is deliberately **not** the default
#: ``@test`` identity. ``conftest`` puts ``@test`` in ``CALIBER_ADMIN_USERS``
#: (``_PERMISSIVE_TEST_USERS``), and ``db/scoping.py`` short-circuits on admin — so a
#: positive assertion made as ``@test`` would pass through the admin bypass and prove
#: nothing about project scoping. ``@dev3`` reaches the run because it shares the project.
MINE = {"X-CALIBER-User": "@dev3", "X-CALIBER-Project": "PRJ-MINE"}


@pytest.fixture(autouse=True)
def _second_developer(client: TestClient) -> None:
    """Grant ``@dev2``/``@dev3`` operator scope without making either an admin.

    Neither is in ``_PERMISSIVE_TEST_USERS``, so neither inherits the admin bypass that
    would make every assertion here vacuous. Operator scope is still required, or
    ``require_scopes`` would return 403 before any row was resolved — which would prove
    scope enforcement rather than project isolation.

    The queue is enabled for the same reason: ``cancel``/``retry``/``resume`` check the
    feature gate *before* resolving the row, so with it off they return 409 and the request
    never reaches the scoping guard under test.
    """
    client.app.state.config = client.app.state.config.model_copy(
        update={
            "operator_users": "@dev2,@dev3",
            "approver_users": "@dev2,@dev3",
            "workflow_run_queue_enabled": True,
            "workflow_run_checkpointing_enabled": True,
        }
    )


@pytest.fixture
def foreign_run(db_session: Session) -> str:
    """A completed run inside a project the caller is not a member of."""
    # Owned by ``@dev3``, deliberately. For a non-admin the *project* tier in
    # ``apply_visibility_filter`` requires the project to match **and** the row to be owned
    # by the caller — "project" visibility is not project-wide read for non-owners. Owning
    # it by ``@dev3`` is therefore what makes the positive assertion below reachable at all.
    db_session.add(
        CaliberWorkflow(
            workflow_id="WF-RUNS",
            name="Runs",
            owner="@dev3",
            project_id="PRJ-MINE",
            visibility="project",
        )
    )
    db_session.add(
        CaliberWorkflowVersion(
            version_id="WFV-RUNS",
            workflow_id="WF-RUNS",
            version_number=1,
            status="published",
            manifest=make_support_manifest("WF-RUNS"),
            manifest_hash="hash-runs",
        )
    )
    db_session.add(
        CaliberWorkflowRun(
            workflow_run_id="WFR-SECRET",
            workflow_id="WF-RUNS",
            workflow_version_id="WFV-RUNS",
            status="completed",
            attempt_number=1,
            summary={"output": "customer data that must not leak"},
        )
    )
    db_session.commit()
    return "WFR-SECRET"


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "suffix",
    [
        "",
        "/lineage",
        "/manifest",
        "/events",
        "/trace",
        "/checkpoints",
        "/approvals",
    ],
)
def test_a_foreign_runs_reads_are_all_404(
    client: TestClient, foreign_run: str, suffix: str
) -> None:
    """Every read route resolves through the one helper, and every one is asserted."""
    response = client.get(f"{PREFIX}/workflow-runs/{foreign_run}{suffix}", headers=OTHER)

    assert response.status_code == 404, f"GET {suffix or '/'} -> {response.text}"
    assert "forbidden" not in response.text.lower()


def test_the_trace_does_not_leak_run_output_in_the_error(
    client: TestClient, foreign_run: str
) -> None:
    """The trace is the highest-value target here: node inputs and outputs are where
    customer data sits. Neither the body nor the status may hint that the run exists."""
    response = client.get(f"{PREFIX}/workflow-runs/{foreign_run}/trace", headers=OTHER)

    assert response.status_code == 404
    assert "customer data" not in response.text


# ---------------------------------------------------------------------------
# Mutations — a read guard a write path skips is not a boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("post", "/cancel", {}),
        ("post", "/retry", {}),
        ("post", "/resume", {}),
    ],
)
def test_a_foreign_run_cannot_be_mutated(
    client: TestClient, foreign_run: str, method: str, suffix: str, body: object
) -> None:
    """Cancel, retry, and resume all change another project's run state. A 409 here would
    be a failure too: it would mean the row was resolved before the guard ran."""
    call = getattr(client, method)

    response = call(f"{PREFIX}/workflow-runs/{foreign_run}{suffix}", json=body, headers=OTHER)

    assert response.status_code == 404, f"{method.upper()} {suffix} -> {response.text}"


# ---------------------------------------------------------------------------
# The guard must stay narrow
# ---------------------------------------------------------------------------


def test_a_developer_in_the_owning_project_still_reaches_the_run(
    client: TestClient, foreign_run: str
) -> None:
    """Scoping that also blocks the legitimate caller is a regression, not a fix — this is
    the assertion that catches a helper which simply 404s everything.

    Made as ``@dev3``, a **non-admin** in ``PRJ-MINE``, so it demonstrates project
    membership granting access rather than the admin short-circuit granting it.
    """
    response = client.get(f"{PREFIX}/workflow-runs/{foreign_run}", headers=MINE)

    assert response.status_code == 200, response.text
    assert response.json()["data"]["workflow_run_id"] == foreign_run


def test_a_foreign_runs_files_cannot_be_listed_or_downloaded(
    client: TestClient, foreign_run: str
) -> None:
    """The file routes are the most consequential instance of this pattern.

    ``routes/files.py`` has an IDOR guard tying a file to its run — but that guard is
    worthless while the run itself is reachable by anyone who knows its id, because the
    file genuinely *does* belong to that run, so the check passes and the bytes are served.
    Scoping the run is what makes the IDOR guard mean something.
    """
    listed = client.get(f"{PREFIX}/workflow-runs/{foreign_run}/files", headers=OTHER)
    assert listed.status_code == 404, listed.text

    # The download proxy, asserted separately: it is the route that actually returns
    # content, and a guard the read path has but the download path skips is not a guard.
    content = client.get(
        f"{PREFIX}/workflow-runs/{foreign_run}/files/WFF-anything/content", headers=OTHER
    )
    assert content.status_code == 404, content.text


@pytest.fixture
def foreign_judge(db_session: Session) -> str:
    """A judge in a project the caller is not a member of."""
    from caliber.db.models import CaliberJudge

    db_session.add(
        CaliberJudge(
            judge_id="JDG-SECRET",
            name="secret-judge",
            instructions="Grade {{ inputs }} against our proprietary rubric.",
            owner="@owner",
            project_id="PRJ-MINE",
            visibility="project",
        )
    )
    db_session.commit()
    return "JDG-SECRET"


@pytest.mark.parametrize(
    ("suffix", "body"),
    [
        # A *schema-valid* body per route: request-body validation runs before the row is
        # resolved, so a wrong shape returns 400 and would test parsing rather than scoping.
        # ``inputs`` is a dict and ``outputs`` a string; the alignment route wraps both in
        # ``examples``.
        ("/test-run", {"inputs": {"q": "x"}, "outputs": "y"}),
        (
            "/alignment",
            {"examples": [{"inputs": {"q": "x"}, "outputs": "y", "label": True}]},
        ),
    ],
)
def test_a_foreign_judge_cannot_be_run_or_aligned(
    client: TestClient, foreign_judge: str, suffix: str, body: object
) -> None:
    """``GET /judges/{id}`` was scoped through ``get_visible``; these two were not, while
    requiring only ``require_user``.

    A judge's ``instructions`` are its whole substance — the authored prompt, which is the
    evaluation IP. An unscoped read let any signed-in caller execute another project's judge
    on inputs of their choosing, which both discloses the rubric's behaviour and spends the
    owner's model budget. A read guard that two execution paths skip is not a guard.
    """
    response = client.post(f"{PREFIX}/judges/{foreign_judge}{suffix}", json=body, headers=OTHER)

    # 404, and specifically not 400 (which would mean the body was rejected before the row
    # was resolved, testing parsing rather than scoping) nor 500 (which would mean we built
    # a judge we should never have resolved).
    assert response.status_code == 404, f"POST {suffix} -> {response.text}"
    assert "proprietary rubric" not in response.text


def test_a_foreign_workflow_cannot_be_queued_for_a_run(
    client: TestClient, foreign_run: str
) -> None:
    """Queueing is worse than reading: it executes someone else's graph on their providers.

    `_workflow_and_version_for_run` used bare primary-key reads for both the version's
    parent and the direct workflow lookup, and an independent probe observed **202
    Accepted** for a foreign workflow ID.
    """
    client.app.state.config = client.app.state.config.model_copy(
        update={
            "operator_users": "@dev2,@dev3",
            "approver_users": "@dev2,@dev3",
            "workflow_run_queue_enabled": True,
        }
    )

    by_workflow = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_id": "WF-RUNS", "input": "hi"},
        headers=OTHER,
    )
    assert by_workflow.status_code == 404, by_workflow.text

    by_version = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": "WFV-RUNS", "input": "hi"},
        headers=OTHER,
    )
    assert by_version.status_code == 404, by_version.text

    missing_version = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_version_id": "WFV-DOES-NOT-EXIST", "input": "hi"},
        headers=OTHER,
    )
    assert missing_version.status_code == 404, missing_version.text
    # A bare version lookup is used only to locate the parent for visibility checking.
    # If that parent is forbidden, naming it in the response confirms the version exists
    # and reveals a second foreign identifier. Forbidden and missing versions must have
    # the same response shape, differing only in the caller-supplied version id.
    assert "WF-RUNS" not in by_version.text
    assert by_version.text.replace("WFV-RUNS", "X") == missing_version.text.replace(
        "WFV-DOES-NOT-EXIST", "X"
    )


def test_an_event_cannot_resume_a_foreign_waiting_run(client: TestClient, foreign_run: str) -> None:
    """Resume-by-event scanned every waiting run in the database.

    Two problems, not one: an external event could resume another project's run, and the
    409 diagnostics echo run IDs, so even a non-matching event enumerated foreign runs. The
    query is now restricted to workflows the caller can see.
    """
    client.app.state.config = client.app.state.config.model_copy(
        update={
            "operator_users": "@dev2,@dev3",
            "approver_users": "@dev2,@dev3",
            "workflow_run_queue_enabled": True,
            "workflow_run_checkpointing_enabled": True,
        }
    )

    response = client.post(
        f"{PREFIX}/workflow-runs/resume-by-event",
        json={"event_name": "anything", "event_payload": {}},
        headers=OTHER,
    )

    # Whatever the outcome, no foreign run id may appear in the body.
    assert foreign_run not in response.text, "resume-by-event enumerated a foreign run"


@pytest.mark.parametrize("body", [{"event_name": "secret.event"}, {"alias": "prod"}])
def test_an_event_trigger_cannot_start_a_foreign_workflow(
    client: TestClient, db_session: Session, body: dict[str, str]
) -> None:
    """Both trigger resolver branches must scope the workflow before deployment reads.

    Omitting ``alias`` scans active deployments; providing it goes through the general
    run-creation resolver. The remediation changed both branches but originally added no
    foreign-workflow regression for this route, so either one could drift back to the old
    bare lookup while the other stayed green.
    """
    manifest = make_support_manifest("WF-EVENT-SECRET")
    manifest["nodes"]["start"]["trigger"] = {
        "mode": "event",
        "event_name": "secret.event",
        "alias": "prod",
    }
    db_session.add(
        CaliberWorkflow(
            workflow_id="WF-EVENT-SECRET",
            name="Foreign event workflow",
            owner="@dev3",
            project_id="PRJ-MINE",
            visibility="project",
        )
    )
    db_session.add(
        CaliberWorkflowVersion(
            version_id="WFV-EVENT-SECRET",
            workflow_id="WF-EVENT-SECRET",
            version_number=1,
            status="published",
            manifest=manifest,
            manifest_hash="hash-event-secret",
        )
    )
    db_session.add(
        CaliberWorkflowDeployment(
            deployment_id="WFD-EVENT-SECRET",
            workflow_id="WF-EVENT-SECRET",
            alias="prod",
            version_id="WFV-EVENT-SECRET",
            status="active",
            deployed_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()

    response = client.post(
        f"{PREFIX}/workflows/WF-EVENT-SECRET/trigger",
        json=body,
        headers=OTHER,
    )

    assert response.status_code == 404, response.text
    run_count = db_session.scalar(
        select(func.count())
        .select_from(CaliberWorkflowRun)
        .where(CaliberWorkflowRun.workflow_id == "WF-EVENT-SECRET")
    )
    assert run_count == 0, "a foreign event trigger created a run before authorization"


def test_a_playground_runs_files_are_private_to_their_uploader(client: TestClient) -> None:
    """A playground run has no parent workflow, so it is scoped by uploader.

    The list and download routes filtered on nothing but the caller-supplied
    `playground_run_id`, so any authenticated caller who knew or guessed one could read
    another user's files — an independent probe observed 200 on both. `project_id` cannot
    carry this: playground uploads default it to `'default'`, so it isolates nobody.
    """
    upload = client.post(
        f"{PREFIX}/playground-runs/PG-secret/files",
        files={"file": ("notes.txt", b"my private scratch", "text/plain")},
        data={"kind": "input"},
    )
    assert upload.status_code == 201, upload.text
    file_id = upload.json()["data"]["file_id"]

    # The uploader still sees it — the guard must not block the legitimate caller.
    mine = client.get(f"{PREFIX}/playground-runs/PG-secret/files")
    assert mine.status_code == 200
    assert [i["file_id"] for i in mine.json()["data"]["items"]] == [file_id]

    # A different developer, knowing the run ID, must see nothing and download nothing.
    listed = client.get(f"{PREFIX}/playground-runs/PG-secret/files", headers=OTHER)
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"]["items"] == [], "another user's playground files leaked"

    content = client.get(
        f"{PREFIX}/playground-runs/PG-secret/files/{file_id}/content", headers=OTHER
    )
    assert content.status_code == 404, content.text
    assert b"private scratch" not in content.content


def test_a_duplicate_judge_name_does_not_disclose_the_conflicting_id(
    client: TestClient, foreign_judge: str
) -> None:
    """`uq_judge_name` is global, so a name collision can cross a project boundary.

    The 409 used to echo the conflicting `judge_id`, handing the caller an identifier from
    a project they cannot see — which is then usable against any route still taking a bare
    ID. The caller needs to know the name is taken, not whose it is.
    """
    response = client.post(
        f"{PREFIX}/judges",
        json={
            "name": "secret-judge",
            "instructions": "Grade {{ inputs }} somehow.",
            "model": "openai:/gpt-4o-mini",
        },
        headers=OTHER,
    )

    assert response.status_code == 409, response.text
    assert "already in use" in response.text
    assert foreign_judge not in response.text, "the 409 disclosed a foreign judge id"


def test_an_unknown_run_id_is_also_404_and_indistinguishable(
    client: TestClient, foreign_run: str
) -> None:
    """The forbidden and missing cases must produce the same status *and* shape, or the
    difference between them is the disclosure."""
    forbidden = client.get(f"{PREFIX}/workflow-runs/{foreign_run}", headers=OTHER)
    missing = client.get(f"{PREFIX}/workflow-runs/WFR-DOES-NOT-EXIST", headers=OTHER)

    assert forbidden.status_code == missing.status_code == 404
    # Same phrasing, differing only in the id echoed back.
    assert forbidden.text.replace(foreign_run, "X") == missing.text.replace(
        "WFR-DOES-NOT-EXIST", "X"
    )
