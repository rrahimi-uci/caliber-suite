"""The project binding of a personal access token holds in every auth gate (alpha gap P1-01).

``resolve_identity`` always enforced it. ``require_user``, ``require_scopes`` and
``require_all_scopes`` did not, so a token bound to project B could act on project A
through any route that gates only on a scope and passes the ``X-CALIBER-Project`` header
along. These tests drive each gate directly with a request that carries a bound
credential, so the guarantee does not depend on which routes happen to exist.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.exceptions import HTTPException
from starlette.requests import Request

from caliber import auth
from caliber.auth import (
    SCOPE_OPERATOR,
    SCOPE_VIEWER,
    require_all_scopes,
    require_scopes,
    require_user,
    resolve_identity,
)
from caliber.config import CaliberConfig

BOUND = "PRJ-bound"
OTHER = "PRJ-other"


def _request(
    *,
    bound_to: str | None = BOUND,
    header_project: str | None = None,
    path_project: str | None = None,
) -> Request:
    headers = [(b"x-caliber-user", b"@ana")]
    if header_project is not None:
        headers.append((b"x-caliber-project", header_project.encode()))
    config = CaliberConfig.load(
        environ={"CALIBER_AUTH_MODE": "trusted_header", "CALIBER_OPERATOR_USERS": "@ana"}
    )
    scope: dict[str, object] = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers,
        "path_params": {"project_id": path_project} if path_project else {},
        "app": SimpleNamespace(state=SimpleNamespace(config=config)),
    }
    if bound_to is not None:
        # What ``current_user`` stashes after it validates a project-bound PAT.
        scope[auth._CREDENTIAL_KIND_KEY] = "pat"
        scope[auth._CREDENTIAL_PROJECT_KEY] = bound_to
    return Request(scope)


GATES = {
    "require_user": lambda request: require_user(request),
    "require_scopes": lambda request: require_scopes(request, [SCOPE_VIEWER]),
    "require_all_scopes": lambda request: require_all_scopes(
        request, [SCOPE_VIEWER, SCOPE_OPERATOR]
    ),
}


@pytest.mark.parametrize("gate", sorted(GATES))
def test_every_gate_refuses_another_projects_header(gate: str) -> None:
    with pytest.raises(HTTPException) as refused:
        GATES[gate](_request(header_project=OTHER))
    assert refused.value.status_code == 403
    assert BOUND in str(refused.value.detail)


@pytest.mark.parametrize("gate", sorted(GATES))
def test_every_gate_refuses_another_projects_path_segment(gate: str) -> None:
    with pytest.raises(HTTPException) as refused:
        GATES[gate](_request(path_project=OTHER))
    assert refused.value.status_code == 403


@pytest.mark.parametrize("gate", sorted(GATES))
def test_every_gate_accepts_the_bound_project_or_none(gate: str) -> None:
    assert GATES[gate](_request(header_project=BOUND)) == "@ana"
    assert GATES[gate](_request(path_project=BOUND)) == "@ana"
    assert GATES[gate](_request()) == "@ana"


@pytest.mark.parametrize("gate", sorted(GATES))
def test_unbound_credentials_are_unaffected(gate: str) -> None:
    assert GATES[gate](_request(bound_to=None, header_project=OTHER)) == "@ana"


def test_a_blank_project_header_is_not_a_conflicting_project() -> None:
    assert require_scopes(_request(header_project="   "), [SCOPE_VIEWER]) == "@ana"


def test_a_bound_token_with_no_header_acts_in_its_bound_project() -> None:
    identity = resolve_identity(_request())
    assert identity.active_project_id == BOUND
    assert identity.credential_project_id == BOUND


def test_an_explicit_header_for_the_bound_project_is_kept() -> None:
    assert resolve_identity(_request(header_project=BOUND)).active_project_id == BOUND


def test_an_unbound_credential_with_no_header_has_no_active_project() -> None:
    assert resolve_identity(_request(bound_to=None)).active_project_id is None
