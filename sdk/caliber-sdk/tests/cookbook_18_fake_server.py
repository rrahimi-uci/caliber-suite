"""A small stateful stand-in for the auth, project and audit routes cookbook 18 calls.

``test_cookbook_examples.py`` runs the real example script against this instead of a server, so
the script's call sequence and what it sends stay covered offline. The role rules are encoded here
a second time, independently of ``role-matrix.csv``: the tests then check that the data file, the
fake and the script all agree, and that the script notices when the server stops following the
rules. The behaviour that matters in production (the real role checks, token binding, expiry and
the audit trail) is exercised by running the script against a live server (see the lab README) and
by ``caliber/tests/test_*project*`` / ``test_*auth*``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx

PREFIX = "/ajax-api/2.0/mlflow/caliber"

#: Which project roles hold each project action (a restatement of ``PROJECT_ACTIONS`` for the
#: actions this lab touches, so the fake does not import the server).
ROLE_ACTIONS: dict[str, frozenset[str]] = {
    "read": frozenset({"owner", "editor", "reviewer", "viewer"}),
    "resource.write.evidence": frozenset({"owner", "editor", "reviewer"}),
    "resource.write.runtime": frozenset({"owner", "editor"}),
    "resource.execute": frozenset({"owner", "editor", "reviewer"}),
    "release.apply": frozenset({"owner", "editor"}),
    "project.update": frozenset({"owner", "editor"}),
    "project.manage_members": frozenset({"owner"}),
}

PLATFORM_SCOPES: dict[str, set[str]] = {
    "none": {"caliber.viewer"},
    "operator": {"caliber.viewer", "caliber.operator"},
    "operator+approver": {"caliber.viewer", "caliber.operator", "caliber.approver"},
}


def _envelope(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"data": data})


def _error(status: int, detail: str) -> httpx.Response:
    return httpx.Response(status, json={"detail": detail, "status_code": status})


class FakeCaliber:
    """Routes the example makes, with the same status codes and fields the server answers.

    ``roster`` is the parsed ``team-roster.csv``: it supplies each account's platform scopes. The
    fake's *own* switches (``bend``) let a test make it deviate from the rules in one named way, to
    prove the script reports the deviation instead of passing.
    """

    ADMIN = "admin"
    ADMIN_TOKEN = "calpat_admin"

    def __init__(
        self,
        roster: list[dict[str, str]],
        *,
        password: str = "lab18-demo-passphrase",
        csrf_enabled: bool = False,
        scopes_override: dict[str, str] | None = None,
        bend: str | None = None,
        simulated: tuple[str, ...] = ("eval", "llm"),
    ) -> None:
        self.password = password
        self.csrf_enabled = csrf_enabled
        self.bend = bend
        self.simulated = simulated
        self.scope_names = {p["user_id"]: p["platform_scopes"] for p in roster}
        self.scope_names.update(scopes_override or {})
        self.accounts: dict[str, bool] = {}  # user -> disabled
        self.sessions: dict[str, str] = {}
        self.tokens: dict[str, dict[str, Any]] = {}
        self.by_secret: dict[str, str] = {}
        self.projects: dict[str, dict[str, Any]] = {}
        self.members: dict[str, dict[str, dict[str, str]]] = {}
        self.prompts: dict[str, dict[str, Any]] = {}
        self.datasets: dict[str, str] = {}
        self.audit: list[dict[str, Any]] = []
        self.timeline: list[dict[str, Any]] = []
        self.live: dict[str, dict[str, Any]] = {}
        self.workflows: dict[str, str | None] = {}  # installed workflow id -> project
        self.clock = 0
        self.calls: list[str] = []
        self.expired: set[str] = set()

    # -- test plumbing -------------------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def age(self, _seconds: float = 0.0) -> None:
        """What waiting does: every token that has an ``expires_at`` is now past it."""
        self.expired.update(t["secret"] for t in self.tokens.values() if t["expires_at"])

    def login_attempt(self, user_id: str, password: str) -> tuple[int, str]:
        if user_id not in self.accounts or password != self.password:
            return 401, "invalid credentials"
        if self.accounts[user_id]:
            return 401, "invalid credentials"
        session = f"sess-{user_id}-{len(self.sessions)}"
        self.sessions[session] = user_id
        self._log(user_id, "sign_in", "session", session, client="127.0.0.1")
        return 200, session

    def _login(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        if self.csrf_enabled and not request.headers.get("X-CALIBER-CSRF"):
            return _error(403, "CSRF check failed: missing CSRF token")
        status, value = self.login_attempt(body["user_id"], body["password"])
        if status != 200:
            return _error(status, value)
        return httpx.Response(
            200,
            json={"data": {"user_id": body["user_id"]}},
            headers={"set-cookie": f"caliber_session={value}; Path=/; HttpOnly"},
        )

    def _log(
        self, actor: str, action: str, entity_type: str, entity_id: str, **details: Any
    ) -> None:
        self.clock += 1
        self.audit.append(
            {
                "log_id": self.clock,
                "timestamp": f"2026-10-16T10:00:{self.clock % 60:02d}",
                "actor": actor,
                "action": action,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "severity": "standard",
                "details": details,
            }
        )

    # -- identity ------------------------------------------------------------------------
    def _identity(self, request: httpx.Request) -> tuple[str | None, set[str], str | None]:
        """``(user, scopes, bound_project)``; ``user`` is ``None`` for anonymous."""
        header = request.headers.get("Authorization", "")
        secret = header.removeprefix("Bearer ").strip()
        if secret == self.ADMIN_TOKEN:
            return (
                self.ADMIN,
                {"caliber.admin", "caliber.operator", "caliber.approver", "caliber.viewer"},
                None,
            )
        if secret in self.sessions:
            user = self.sessions[secret]
            return user, self._scopes(user), None
        token_id = self.by_secret.get(secret)
        if token_id is None:
            return None, set(), None
        token = self.tokens[token_id]
        if token["revoked_at"] or secret in self.expired:
            return None, set(), None
        owner_scopes = self._scopes(token["user_id"])
        requested = set(token["scopes"])
        return (
            token["user_id"],
            (owner_scopes & requested if requested else owner_scopes),
            token["project_id"],
        )

    def _scopes(self, user: str) -> set[str]:
        return set(PLATFORM_SCOPES[self.scope_names.get(user, "none")])

    def _role(self, project_id: str, user: str, scopes: set[str]) -> str | None:
        if self.bend == "non-member-sees-project" and user == "nora":
            return "viewer"
        member = self.members.get(project_id, {}).get(user)
        if member is None or member["status"] != "active":
            return None
        role = member["role"]
        if role == "owner" and not {"caliber.operator", "caliber.approver"} <= scopes:
            return "editor"
        return role

    # -- request dispatch ----------------------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(PREFIX)
        method = request.method
        self.calls.append(f"{method} {path}")
        body = json.loads(request.content) if request.content else {}
        user, scopes, bound = self._identity(request)
        project_header = request.headers.get("X-CALIBER-Project")

        if path == "/health":
            return httpx.Response(200, json={"status": "ok", "version": "0.1.0a1", "db": "ok"})
        if path == "/readiness":
            return _envelope(self._readiness())
        if path == "/capabilities":
            return _envelope({"features": {"cookbooks": {"enabled": True}}})
        if path == "/csrf":
            return _envelope({"enabled": self.csrf_enabled, "token": "csrf", "ttl_seconds": 3600})
        if path == "/auth/login" and method == "POST":
            return self._login(request, body)
        if path == "/me":
            if user is None:
                return _envelope({"user_id": "anonymous", "scopes": [], "is_admin": False})
            return _envelope(
                {
                    "user_id": user,
                    "scopes": sorted(scopes),
                    "is_admin": "caliber.admin" in scopes,
                }
            )
        if user is None:
            return _error(401, "authentication required (no identity in request)")

        if (
            self.csrf_enabled
            and method not in {"GET", "HEAD"}
            and not request.headers.get("X-CALIBER-CSRF")
        ):
            return _error(403, "CSRF check failed: missing CSRF token")

        # A bound token may only act in its own project (header or URL).
        named = project_header or self._project_in(path)
        if bound and named and named != bound:
            return _error(
                403,
                f"this personal access token is bound to project {bound!r} and cannot act on "
                f"project {named!r}",
            )
        acting_project = project_header or bound

        handlers: list[tuple[str, str, Callable[..., httpx.Response]]] = [
            ("GET", r"/auth/session", self._session),
            ("GET", r"/auth/accounts", self._accounts_list),
            ("POST", r"/auth/accounts", self._accounts_create),
            ("PATCH", r"/auth/accounts/(?P<u>[^/]+)", self._accounts_update),
            ("DELETE", r"/auth/accounts/(?P<u>[^/]+)/sessions", self._accounts_sessions),
            ("GET", r"/auth/tokens", self._tokens_list),
            ("POST", r"/auth/tokens", self._tokens_create),
            ("DELETE", r"/auth/tokens/(?P<t>[^/]+)", self._tokens_revoke),
            ("GET", r"/projects", self._projects_list),
            ("POST", r"/projects", self._projects_create),
            ("GET", r"/projects/(?P<p>[^/]+)/members", self._members_list),
            ("POST", r"/projects/(?P<p>[^/]+)/members", self._members_add),
            ("PATCH", r"/projects/(?P<p>[^/]+)/members/(?P<u>[^/]+)", self._members_update),
            ("DELETE", r"/projects/(?P<p>[^/]+)/members/(?P<u>[^/]+)", self._members_remove),
            ("GET", r"/projects/(?P<p>[^/]+)", self._project_get),
            ("PATCH", r"/projects/(?P<p>[^/]+)", self._project_update),
            ("POST", r"/projects/(?P<p>[^/]+)/archive", self._project_archive),
            ("GET", r"/prompts", self._prompts_list),
            ("POST", r"/prompts", self._prompt_create),
            ("POST", r"/prompts/(?P<n>[^/]+)/aliases/(?P<a>[^/]+)", self._prompt_promote),
            ("POST", r"/eval-datasets", self._dataset_create),
            ("POST", r"/eval-datasets/(?P<d>[^/]+)/examples", self._example_create),
            ("POST", r"/evaluations", self._evaluation),
            ("GET", r"/releases/timeline", self._timeline),
            ("GET", r"/releases/live", self._live),
            ("GET", r"/audit-log", self._audit),
            ("GET", r"/cookbooks", self._cookbooks),
            ("POST", r"/cookbooks/(?P<c>[^/]+)/install", self._cookbook_install),
        ]
        for verb, pattern, handler in handlers:
            match = re.fullmatch(pattern, path)
            if verb == method and match:
                return handler(
                    request=request,
                    body=body,
                    user=user,
                    scopes=scopes,
                    acting=acting_project,
                    **match.groupdict(),
                )
        raise AssertionError(f"the example called an unstubbed route: {method} {path}")

    @staticmethod
    def _project_in(path: str) -> str | None:
        match = re.match(r"/projects/([^/]+)", path)
        return match.group(1) if match else None

    def _readiness(self) -> dict[str, Any]:
        return {
            "ready": True,
            "blockers": [],
            "simulated": list(self.simulated),
            "all_real": not self.simulated,
            "checks": [
                {
                    "name": "providers",
                    "ready": not self.simulated,
                    "detail": "simulated: " + ", ".join(self.simulated)
                    if self.simulated
                    else "all real",
                }
            ],
        }

    # -- guards --------------------------------------------------------------------------
    def _need_scope(self, scopes: set[str], scope: str) -> httpx.Response | None:
        if scope in scopes:
            return None
        return _error(
            403,
            f"missing required scope; expected one of ['{scope}'], have {sorted(scopes)}",
        )

    def _project_gate(
        self, project_id: str | None, user: str, scopes: set[str], action: str
    ) -> httpx.Response | None:
        if project_id not in self.projects:
            return _error(404, f"project {project_id!r} not found")
        role = self._role(project_id, user, scopes)
        if role is None:
            return _error(404, f"project {project_id!r} not found")
        allowed = ROLE_ACTIONS[action]
        if self.bend == "viewer-can-promote" and action == "release.apply" and role == "viewer":
            return None
        if role not in allowed:
            return _error(403, f"project role {role!r} cannot perform {action}")
        return None

    # -- auth routes ---------------------------------------------------------------------
    def _session(self, **kw: Any) -> httpx.Response:
        user, scopes = kw["user"], kw["scopes"]
        return _envelope(
            {
                "user_id": user,
                "scopes": sorted(scopes),
                "is_admin": "caliber.admin" in scopes,
                "auth_mode": "session",
                "authenticated_by": "session",
                "login_required": False,
            }
        )

    def _accounts_list(self, **kw: Any) -> httpx.Response:
        if (denied := self._need_scope(kw["scopes"], "caliber.admin")) is not None:
            return denied
        return _envelope(
            {
                "accounts": [
                    {"user_id": u, "disabled": d} for u, d in sorted(self.accounts.items())
                ],
                "total": len(self.accounts),
            }
        )

    def _accounts_create(self, **kw: Any) -> httpx.Response:
        if (denied := self._need_scope(kw["scopes"], "caliber.admin")) is not None:
            return denied
        user_id = kw["body"]["user_id"]
        if user_id in self.accounts:
            return _error(400, f"account {user_id!r} already exists")
        self.accounts[user_id] = False
        self._log(kw["user"], "create_account", "account", user_id)
        return _envelope({"user_id": user_id, "disabled": False}, 201)

    def _accounts_update(self, **kw: Any) -> httpx.Response:
        if (denied := self._need_scope(kw["scopes"], "caliber.admin")) is not None:
            return denied
        user_id = kw["u"]
        if "disabled" in kw["body"]:
            self.accounts[user_id] = bool(kw["body"]["disabled"])
        return _envelope({"user_id": user_id, "changed": ["disabled"]})

    def _accounts_sessions(self, **kw: Any) -> httpx.Response:
        user_id = kw["u"]
        stale = [s for s, u in self.sessions.items() if u == user_id]
        for session in stale:
            del self.sessions[session]
        return _envelope({"user_id": user_id, "revoked": len(stale)})

    def _tokens_list(self, **kw: Any) -> httpx.Response:
        rows = [
            {k: v for k, v in token.items() if k != "secret"}
            for token in self.tokens.values()
            if token["user_id"] == kw["user"]
        ]
        return _envelope({"tokens": rows})

    def _tokens_create(self, **kw: Any) -> httpx.Response:
        body, user, scopes = kw["body"], kw["user"], kw["scopes"]
        requested = list(body.get("scopes") or [])
        excessive = sorted(set(requested) - scopes)
        if excessive:
            return _error(403, f"cannot grant scopes you do not hold: {excessive}")
        project_id = body.get("project_id")
        if project_id and self._role(project_id, user, scopes) is None:
            return _error(404, f"project {project_id!r} not found")
        token_id = f"PAT-{len(self.tokens) + 1:04d}"
        secret = f"calpat_{token_id}"
        row = {
            "token_id": token_id,
            "user_id": user,
            "name": body["name"],
            "scopes": sorted(requested),
            "project_id": project_id,
            "expires_at": body.get("expires_at"),
            "revoked_at": None,
            "active": True,
        }
        self.tokens[token_id] = {**row, "secret": secret}
        self.by_secret[secret] = token_id
        if body.get("expires_at"):
            datetime.fromisoformat(body["expires_at"])  # the server rejects an unparseable one
        self._log(
            user,
            "create_personal_access_token",
            "personal_access_token",
            token_id,
            scopes=sorted(requested),
            project_id=project_id,
        )
        return _envelope({**row, "token": secret}, 201)

    def _tokens_revoke(self, **kw: Any) -> httpx.Response:
        token = self.tokens.get(kw["t"])
        if token is None or token["user_id"] != kw["user"]:
            return _error(404, f"token {kw['t']!r} not found")
        token["revoked_at"] = datetime.now(timezone.utc).isoformat()
        token["active"] = False
        self._log(kw["user"], "revoke_personal_access_token", "personal_access_token", kw["t"])
        return _envelope({"token_id": kw["t"], "revoked": True})

    # -- projects ------------------------------------------------------------------------
    def _project_view(self, project_id: str, user: str, scopes: set[str]) -> dict[str, Any]:
        project = self.projects[project_id]
        role = self._role(project_id, user, scopes)
        return {**project, "access_role": role, "permissions": []}

    def _projects_list(self, **kw: Any) -> httpx.Response:
        rows = [
            self._project_view(pid, kw["user"], kw["scopes"])
            for pid, project in self.projects.items()
            if project["status"] == "active"
            and self._role(pid, kw["user"], kw["scopes"]) is not None
        ]
        return _envelope(rows)

    def _project_archive(self, **kw: Any) -> httpx.Response:
        if (denied := self._need_scope(kw["scopes"], "caliber.operator")) is not None:
            return denied
        denied = self._project_gate(kw["p"], kw["user"], kw["scopes"], "project.manage_members")
        if denied is not None:
            return denied
        self.projects[kw["p"]]["status"] = "archived"
        self._log(kw["user"], "archive_project", "project", kw["p"])
        return _envelope(self._project_view(kw["p"], kw["user"], kw["scopes"]))

    def _projects_create(self, **kw: Any) -> httpx.Response:
        user, scopes = kw["user"], kw["scopes"]
        if not {"caliber.operator", "caliber.approver"} <= scopes:
            return _error(403, "missing required scopes")
        project_id = f"PRJ-{len(self.projects) + 1:04d}"
        self.projects[project_id] = {
            "project_id": project_id,
            "name": kw["body"]["name"],
            "description": kw["body"].get("description", ""),
            "owner": user,
            "status": "active",
        }
        self.members[project_id] = {user: {"role": "owner", "status": "active"}}
        self._log(user, "create_project", "project", project_id, name=kw["body"]["name"])
        return _envelope(self._project_view(project_id, user, scopes), 201)

    def _project_get(self, **kw: Any) -> httpx.Response:
        denied = self._project_gate(kw["p"], kw["user"], kw["scopes"], "read")
        return denied or _envelope(self._project_view(kw["p"], kw["user"], kw["scopes"]))

    def _project_update(self, **kw: Any) -> httpx.Response:
        if (denied := self._need_scope(kw["scopes"], "caliber.operator")) is not None:
            return denied
        denied = self._project_gate(kw["p"], kw["user"], kw["scopes"], "project.update")
        if denied is not None:
            return denied
        self.projects[kw["p"]]["description"] = kw["body"].get("description", "")
        self._log(kw["user"], "update_project", "project", kw["p"])
        return _envelope(self._project_view(kw["p"], kw["user"], kw["scopes"]))

    def _members_list(self, **kw: Any) -> httpx.Response:
        denied = self._project_gate(kw["p"], kw["user"], kw["scopes"], "read")
        if denied is not None:
            return denied
        rows = [
            {"project_id": kw["p"], "user_id": u, **m}
            for u, m in self.members[kw["p"]].items()
            if m["status"] == "active"
        ]
        return _envelope({"members": rows})

    def _members_add(self, **kw: Any) -> httpx.Response:
        if (denied := self._need_scope(kw["scopes"], "caliber.operator")) is not None:
            return denied
        denied = self._project_gate(kw["p"], kw["user"], kw["scopes"], "project.manage_members")
        if denied is not None:
            return denied
        body = kw["body"]
        if body["role"] not in {"owner", "editor", "reviewer", "viewer"}:
            return _error(400, "role must be owner, editor, reviewer, or viewer")
        if body["user_id"] in self.members[kw["p"]] and (
            self.members[kw["p"]][body["user_id"]]["status"] == "active"
        ):
            return _error(409, "user is already a project member")
        self.members[kw["p"]][body["user_id"]] = {"role": body["role"], "status": "active"}
        self._log(kw["user"], "add_project_member", "project", kw["p"], **body)
        return _envelope(
            {
                "project_id": kw["p"],
                "user_id": body["user_id"],
                "role": body["role"],
                "status": "active",
            },
            201,
        )

    def _members_update(self, **kw: Any) -> httpx.Response:
        if (denied := self._need_scope(kw["scopes"], "caliber.operator")) is not None:
            return denied
        denied = self._project_gate(kw["p"], kw["user"], kw["scopes"], "project.manage_members")
        if denied is not None:
            return denied
        target, body = kw["u"], kw["body"]
        if self.projects[kw["p"]]["owner"] == target:
            return _error(409, "the primary owner cannot be changed here; use transfer-ownership")
        if body.get("role") == "owner" and self.scope_names.get(target) != "operator+approver":
            return _error(
                409,
                f"{target!r} lacks the caliber.operator + caliber.approver scopes required for "
                "the owner role",
            )
        self.members[kw["p"]][target]["role"] = body.get(
            "role", self.members[kw["p"]][target]["role"]
        )
        self._log(kw["user"], "update_project_member", "project", kw["p"], user_id=target)
        return _envelope(
            {
                "project_id": kw["p"],
                "user_id": target,
                "role": self.members[kw["p"]][target]["role"],
            }
        )

    def _members_remove(self, **kw: Any) -> httpx.Response:
        if (denied := self._need_scope(kw["scopes"], "caliber.operator")) is not None:
            return denied
        denied = self._project_gate(kw["p"], kw["user"], kw["scopes"], "project.manage_members")
        if denied is not None:
            return denied
        target = kw["u"]
        if self.projects[kw["p"]]["owner"] == target:
            return _error(409, "the primary owner cannot be removed")
        self.members[kw["p"]][target]["status"] = "inactive"
        self._log(kw["user"], "remove_project_member", "project", kw["p"], user_id=target)
        return httpx.Response(
            200, json={"data": {"project_id": kw["p"], "user_id": target, "removed": True}}
        )

    # -- project content -----------------------------------------------------------------
    def _write_gate(self, kw: dict[str, Any], action: str) -> httpx.Response | None:
        if (denied := self._need_scope(kw["scopes"], "caliber.operator")) is not None:
            return denied
        return self._project_gate(kw["acting"], kw["user"], kw["scopes"], action)

    def _prompts_list(self, **kw: Any) -> httpx.Response:
        """The inventory: prompts, and workflow-node prompts, of projects the caller belongs to."""
        user, scopes = kw["user"], kw["scopes"]
        rows = [
            {"agent_id": name}
            for name, prompt in self.prompts.items()
            if self._role(prompt["project_id"], user, scopes) is not None
        ]
        for workflow_id, project_id in self.workflows.items():
            if self.bend == "workflow-prompts-unscoped" or (
                project_id is not None and self._role(project_id, user, scopes) is not None
            ):
                rows.append({"agent_id": f"wf-{workflow_id}-agent"})
        return _envelope(rows)

    def _prompt_create(self, **kw: Any) -> httpx.Response:
        denied = self._write_gate(kw, "resource.write.runtime")
        if denied is not None:
            return denied
        name = kw["body"]["name"]
        self.prompts[name] = {"project_id": kw["acting"], "versions": 1}
        self._log(kw["user"], "prompt.create", "prompt", name)
        return _envelope({"name": name, "version": 1}, 201)

    def _prompt_promote(self, **kw: Any) -> httpx.Response:
        if (denied := self._need_scope(kw["scopes"], "caliber.operator")) is not None:
            return denied
        prompt = self.prompts.get(kw["n"])
        if prompt is None or self._role(prompt["project_id"], kw["user"], kw["scopes"]) is None:
            return _error(404, f"prompt {kw['n']!r} not found")
        denied = self._project_gate(prompt["project_id"], kw["user"], kw["scopes"], "release.apply")
        if denied is not None:
            return denied
        operation = f"REL-{len(self.timeline) + 1:04d}"
        details = {
            "operation_id": operation,
            "alias": kw["a"],
            "from_version": None,
            "to_version": kw["body"]["version"],
            "gate_state": "none",
            "overridden": False,
        }
        self._log(kw["user"], "promote_prompt", "prompt", kw["n"], **details)
        self.timeline.append(
            {
                "log_id": self.clock,
                "action": "promote_prompt",
                "actor": kw["user"],
                "entity_type": "prompt",
                "entity_id": kw["n"],
                "details": details,
            }
        )
        self.live[kw["n"]] = {
            "artifact_type": "prompt",
            "artifact_id": kw["n"],
            "alias": kw["a"],
            "version_id": f"v{kw['body']['version']}",
            "by": kw["user"],
        }
        return _envelope(
            {
                "name": kw["n"],
                "alias": kw["a"],
                "version": 1,
                "operation_id": operation,
                "release_status": "applied",
            }
        )

    def _dataset_create(self, **kw: Any) -> httpx.Response:
        denied = self._write_gate(kw, "resource.write.evidence")
        if denied is not None:
            return denied
        dataset_id = f"ED-{len(self.datasets) + 1:04d}"
        self.datasets[dataset_id] = kw["acting"]
        return _envelope({"dataset_id": dataset_id}, 201)

    def _example_create(self, **kw: Any) -> httpx.Response:
        return _envelope({"example_id": "EX-1", "dataset_id": kw["d"]}, 201)

    def _evaluation(self, **kw: Any) -> httpx.Response:
        if (denied := self._need_scope(kw["scopes"], "caliber.operator")) is not None:
            return denied
        project_id = self.datasets.get(kw["body"]["dataset_id"])
        if project_id is None or self._role(project_id, kw["user"], kw["scopes"]) is None:
            return _error(404, f"eval dataset {kw['body']['dataset_id']!r} not found")
        denied = self._project_gate(project_id, kw["user"], kw["scopes"], "resource.execute")
        if denied is not None:
            return denied
        return _error(400, "evaluation requires a real LLM provider; set CALIBER_LLM_PROVIDER")

    # -- reading back --------------------------------------------------------------------
    def _timeline(self, **kw: Any) -> httpx.Response:
        wanted = kw["request"].url.params.get("entity_type")
        rows = [
            row for row in reversed(self.timeline) if not wanted or row["entity_type"] == wanted
        ]
        return _envelope(rows)

    def _live(self, **_kw: Any) -> httpx.Response:
        return _envelope(list(self.live.values()))

    def _audit(self, **kw: Any) -> httpx.Response:
        if (denied := self._need_scope(kw["scopes"], "caliber.admin")) is not None:
            return denied
        params = kw["request"].url.params
        rows = [
            row
            for row in reversed(self.audit)
            if all(
                params.get(key) in (None, row[key])
                for key in ("actor", "action", "entity_type", "entity_id")
            )
        ]
        return _envelope({"entries": rows, "total": len(rows), "limit": 500, "offset": 0})

    def _cookbooks(self, **_kw: Any) -> httpx.Response:
        recipe = {
            "id": "18",
            "title": "Projects, Roles, Tokens & Audit",
            "summary": "s",
            "prerequisites": ["Accounts for the team"],
            "steps": [{"id": f"18.{n}", "title": "step", "route": "/"} for n in range(1, 11)],
            "readiness": {
                "status": "configuration_required",
                "checks": [{"label": "Accounts", "status": "operator_confirmation_required"}],
            },
        }
        return _envelope({"recipes": [recipe]})

    def _cookbook_install(self, **kw: Any) -> httpx.Response:
        if not kw["body"].get("acknowledge_prerequisites"):
            return _error(409, "prerequisites must be acknowledged")
        self.workflows["WF-18"] = kw["acting"]
        return _envelope(
            {
                "recipe": {"id": kw["c"]},
                "workflow": {
                    "workflow_id": "WF-18",
                    "project_id": kw["acting"],
                    "status": "paused",
                },
                "version": {"version_id": "WFV-18", "status": "draft"},
                "activation_requires_review": True,
            },
            201,
        )
