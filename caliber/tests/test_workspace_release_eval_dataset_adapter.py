"""`P4-B`/`P4-C`: the fourth real Workspace resource adapter -- a CALIBER
eval dataset.

Mirrors ``test_workspace_release_judge_adapter.py``'s pattern (proving the
:class:`~caliber.workspace_release_adapters.WorkspaceResourceAdapter`
Protocol against a genuine resource type), but for a *collection* resource
with a real, explicit version number -- see
``workspace_release_eval_dataset_adapter.py``'s module docstring for why an
eval dataset's ``version_ref`` is an explicit caller-supplied integer (not
the judge's ``"current"`` sentinel, and not a fabricated digest), and why
``content_sha256`` hashes the reconstructed "as of version N" example set
rather than the dataset row's own metadata columns.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from caliber.auth import SCOPE_ADMIN, CaliberIdentity
from caliber.db.models import (
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberProject,
    CaliberProjectMember,
    CaliberWorkspaceEnvironment,
    CaliberWorkspaceRevisionResource,
)
from caliber.workspace_release_adapters import (
    PreparedAction,
    SnapshotPin,
    WorkspaceReleaseAdapterError,
)
from caliber.workspace_release_eval_dataset_adapter import (
    EVAL_DATASET_ADAPTER_VERSION,
    EvalDatasetWorkspaceResourceAdapter,
    ResolvedEvalDataset,
    _content_sha256,
)

PROJECT_ID = "PRJ-eval-dataset-adapter"
OTHER_PROJECT_ID = "PRJ-eval-dataset-adapter-other"
ADAPTER = EvalDatasetWorkspaceResourceAdapter()


def _identity(
    user_id: str = "@caller", *, active_project_id: str | None = None, admin: bool = False
) -> CaliberIdentity:
    return CaliberIdentity(
        user_id=user_id,
        scopes=frozenset({SCOPE_ADMIN}) if admin else frozenset(),
        active_project_id=active_project_id,
    )


def _seed_project(session: Session, *, project_id: str = PROJECT_ID) -> None:
    session.add(
        CaliberProject(
            project_id=project_id, name=f"Eval dataset adapter {project_id}", owner="admin"
        )
    )
    session.flush()


def _seed_dataset(
    session: Session,
    *,
    dataset_id: str,
    project_id: str | None,
    owner: str = "@test",
    visibility: str | None = None,
    name: str | None = None,
    version: int = 1,
    status: str = "active",
) -> CaliberEvalDataset:
    dataset = CaliberEvalDataset(
        dataset_id=dataset_id,
        name=name or dataset_id,
        description="",
        owner=owner,
        project_id=project_id,
        visibility=visibility or ("project" if project_id else "user"),
        tags=[],
        status=status,
        version=version,
    )
    session.add(dataset)
    session.flush()
    return dataset


def _add_example(
    session: Session,
    *,
    example_id: str,
    dataset_id: str,
    dataset_version: int,
    input: dict[str, object] | None = None,
    expected: dict[str, object] | None = None,
    weight: float = 1.0,
    tags: list[str] | None = None,
    superseded_version: int | None = None,
) -> CaliberEvalDatasetExample:
    example = CaliberEvalDatasetExample(
        example_id=example_id,
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        input=input if input is not None else {"q": example_id},
        expected=expected if expected is not None else {"a": example_id},
        weight=weight,
        tags=tags or [],
    )
    if superseded_version is not None:
        example.superseded_at = datetime.now(timezone.utc)
        example.superseded_version = superseded_version
    session.add(example)
    session.flush()
    return example


# -- resolve --------------------------------------------------------------


def test_resolve_loads_the_dataset_and_its_examples_as_of_the_pinned_version(
    db_session: Session,
) -> None:
    _seed_project(db_session)
    _seed_dataset(db_session, dataset_id="ED-1", project_id=PROJECT_ID, owner="@caller", version=2)
    _add_example(db_session, example_id="EX-1", dataset_id="ED-1", dataset_version=1)
    _add_example(db_session, example_id="EX-2", dataset_id="ED-1", dataset_version=2)
    project = db_session.get(CaliberProject, PROJECT_ID)

    resolved = ADAPTER.resolve(
        db_session,
        project,
        {"resource_id": "ED-1", "version_ref": "2"},
        _identity("@caller", active_project_id=PROJECT_ID),
    )
    assert isinstance(resolved, ResolvedEvalDataset)
    assert resolved.dataset_id == "ED-1"
    assert resolved.name == "ED-1"
    assert resolved.version == 2
    assert [e["example_id"] for e in resolved.examples] == ["EX-1", "EX-2"]
    assert resolved.provider_ref == "caliber-eval-dataset:/ED-1@2"


def test_resolve_reconstructs_a_historical_version_excluding_later_and_retired_examples(
    db_session: Session,
) -> None:
    """The core collection-shape behavior: pinning an older version must not
    include examples added later, and must still include examples that were
    active at that version even if since retired."""
    _seed_project(db_session)
    _seed_dataset(
        db_session, dataset_id="ED-hist", project_id=PROJECT_ID, owner="@caller", version=3
    )
    _add_example(db_session, example_id="EX-1", dataset_id="ED-hist", dataset_version=1)
    _add_example(
        db_session,
        example_id="EX-2",
        dataset_id="ED-hist",
        dataset_version=2,
        superseded_version=3,
    )
    _add_example(db_session, example_id="EX-3", dataset_id="ED-hist", dataset_version=3)

    # As of version 2: EX-1 (added v1) and EX-2 (added v2, retired at v3 --
    # still active as of v2) but not EX-3 (added v3, doesn't exist yet).
    resolved = ADAPTER.resolve(
        db_session,
        None,
        {"resource_id": "ED-hist", "version_ref": "2"},
        _identity("@caller", active_project_id=PROJECT_ID),
    )
    assert [e["example_id"] for e in resolved.examples] == ["EX-1", "EX-2"]

    # As of version 3: EX-1 and EX-3 (EX-2 retired exactly at v3).
    resolved_v3 = ADAPTER.resolve(
        db_session,
        None,
        {"resource_id": "ED-hist", "version_ref": "3"},
        _identity("@caller", active_project_id=PROJECT_ID),
    )
    assert [e["example_id"] for e in resolved_v3.examples] == ["EX-1", "EX-3"]


def test_resolve_requires_resource_id_and_version_ref(db_session: Session) -> None:
    identity = _identity()
    with pytest.raises(WorkspaceReleaseAdapterError, match="requires"):
        ADAPTER.resolve(db_session, None, {"resource_id": "x"}, identity)
    with pytest.raises(WorkspaceReleaseAdapterError, match="requires"):
        ADAPTER.resolve(db_session, None, {}, identity)
    with pytest.raises(WorkspaceReleaseAdapterError):
        ADAPTER.resolve(db_session, None, "not-a-mapping", identity)  # type: ignore[arg-type]


def test_resolve_rejects_a_non_integer_version_ref(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_dataset(db_session, dataset_id="ED-1", project_id=PROJECT_ID)
    with pytest.raises(WorkspaceReleaseAdapterError, match="must be an integer"):
        ADAPTER.resolve(
            db_session,
            None,
            {"resource_id": "ED-1", "version_ref": "not-a-number"},
            _identity("@caller", active_project_id=PROJECT_ID),
        )


def test_resolve_rejects_a_non_positive_version_ref(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_dataset(db_session, dataset_id="ED-1", project_id=PROJECT_ID)
    with pytest.raises(WorkspaceReleaseAdapterError, match="positive integer"):
        ADAPTER.resolve(
            db_session,
            None,
            {"resource_id": "ED-1", "version_ref": "0"},
            _identity("@caller", active_project_id=PROJECT_ID),
        )


def test_resolve_rejects_a_version_not_yet_reached(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_dataset(db_session, dataset_id="ED-1", project_id=PROJECT_ID, owner="@caller", version=1)
    with pytest.raises(WorkspaceReleaseAdapterError, match="has no version 5"):
        ADAPTER.resolve(
            db_session,
            None,
            {"resource_id": "ED-1", "version_ref": "5"},
            _identity("@caller", active_project_id=PROJECT_ID),
        )


def test_resolve_raises_when_the_dataset_does_not_exist(db_session: Session) -> None:
    with pytest.raises(WorkspaceReleaseAdapterError, match="not found"):
        ADAPTER.resolve(
            db_session,
            None,
            {"resource_id": "ED-ghost", "version_ref": "1"},
            _identity(),
        )


def test_resolve_refuses_a_dataset_bound_to_a_different_project(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_project(db_session, project_id=OTHER_PROJECT_ID)
    _seed_dataset(db_session, dataset_id="ED-other", project_id=OTHER_PROJECT_ID, owner="@owner")
    caller = _identity("@caller", active_project_id=PROJECT_ID)
    with pytest.raises(WorkspaceReleaseAdapterError, match="not found or not visible"):
        ADAPTER.resolve(
            db_session,
            None,
            {"resource_id": "ED-other", "version_ref": "1"},
            caller,
        )


def test_resolve_allows_a_project_scoped_dataset_for_an_active_member(db_session: Session) -> None:
    """The positive side of the project-tier check: a caller who is an
    active member of the target's own project (not its owner) can still
    resolve it -- proving this reuses the *real* visibility model
    (owner-or-member), not a stricter "owner only" shortcut."""
    _seed_project(db_session)
    _seed_dataset(db_session, dataset_id="ED-team", project_id=PROJECT_ID, owner="@owner")
    db_session.add(
        CaliberProjectMember(
            member_id="PM-eval-dataset-adapter-1",
            project_id=PROJECT_ID,
            user_id="@teammate",
            role="editor",
            status="active",
            created_by="@owner",
        )
    )
    db_session.flush()
    member = _identity("@teammate", active_project_id=PROJECT_ID)
    resolved = ADAPTER.resolve(
        db_session,
        None,
        {"resource_id": "ED-team", "version_ref": "1"},
        member,
    )
    assert resolved.dataset_id == "ED-team"


def test_resolve_refuses_another_users_unshared_personal_dataset(db_session: Session) -> None:
    """The actual disclosure this design closes from the start: a personal
    dataset (``project_id=None``, ``visibility="user"``) has the *same*
    ``None`` project id as a public dataset, so a bare ``dataset.project_id
    != caller's project`` comparison could not tell them apart and would let
    any operator on any project snapshot another user's unshared personal
    dataset by id. Must be refused, the same way
    ``routes/eval_datasets.py``'s lookup routes already refuse it."""
    _seed_project(db_session)
    _seed_dataset(db_session, dataset_id="ED-personal", project_id=None, owner="@someone-else")
    other_user = _identity("@a-different-user", active_project_id=PROJECT_ID)
    with pytest.raises(WorkspaceReleaseAdapterError, match="not found or not visible"):
        ADAPTER.resolve(
            db_session,
            None,
            {"resource_id": "ED-personal", "version_ref": "1"},
            other_user,
        )


def test_resolve_allows_the_owners_own_personal_dataset(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_dataset(db_session, dataset_id="ED-mine", project_id=None, owner="@owner-self")
    owner = _identity("@owner-self", active_project_id=PROJECT_ID)
    resolved = ADAPTER.resolve(
        db_session,
        None,
        {"resource_id": "ED-mine", "version_ref": "1"},
        owner,
    )
    assert resolved.dataset_id == "ED-mine"


def test_resolve_allows_a_public_dataset_for_any_caller(db_session: Session) -> None:
    """A genuinely public dataset also has ``project_id=None`` -- proving
    this allows that tier too, not just refusing everything with a ``None``
    project id."""
    _seed_project(db_session)
    _seed_dataset(
        db_session,
        dataset_id="ED-public",
        project_id=None,
        owner="@publisher",
        visibility="public",
    )
    stranger = _identity("@total-stranger", active_project_id=PROJECT_ID)
    resolved = ADAPTER.resolve(
        db_session,
        None,
        {"resource_id": "ED-public", "version_ref": "1"},
        stranger,
    )
    assert resolved.dataset_id == "ED-public"


def test_resolve_admin_bypasses_visibility(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_dataset(db_session, dataset_id="ED-personal", project_id=None, owner="@someone-else")
    admin = _identity("@platform-admin", active_project_id=PROJECT_ID, admin=True)
    resolved = ADAPTER.resolve(
        db_session,
        None,
        {"resource_id": "ED-personal", "version_ref": "1"},
        admin,
    )
    assert resolved.dataset_id == "ED-personal"


# -- snapshot ---------------------------------------------------------------


def test_snapshot_computes_a_genuine_content_digest() -> None:
    resolved = ResolvedEvalDataset(
        dataset_id="ED-1",
        name="regression-set",
        version=2,
        examples=(
            {
                "example_id": "EX-1",
                "input": {"q": "a"},
                "expected": {"a": "b"},
                "weight": 1.0,
                "tags": [],
            },
        ),
        provider_ref="caliber-eval-dataset:/ED-1@2",
    )
    pin = ADAPTER.snapshot(None, resolved)
    assert isinstance(pin, SnapshotPin)
    assert pin.resource_id == "ED-1"
    assert pin.version_ref == "2"
    assert pin.content_sha256 == _content_sha256(resolved.examples)
    assert pin.provider_ref == "caliber-eval-dataset:/ED-1@2"
    assert pin.resolution["strategy"] == "live_resource_adapter"
    assert pin.resolution["adapter_version"] == EVAL_DATASET_ADAPTER_VERSION
    assert pin.resolution["example_count"] == 1


def test_snapshot_distinguishes_an_added_example(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_dataset(db_session, dataset_id="ED-1", project_id=PROJECT_ID, owner="@caller", version=2)
    _add_example(db_session, example_id="EX-1", dataset_id="ED-1", dataset_version=1)
    _add_example(db_session, example_id="EX-2", dataset_id="ED-1", dataset_version=2)
    identity = _identity("@caller", active_project_id=PROJECT_ID)

    before = ADAPTER.snapshot(
        None,
        ADAPTER.resolve(db_session, None, {"resource_id": "ED-1", "version_ref": "1"}, identity),
    )
    after = ADAPTER.snapshot(
        None,
        ADAPTER.resolve(db_session, None, {"resource_id": "ED-1", "version_ref": "2"}, identity),
    )
    assert before.content_sha256 != after.content_sha256


def test_snapshot_distinguishes_a_removed_example(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_dataset(db_session, dataset_id="ED-2", project_id=PROJECT_ID, owner="@caller", version=3)
    _add_example(db_session, example_id="EX-1", dataset_id="ED-2", dataset_version=1)
    _add_example(
        db_session,
        example_id="EX-2",
        dataset_id="ED-2",
        dataset_version=2,
        superseded_version=3,
    )
    identity = _identity("@caller", active_project_id=PROJECT_ID)

    with_both = ADAPTER.snapshot(
        None,
        ADAPTER.resolve(db_session, None, {"resource_id": "ED-2", "version_ref": "2"}, identity),
    )
    after_supersede = ADAPTER.snapshot(
        None,
        ADAPTER.resolve(db_session, None, {"resource_id": "ED-2", "version_ref": "3"}, identity),
    )
    assert with_both.content_sha256 != after_supersede.content_sha256


def test_snapshot_distinguishes_edited_example_content() -> None:
    a = ADAPTER.snapshot(
        None,
        ResolvedEvalDataset(
            dataset_id="ED-1",
            name="n",
            version=1,
            examples=(
                {
                    "example_id": "EX-1",
                    "input": {"q": "a"},
                    "expected": {"a": "1"},
                    "weight": 1.0,
                    "tags": [],
                },
            ),
            provider_ref="ref",
        ),
    )
    b = ADAPTER.snapshot(
        None,
        ResolvedEvalDataset(
            dataset_id="ED-1",
            name="n",
            version=1,
            examples=(
                {
                    "example_id": "EX-1",
                    "input": {"q": "a"},
                    "expected": {"a": "2"},
                    "weight": 1.0,
                    "tags": [],
                },
            ),
            provider_ref="ref",
        ),
    )
    assert a.content_sha256 != b.content_sha256


def test_snapshot_distinguishes_weight_and_tags() -> None:
    base = ResolvedEvalDataset(
        dataset_id="ED-1",
        name="n",
        version=1,
        examples=({"example_id": "EX-1", "input": {}, "expected": {}, "weight": 1.0, "tags": []},),
        provider_ref="ref",
    )
    other_weight = ResolvedEvalDataset(
        dataset_id="ED-1",
        name="n",
        version=1,
        examples=({"example_id": "EX-1", "input": {}, "expected": {}, "weight": 2.0, "tags": []},),
        provider_ref="ref",
    )
    other_tags = ResolvedEvalDataset(
        dataset_id="ED-1",
        name="n",
        version=1,
        examples=(
            {"example_id": "EX-1", "input": {}, "expected": {}, "weight": 1.0, "tags": ["x"]},
        ),
        provider_ref="ref",
    )
    a = ADAPTER.snapshot(None, base)
    b = ADAPTER.snapshot(None, other_weight)
    c = ADAPTER.snapshot(None, other_tags)
    assert a.content_sha256 != b.content_sha256
    assert a.content_sha256 != c.content_sha256


def test_snapshot_rejects_a_non_resolved_input() -> None:
    with pytest.raises(WorkspaceReleaseAdapterError, match="resolved eval dataset"):
        ADAPTER.snapshot(None, {"not": "resolved"})


# -- validate -----------------------------------------------------------


def test_validate_accepts_a_pin(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_dataset(db_session, dataset_id="ED-validate-1", project_id=PROJECT_ID, version=1)
    pin = CaliberWorkspaceRevisionResource(
        resource_pin_id="WSRR-validate-1",
        revision_id="WSR-validate-1",
        resource_type="eval_dataset",
        logical_name="ED-validate-1",
        resource_id="ED-validate-1",
        version_ref="1",
        content_sha256="a" * 64,
        provider_ref="caliber-eval-dataset:/ED-validate-1@1",
        purpose="runtime",
        resolution={},
    )
    result = ADAPTER.validate(db_session, pin)
    assert result == {
        "valid": True,
        "resource_pin_id": "WSRR-validate-1",
        "dataset_id": "ED-validate-1",
        "version": 1,
    }


def test_validate_refuses_a_missing_dataset(db_session: Session) -> None:
    pin = CaliberWorkspaceRevisionResource(
        resource_pin_id="WSRR-validate-ghost",
        revision_id="WSR-validate-ghost",
        resource_type="eval_dataset",
        logical_name="ED-ghost",
        resource_id="ED-ghost",
        version_ref="1",
        content_sha256="a" * 64,
        purpose="runtime",
        resolution={},
    )
    with pytest.raises(WorkspaceReleaseAdapterError, match="not found"):
        ADAPTER.validate(db_session, pin)


def test_validate_refuses_a_dataset_outside_the_environments_project(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_project(db_session, project_id=OTHER_PROJECT_ID)
    _seed_dataset(db_session, dataset_id="ED-other", project_id=OTHER_PROJECT_ID)
    environment = CaliberWorkspaceEnvironment(
        environment_id="WSE-validate",
        project_id=PROJECT_ID,
        name="prod",
        environment_class="production",
        promotion_order=40,
        status="active",
        created_by="admin",
    )
    pin = CaliberWorkspaceRevisionResource(
        resource_pin_id="WSRR-validate-2",
        revision_id="WSR-validate-2",
        resource_type="eval_dataset",
        logical_name="ED-other",
        resource_id="ED-other",
        version_ref="1",
        content_sha256="a" * 64,
        purpose="runtime",
        resolution={},
    )
    with pytest.raises(WorkspaceReleaseAdapterError, match="project"):
        ADAPTER.validate(db_session, pin, environment)


def test_validate_rejects_a_non_integer_version_ref(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_dataset(db_session, dataset_id="ED-bad-version", project_id=PROJECT_ID)
    pin = CaliberWorkspaceRevisionResource(
        resource_pin_id="WSRR-validate-3",
        revision_id="WSR-validate-3",
        resource_type="eval_dataset",
        logical_name="ED-bad-version",
        resource_id="ED-bad-version",
        version_ref="not-a-number",
        content_sha256="a" * 64,
        purpose="runtime",
        resolution={},
    )
    with pytest.raises(WorkspaceReleaseAdapterError, match="non-integer version"):
        ADAPTER.validate(db_session, pin)


# -- prepare_release ------------------------------------------------------


def _pin(
    *, version_ref: str = "1", content_sha256: str = "a" * 64
) -> CaliberWorkspaceRevisionResource:
    return CaliberWorkspaceRevisionResource(
        resource_pin_id="WSRR-prep-1",
        revision_id="WSR-prep-1",
        resource_type="eval_dataset",
        logical_name="ED-1",
        resource_id="ED-1",
        version_ref=version_ref,
        content_sha256=content_sha256,
        purpose="runtime",
        resolution={},
    )


def _environment() -> CaliberWorkspaceEnvironment:
    return CaliberWorkspaceEnvironment(
        environment_id="WSE-prep-1",
        project_id=PROJECT_ID,
        name="prod",
        environment_class="production",
        promotion_order=40,
        status="active",
        created_by="admin",
    )


def test_prepare_release_verifies_when_before_differs_from_after() -> None:
    prepared = ADAPTER.prepare_release(_pin(version_ref="2"), _environment(), before_ref="1")
    assert prepared.action == "verify"
    assert prepared.target_ref == "eval_dataset:ED-1"
    assert prepared.before_ref == "1"
    assert prepared.after_ref == "2"
    assert prepared.metadata["dataset_id"] == "ED-1"
    assert prepared.metadata["content_sha256"] == "a" * 64


def test_prepare_release_is_a_no_op_when_before_equals_after() -> None:
    prepared = ADAPTER.prepare_release(_pin(version_ref="1"), _environment(), before_ref="1")
    assert prepared.action == "no_op"


# -- apply / observe / rollback -------------------------------------------


def _prepared_action(
    *,
    action: str = "verify",
    after_ref: str = "1",
    dataset_id: str = "ED-1",
    content_sha256: str = "a" * 64,
) -> PreparedAction:
    return PreparedAction(
        resource_pin_id="WSRR-apply-1",
        resource_type="eval_dataset",
        action=action,
        target_ref=f"eval_dataset:{dataset_id}",
        before_ref="0",
        after_ref=after_ref,
        metadata={
            "dataset_id": dataset_id,
            "environment_id": "WSE-1",
            "project_id": PROJECT_ID,
            "content_sha256": content_sha256,
        },
    )


def test_apply_release_confirms_matching_content(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_dataset(db_session, dataset_id="ED-1", project_id=PROJECT_ID, version=1)
    _add_example(db_session, example_id="EX-1", dataset_id="ED-1", dataset_version=1)
    expected_sha = _content_sha256(
        (
            {
                "example_id": "EX-1",
                "input": {"q": "EX-1"},
                "expected": {"a": "EX-1"},
                "weight": 1.0,
                "tags": [],
            },
        )
    )
    outcome = ADAPTER.apply_release(
        db_session, _prepared_action(after_ref="1", content_sha256=expected_sha)
    )
    assert outcome.status == "applied"
    assert outcome.provider_result["content_sha256"] == expected_sha
    assert outcome.provider_result["example_count"] == 1


def test_apply_release_no_op_does_not_touch_the_database(db_session: Session) -> None:
    outcome = ADAPTER.apply_release(db_session, _prepared_action(action="no_op"))
    assert outcome.status == "applied"
    assert outcome.provider_result["version"] == "1"


def test_apply_release_reports_a_missing_dataset(db_session: Session) -> None:
    outcome = ADAPTER.apply_release(db_session, _prepared_action(dataset_id="ED-ghost"))
    assert outcome.status == "failed"
    assert outcome.error_code == "eval_dataset_not_found"


def test_apply_release_reports_an_archived_dataset(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_dataset(
        db_session, dataset_id="ED-1", project_id=PROJECT_ID, version=1, status="archived"
    )
    outcome = ADAPTER.apply_release(db_session, _prepared_action())
    assert outcome.status == "failed"
    assert outcome.error_code == "eval_dataset_archived"


def test_apply_release_reports_a_project_mismatch(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_project(db_session, project_id=OTHER_PROJECT_ID)
    _seed_dataset(db_session, dataset_id="ED-1", project_id=OTHER_PROJECT_ID, version=1)
    outcome = ADAPTER.apply_release(db_session, _prepared_action())
    assert outcome.status == "failed"
    assert outcome.error_code == "resource_outside_project"


def test_apply_release_reports_a_version_not_yet_reached(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_dataset(db_session, dataset_id="ED-1", project_id=PROJECT_ID, version=1)
    outcome = ADAPTER.apply_release(db_session, _prepared_action(after_ref="9"))
    assert outcome.status == "failed"
    assert outcome.error_code == "eval_dataset_version_not_found"


def test_apply_release_reports_an_invalid_version(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_dataset(db_session, dataset_id="ED-1", project_id=PROJECT_ID, version=1)
    outcome = ADAPTER.apply_release(db_session, _prepared_action(after_ref="not-a-number"))
    assert outcome.status == "failed"
    assert outcome.error_code == "invalid_eval_dataset_version"


def test_apply_release_reports_content_drift(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_dataset(db_session, dataset_id="ED-1", project_id=PROJECT_ID, version=1)
    _add_example(db_session, example_id="EX-1", dataset_id="ED-1", dataset_version=1)
    outcome = ADAPTER.apply_release(
        db_session, _prepared_action(after_ref="1", content_sha256="stale" + "a" * 59)
    )
    assert outcome.status == "failed"
    assert outcome.error_code == "eval_dataset_content_drifted"


def test_rollback_release_runs_the_same_verification(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_dataset(db_session, dataset_id="ED-1", project_id=PROJECT_ID, version=1)
    _add_example(db_session, example_id="EX-1", dataset_id="ED-1", dataset_version=1)
    expected_sha = _content_sha256(
        (
            {
                "example_id": "EX-1",
                "input": {"q": "EX-1"},
                "expected": {"a": "EX-1"},
                "weight": 1.0,
                "tags": [],
            },
        )
    )
    outcome = ADAPTER.rollback_release(
        db_session, _prepared_action(after_ref="1", content_sha256=expected_sha)
    )
    assert outcome.status == "applied"


def test_observe_release_runs_the_same_verification(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_dataset(db_session, dataset_id="ED-1", project_id=PROJECT_ID, version=1)
    _add_example(db_session, example_id="EX-1", dataset_id="ED-1", dataset_version=1)
    expected_sha = _content_sha256(
        (
            {
                "example_id": "EX-1",
                "input": {"q": "EX-1"},
                "expected": {"a": "EX-1"},
                "weight": 1.0,
                "tags": [],
            },
        )
    )
    outcome = ADAPTER.observe_release(
        db_session, _prepared_action(after_ref="1", content_sha256=expected_sha)
    )
    assert outcome.status == "applied"


def test_malformed_target_ref_is_rejected(db_session: Session) -> None:
    bad = PreparedAction(
        resource_pin_id="WSRR-bad",
        resource_type="eval_dataset",
        action="verify",
        target_ref="not-an-eval-dataset-ref",
        before_ref=None,
        after_ref="1",
        metadata={},
    )
    with pytest.raises(WorkspaceReleaseAdapterError, match="malformed"):
        ADAPTER._verify(db_session, bad)


def test_apply_release_requires_an_after_ref(db_session: Session) -> None:
    bad = PreparedAction(
        resource_pin_id="WSRR-bad-2",
        resource_type="eval_dataset",
        action="verify",
        target_ref="eval_dataset:ED-1",
        before_ref=None,
        after_ref=None,
        metadata={},
    )
    with pytest.raises(WorkspaceReleaseAdapterError, match="no after_ref"):
        ADAPTER._verify(db_session, bad)
