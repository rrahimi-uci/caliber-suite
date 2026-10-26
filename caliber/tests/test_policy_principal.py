"""Subject, Workload and RunPrincipal: immutable, pure, digest-derived and strict about what they read back (#657)."""

from __future__ import annotations

import ast
import dataclasses
import hashlib
from pathlib import Path
from typing import Any

import pytest

from caliber import contracts
from caliber.auth import CaliberIdentity
from caliber.policy import principal as principal_module
from caliber.policy.principal import (
    AUTH_METHODS,
    PrincipalError,
    RunPrincipal,
    Subject,
    Workload,
    workload_id_for,
)

DIGEST = hashlib.sha256(b"workflow-version-1").hexdigest()
OTHER_DIGEST = hashlib.sha256(b"workflow-version-2").hexdigest()


def _subject(**overrides: Any) -> Subject:
    return Subject(
        **{"user_id": "alice", "auth_method": "session", "project_id": "proj-1", **overrides}
    )


def _workload() -> Workload:
    return Workload.for_workflow("triage", DIGEST)


# ---------------------------------------------------------------- immutability


@pytest.mark.parametrize(
    ("value", "field"),
    [
        (_subject(), "user_id"),
        (_subject(), "project_id"),
        (_workload(), "name"),
        (_workload(), "workload_id"),
        (RunPrincipal(_subject(), _workload(), "grant-1"), "grant_id"),
    ],
)
def test_the_value_objects_are_immutable(value: Any, field: str) -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(value, field, "other")


def test_equal_values_are_equal_and_hashable() -> None:
    assert _subject() == _subject()
    assert hash(_workload()) == hash(Workload.for_workflow("triage", DIGEST))
    assert RunPrincipal(_subject(), _workload(), "g") != RunPrincipal(_subject(), _workload(), "h")


def test_the_subject_and_the_workload_are_distinct_types_a_run_principal_will_not_swap() -> None:
    with pytest.raises(PrincipalError, match="subject must be a Subject"):
        RunPrincipal(_workload(), _workload(), "g")  # type: ignore[arg-type]
    with pytest.raises(PrincipalError, match="workload must be a Workload"):
        RunPrincipal(_subject(), _subject(), "g")  # type: ignore[arg-type]


# ---------------------------------------------------------------- the workload id comes from the full digest


def test_the_workload_id_is_derived_from_the_full_digest() -> None:
    workload = Workload.for_workflow("triage", DIGEST)
    assert workload.workload_id == "wl_" + DIGEST
    assert workload.workload_id == workload_id_for(DIGEST)
    assert Workload.for_workflow("renamed", DIGEST).workload_id == workload.workload_id, (
        "the name is not the id"
    )
    assert Workload.for_workflow("triage", OTHER_DIGEST).workload_id != workload.workload_id


@pytest.mark.parametrize(
    "digest",
    [
        DIGEST[:63],
        DIGEST + "0",
        DIGEST.upper(),
        "sha256:" + DIGEST,
        "g" * 64,
        "",
        " " + DIGEST[1:],
        None,
        12,
    ],
)
def test_only_a_full_lowercase_sha256_digest_derives_an_id(digest: Any) -> None:
    with pytest.raises(PrincipalError, match="64-hex"):
        workload_id_for(digest)
    with pytest.raises(PrincipalError, match="64-hex"):
        Workload.for_workflow("triage", digest)


def test_a_caller_chosen_workload_id_is_rejected() -> None:
    with pytest.raises(PrincipalError, match="derived"):
        Workload(workload_id="wl_chosen-by-caller", name="triage", workflow_version_digest=DIGEST)
    with pytest.raises(PrincipalError, match="derived"):
        Workload(workload_id="wl_" + OTHER_DIGEST, name="triage", workflow_version_digest=DIGEST)
    with pytest.raises(PrincipalError, match="derived"):
        Workload(workload_id=DIGEST, name="triage", workflow_version_digest=DIGEST)


# ---------------------------------------------------------------- field validation


@pytest.mark.parametrize("value", ["", " alice", "alice ", None, 7])
def test_a_subject_user_id_must_be_a_clean_non_empty_string(value: Any) -> None:
    with pytest.raises(PrincipalError, match="user_id"):
        _subject(user_id=value)


@pytest.mark.parametrize("value", ["", "oauth", "Session", None, ["session"]])
def test_a_subject_auth_method_must_be_a_known_credential_kind(value: Any) -> None:
    with pytest.raises(PrincipalError, match="auth_method"):
        _subject(auth_method=value)


@pytest.mark.parametrize("method", sorted(AUTH_METHODS))
def test_every_credential_kind_caliber_auth_records_is_a_valid_auth_method(method: str) -> None:
    assert _subject(auth_method=method).auth_method == method
    assert {"session", "pat", "trusted_header"} == AUTH_METHODS


def test_the_project_is_optional_but_never_blank() -> None:
    assert _subject(project_id=None).project_id is None
    with pytest.raises(PrincipalError, match="project_id"):
        _subject(project_id="")


@pytest.mark.parametrize("name", ["", " triage", None, 3])
def test_a_workload_name_must_be_a_clean_non_empty_string(name: Any) -> None:
    with pytest.raises(PrincipalError, match="name"):
        Workload.for_workflow(name, DIGEST)


@pytest.mark.parametrize("grant_id", ["", " g", None, 5])
def test_a_run_principal_needs_a_grant_id(grant_id: Any) -> None:
    with pytest.raises(PrincipalError, match="grant_id"):
        RunPrincipal(_subject(), _workload(), grant_id)


# ---------------------------------------------------------------- serialization against the strict manifest schema


def _manifest(subject: dict[str, Any], workload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "v1",
        "manifest_id": "m-1",
        "workflow_run_id": "run-1",
        "tenant_id": "local",
        "region": "local",
        "pins": {
            "workflow_version": "1",
            "bundle_digest": DIGEST,
            "prompt_versions": [],
            "tool_versions": [],
            "skill_versions": [],
            "knowledge_base_versions": [],
            "model_routes": [],
            "policy_version": "p1",
        },
        "subject": subject,
        "workload": workload,
        "purpose": "triage",
        "created_at": "2026-10-26T00:00:00Z",
    }


def test_the_manifest_forms_satisfy_the_strict_run_manifest_schema() -> None:
    contracts.validate_record(
        "run_manifest", _manifest(_subject().to_manifest(), _workload().to_manifest())
    )


def test_the_subject_serializes_only_what_the_schema_accepts_and_never_the_project() -> None:
    assert _subject().to_manifest() == {"user_id": "alice", "auth_method": "session"}
    assert "project_id" not in _subject().to_manifest()
    leaked = {**_subject().to_manifest(), "project_id": "proj-1"}
    with pytest.raises(contracts.ContractValidationError):
        contracts.validate_record("run_manifest", _manifest(leaked, _workload().to_manifest()))
    with pytest.raises(PrincipalError, match="project_id"):
        Subject.from_manifest(leaked)


def test_a_subject_round_trips_with_the_project_supplied_from_server_side_context() -> None:
    restored = Subject.from_manifest(_subject().to_manifest(), project_id="proj-1")
    assert restored == _subject()
    assert Subject.from_manifest(_subject().to_manifest()).project_id is None


def test_a_workload_round_trips() -> None:
    assert Workload.from_manifest(_workload().to_manifest()) == _workload()


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        "alice",
        {},
        {"user_id": "alice"},
        {"auth_method": "session"},
        {"user_id": "alice", "auth_method": "session", "extra": 1},
        {"user_id": "alice", "auth_method": "root"},
        {"user_id": "", "auth_method": "session"},
    ],
)
def test_a_malformed_subject_is_refused(data: Any) -> None:
    with pytest.raises(PrincipalError):
        Subject.from_manifest(data)


def test_a_forged_or_malformed_workload_is_refused() -> None:
    good = _workload().to_manifest()
    for bad in (
        None,
        [],
        {},
        {**good, "extra": 1},
        {k: v for k, v in good.items() if k != "name"},
        {**good, "workload_id": "wl_" + OTHER_DIGEST},
        {**good, "workflow_version_digest": OTHER_DIGEST},
        {**good, "workflow_version_digest": DIGEST[:32]},
        {**good, "name": ""},
    ):
        with pytest.raises(PrincipalError):
            Workload.from_manifest(bad)


def test_the_error_names_extra_and_missing_fields() -> None:
    with pytest.raises(PrincipalError, match=r"extra \['project_id'\], missing \['auth_method'\]"):
        Subject.from_manifest({"user_id": "alice", "project_id": "proj-1"})


# ---------------------------------------------------------------- pure CaliberIdentity conversion


def test_a_resolved_subject_converts_to_exactly_the_identity_it_describes() -> None:
    scopes = frozenset({"caliber:read", "caliber:write"})
    identity = _subject(auth_method="pat").to_identity(scopes)
    assert identity == CaliberIdentity(
        user_id="alice",
        scopes=scopes,
        active_project_id="proj-1",
        credential_kind="pat",
        credential_id=None,
        credential_project_id=None,
    )
    assert _subject(project_id=None).to_identity(frozenset()).active_project_id is None
    assert _subject().to_identity(frozenset()).scopes == frozenset(), "no scope is ever added"


@pytest.mark.parametrize("scopes", [{"caliber:read"}, ["caliber:read"], "caliber:read", None])
def test_scopes_must_be_the_already_resolved_frozenset(scopes: Any) -> None:
    with pytest.raises(PrincipalError, match="frozenset"):
        _subject().to_identity(scopes)


def test_the_conversion_never_carries_a_credential_binding() -> None:
    identity = _subject().to_identity(frozenset({"s"}))
    assert identity.credential_id is None
    assert identity.credential_project_id is None, "only a real credential can bind a project"


# ---------------------------------------------------------------- purity: no database, worker or admission import


def test_the_module_imports_no_database_worker_manifest_or_grant_code() -> None:
    tree = ast.parse(Path(principal_module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert all(
        not name.startswith(("sqlalchemy", "caliber.db", "caliber.orchestrator"))
        for name in imported
    )
    assert not {n for n in imported if "worker" in n or "manifest" in n or "grant" in n}
    called = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not {"build_run_manifest", "load_run_manifest"} & called
    assert imported <= {
        "__future__",
        "re",
        "collections.abc",
        "dataclasses",
        "typing",
        "caliber.auth",
    }
