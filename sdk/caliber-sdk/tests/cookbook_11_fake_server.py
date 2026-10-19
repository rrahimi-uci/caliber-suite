"""A stateful stand-in for the routes cookbook 11 calls.

``test_cookbook_11_example.py`` replays the real example script against this, so the script's call
sequence, what it sends and what it reads stay covered offline. It reproduces the *contract* the
script depends on and copies the server's rules for scoring, blockers, waiver expiry, signoff and
immutability (``caliber/src/caliber/routes/releases.py``). That the real server behaves this way is
shown by the script's run against a live stack (see the lab README) and by ``caliber/tests``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from caliber_sdk import CaliberClient

OPERATOR_TOKENS = {"calpat_admin": "admin", "calpat_operator": "lab11-operator"}
ADMIN_TOKENS = {"calpat_admin": "admin"}


def _envelope(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"data": data})


def _error(status: int, detail: str) -> httpx.Response:
    return httpx.Response(status, json={"detail": detail, "status_code": status})


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


class FakeReleaseServer:
    def __init__(self) -> None:
        self.candidates: dict[str, dict[str, Any]] = {}
        self.signoffs: dict[str, list[dict[str, Any]]] = {}
        self.reports: dict[str, dict[str, Any]] = {}
        self.prompts: dict[str, dict[int, list[str]]] = {}
        self.timeline: list[dict[str, Any]] = []
        self.audit: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self._n = 0

    def client(self, token: str) -> CaliberClient:
        http = httpx.Client(transport=httpx.MockTransport(lambda r: self.handle(r, token)))
        return CaliberClient("https://caliber.test", token=token, http_client=http)

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{self._n:04d}"

    def _log(self, actor: str, action: str, entity_id: str) -> None:
        self.audit.append(
            {
                "log_id": len(self.audit) + 1,
                "actor": actor,
                "action": action,
                "entity_type": "release_candidate",
                "entity_id": entity_id,
            }
        )

    def handle(self, request: httpx.Request, token: str) -> httpx.Response:
        path = request.url.path.rsplit("/caliber", 1)[-1]
        self.calls.append(f"{request.method} {path}")
        body = json.loads(request.content) if request.content else {}
        for method, pattern, handler in self._routes():
            match = re.fullmatch(pattern, path)
            if request.method == method and match:
                return handler(body, token, *match.groups())  # type: ignore[no-any-return]
        raise AssertionError(f"example called an unstubbed route: {request.method} {path}")

    def _routes(self) -> list[tuple[str, str, Any]]:
        return [
            ("POST", r"/prompts", self.create_prompt),
            ("POST", r"/prompts/([^/]+)/versions", self.add_version),
            ("GET", r"/prompts/([^/]+)/versions", self.versions),
            ("POST", r"/prompts/([^/]+)/aliases/([^/]+)", self.promote),
            ("DELETE", r"/prompts/([^/]+)", self.delete_prompt),
            ("POST", r"/releases/candidates", self.create_candidate),
            ("GET", r"/releases/candidates", self.list_candidates),
            ("GET", r"/releases/candidates/([^/]+)", self.get_candidate),
            ("POST", r"/releases/candidates/([^/]+)/evaluate", self.evaluate),
            ("POST", r"/releases/candidates/([^/]+)/waivers", self.waive),
            ("POST", r"/releases/candidates/([^/]+)/reports", self.report),
            ("POST", r"/releases/candidates/([^/]+)/signoffs", self.signoff),
            ("GET", r"/releases/report-jobs/([^/]+)", self.report_job),
            ("GET", r"/releases/timeline", lambda *_: _envelope(list(self.timeline))),
            ("GET", r"/releases/live", lambda *_: _envelope([])),
            ("GET", r"/audit-log", lambda *_: _envelope({"entries": list(reversed(self.audit))})),
        ]

    # -- prompts: a real artifact to release ------------------------------------------------
    def create_prompt(self, body: Any, token: str) -> httpx.Response:
        self.prompts[body["name"]] = {1: []}
        return _envelope({"name": body["name"], "version": 1, "alias_changed": False}, 201)

    def add_version(self, body: Any, token: str, name: str) -> httpx.Response:
        versions = self.prompts[name]
        versions[max(versions) + 1] = []
        return _envelope({"name": name, "version": max(versions), "alias_changed": False}, 201)

    def versions(self, body: Any, token: str, name: str) -> httpx.Response:
        rows = [{"name": name, "version": v, "aliases": a} for v, a in self.prompts[name].items()]
        return _envelope(sorted(rows, key=lambda r: -r["version"]))

    def promote(self, body: Any, token: str, name: str, alias: str) -> httpx.Response:
        for aliases in self.prompts[name].values():
            if alias in aliases:
                aliases.remove(alias)
        self.prompts[name][body["version"]].append(alias)
        self.timeline.append({"action": "promote_prompt", "entity_id": name})
        return _envelope({"name": name, "version": body["version"], "alias": alias})

    def delete_prompt(self, body: Any, token: str, name: str) -> httpx.Response:
        self.prompts.pop(name, None)
        return httpx.Response(204)

    # -- candidates -------------------------------------------------------------------------
    @staticmethod
    def _invalid(body: dict[str, Any]) -> str | None:
        allowed = {
            "name", "artifact_type", "artifact_ref", "version_ref", "criteria", "evidence",
            "required_score", "planned_action", "rollback_target",
        }  # fmt: skip
        if set(body) - allowed:
            return "Extra inputs are not permitted"
        if not body.get("criteria"):
            return "criteria: List should have at least 1 item"
        if not 0 <= body.get("required_score", 0.8) <= 1:
            return "required_score: Input should be less than or equal to 1"
        for item in body["criteria"]:
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", item["key"]):
                return "criteria.key: String should match pattern"
            if not item["weight"] > 0:
                return "criteria.weight: Input should be greater than 0"
            if not 0 <= item["score"] <= 1:
                return "criteria.score: Input should be less than or equal to 1"
        return None

    def _active_waivers(self, candidate: dict[str, Any]) -> set[str]:
        now = datetime.now(timezone.utc)
        keys = set()
        for waiver in candidate["waivers"]:
            expires = waiver.get("expires_at")
            if expires and _utc(expires) <= now:
                continue
            keys.add(waiver["criterion_key"])
        return keys

    def _score(self, candidate: dict[str, Any]) -> None:
        criteria = candidate["criteria"]
        weight = sum(c["weight"] for c in criteria)
        weighted = sum(c["weight"] * c["score"] for c in criteria)
        candidate["weighted_score"] = round(weighted / weight, 6) if weight else 0.0
        waived = self._active_waivers(candidate)
        candidate["blockers"] = [
            {
                "criterion_key": c["key"],
                "title": c["title"],
                "score": c["score"],
                "threshold": c.get("threshold", 0.5),
            }
            for c in criteria
            if c.get("blocking") and c["score"] < c.get("threshold", 0.5) and c["key"] not in waived
        ]
        ready = (
            candidate["weighted_score"] >= candidate["required_score"] and not candidate["blockers"]
        )
        candidate["status"] = "ready" if ready else "blocked"

    def create_candidate(self, body: Any, token: str) -> httpx.Response:
        if token not in OPERATOR_TOKENS:
            return _error(403, "missing required scope; expected one of ['caliber.operator']")
        invalid = self._invalid(body)
        if invalid:
            return _error(400, f"request body validation failed - {invalid}")
        candidate = {
            "candidate_id": self._id("RC"),
            "project_id": None,
            "visibility": "user",
            **body,
            "criteria": [
                {"threshold": 0.5, "blocking": False, "evidence_refs": [], **c}
                for c in body["criteria"]
            ],
            "evidence": body.get("evidence", []),
            "required_score": body.get("required_score", 0.8),
            "planned_action": body.get("planned_action", {}),
            "rollback_target": body.get("rollback_target", {}),
            "waivers": [],
            "owner": OPERATOR_TOKENS[token],
            "created_at": "2026-10-18T00:00:00Z",
            "updated_at": "2026-10-18T00:00:00Z",
        }
        self._score(candidate)
        self.candidates[candidate["candidate_id"]] = candidate
        self._log(OPERATOR_TOKENS[token], "create_release_candidate", candidate["candidate_id"])
        return _envelope(candidate, 201)

    def list_candidates(self, body: Any, token: str) -> httpx.Response:
        return _envelope(list(self.candidates.values()))

    def _signed(self, candidate: dict[str, Any]) -> bool:
        return candidate["status"] in {"signed_go", "signed_no_go"}

    def get_candidate(self, body: Any, token: str, candidate_id: str) -> httpx.Response:
        return _envelope(
            {
                "candidate": self.candidates[candidate_id],
                "signoffs": self.signoffs.get(candidate_id, []),
                "report_jobs": [
                    r for r in self.reports.values() if r["candidate_id"] == candidate_id
                ],
            }
        )

    def evaluate(self, body: Any, token: str, candidate_id: str) -> httpx.Response:
        if token not in OPERATOR_TOKENS:
            return _error(403, "missing required scope")
        candidate = self.candidates[candidate_id]
        if self._signed(candidate):
            return _error(409, "signed release candidates are immutable")
        self._score(candidate)
        self._log(OPERATOR_TOKENS[token], "evaluate_release_candidate", candidate_id)
        return _envelope(candidate)

    def waive(self, body: Any, token: str, candidate_id: str) -> httpx.Response:
        if token not in ADMIN_TOKENS:
            return _error(403, "missing required scope; expected one of ['caliber.admin']")
        if len(body.get("reason", "")) < 8:
            return _error(
                400,
                "request body validation failed - reason: String should have at least 8 characters",
            )
        candidate = self.candidates[candidate_id]
        if self._signed(candidate):
            return _error(409, "signed release candidates are immutable")
        if body["criterion_key"] not in {c["key"] for c in candidate["criteria"]}:
            return _error(400, "waiver criterion does not exist")
        if body.get("expires_at") and _utc(body["expires_at"]) <= datetime.now(timezone.utc):
            return _error(400, "waiver expires_at must be in the future")
        candidate["waivers"].append(
            {
                "criterion_key": body["criterion_key"],
                "reason": body["reason"],
                "expires_at": body.get("expires_at"),
                "approved_by": ADMIN_TOKENS[token],
                "approved_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self._score(candidate)
        self._log(ADMIN_TOKENS[token], "waive_release_blocker", candidate_id)
        return _envelope(candidate)

    def report(self, body: Any, token: str, candidate_id: str) -> httpx.Response:
        if token not in OPERATOR_TOKENS:
            return _error(403, "missing required scope")
        candidate = self.candidates[candidate_id]
        waived = self._active_waivers(candidate)
        results = []
        for c in candidate["criteria"]:
            passed = c["score"] >= c.get("threshold", 0.5)
            status = "passed" if passed else ("skipped" if c["key"] in waived else "failed")
            results.append({"name": c["title"], "status": status})
        digest = json.dumps(
            {k: candidate[k] for k in ("criteria", "waivers", "status", "weighted_score")},
            sort_keys=True,
        )
        job: dict[str, Any] = {
            "report_job_id": self._id("RRJ"),
            "candidate_id": candidate_id,
            "status": "completed",
            "format": "allure-json",
            "report": {
                "format": "allure-compatible-results",
                "candidate_snapshot_sha256": f"{abs(hash(digest)):064x}"[-64:],
                "results": results,
            },
        }
        self.reports[job["report_job_id"]] = job
        self._log(OPERATOR_TOKENS[token], "generate_release_allure_report", candidate_id)
        return _envelope(job, 201)

    def report_job(self, body: Any, token: str, job_id: str) -> httpx.Response:
        return _envelope(self.reports[job_id])

    def signoff(self, body: Any, token: str, candidate_id: str) -> httpx.Response:
        if token not in ADMIN_TOKENS:
            return _error(403, "missing required scope; expected one of ['caliber.admin']")
        if len(body.get("rationale", "")) < 8:
            return _error(
                400,
                "request body validation failed - rationale: String should have at least 8 chars",
            )
        candidate = self.candidates[candidate_id]
        if self._signed(candidate):
            return _error(409, "release candidate already has a final signoff")
        self._score(candidate)
        if body["decision"] == "go":
            if candidate["status"] != "ready":
                return _error(409, "go signoff requires a ready candidate")
            if not candidate["planned_action"] or not candidate["rollback_target"]:
                return _error(
                    409, "go signoff requires a planned release action and rollback target"
                )
        row = {
            "signoff_id": self._id("RSO"),
            "candidate_id": candidate_id,
            "decision": body["decision"],
            "rationale": body["rationale"],
            "decided_by": ADMIN_TOKENS[token],
            "candidate_snapshot": json.loads(json.dumps(candidate)),
        }
        candidate["status"] = f"signed_{body['decision']}"
        self.signoffs.setdefault(candidate_id, []).append(row)
        self._log(ADMIN_TOKENS[token], "signoff_release_candidate", candidate_id)
        return _envelope(row, 201)
