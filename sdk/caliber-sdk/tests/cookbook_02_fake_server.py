"""A small stateful stand-in for the skills routes cookbook 02 calls.

``test_cookbook_examples.py`` runs the real example script against this instead of a server, so
the script's call sequence and the shape of what it sends stay covered offline. The behaviour is a
deliberately thin imitation: the real selector, the real refinement job and the real package
format are exercised by ``caliber/tests/test_cookbook_02_trigger_dataset.py`` and by running the
script against a live server (see the lab README). What this fake does faithfully is the
*contract* the script depends on: the status codes and the response fields it reads.
"""

from __future__ import annotations

import email
import email.policy
import io
import json
import re
import zipfile
from email.message import Message
from typing import Any

import httpx

NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: Cases the starter trigger phrases decide wrongly (verified against the real selector in
#: ``caliber/tests/test_cookbook_02_trigger_dataset.py``); once the tightened phrases are in place
#: the fake answers every case as the dataset expects.
STARTER_MISSES = frozenset({"T14", "T23", "T27", "T28", "T29", "T30", "T31", "T32", "T34", "T35"})
TIGHTENED_MARKER = "kubernetes"


def _envelope(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"data": data})


def _error(status: int, detail: str, *, loc: str | None = None) -> httpx.Response:
    body: dict[str, Any] = {"detail": detail, "status_code": status}
    if loc is not None:
        body["detail"] = "request body validation failed"
        body["errors"] = [{"loc": [loc], "msg": detail, "type": "value_error"}]
    return httpx.Response(status, json=body)


class FakeSkillServer:
    """Routes ``/skills``, ``/jobs`` and the few auth/me calls the example makes."""

    def __init__(self, cases: list[dict[str, Any]], *, viewer: bool = False) -> None:
        self.viewer = viewer
        self.expected = {c["inputs"]["user_message"]: c for c in cases}
        self.skills: dict[str, dict[str, Any]] = {}
        self.versions: dict[str, list[dict[str, Any]]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.baselines: dict[str, str | None] = {}
        self.calls: list[str] = []

    # -- plumbing ------------------------------------------------------------------------
    def client(self, caliber_client_cls: Any) -> Any:
        return caliber_client_cls(
            "https://caliber.test",
            token="calpat_test",
            http_client=httpx.Client(transport=httpx.MockTransport(self.handle)),
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.rsplit("/caliber", 1)[-1]
        self.calls.append(f"{request.method} {path}")
        body: Any = None
        if request.content and request.headers.get("content-type", "").startswith(
            "application/json"
        ):
            body = json.loads(request.content)
        if self.viewer and request.method != "GET":
            return _error(403, "missing required scope; expected one of ['caliber.operator']")
        route = self._route(request.method, path)
        if route is None:  # pragma: no cover - guards against an unstubbed call
            raise AssertionError(f"example called an unstubbed route: {request.method} {path}")
        response: httpx.Response = route(request, body)
        return response

    def _route(self, method: str, path: str) -> Any:
        static = {
            ("GET", "/me"): lambda r, b: _envelope({"user_id": "@alice"}),
            ("POST", "/skills"): self.create,
            ("GET", "/skills"): self.list_skills,
            ("POST", "/skills/test-runs"): self.create_run,
            ("POST", "/skills/import-package.zip"): self.import_zip,
        }
        if (method, path) in static:
            return static[(method, path)]
        match = re.fullmatch(r"/skills/([^/]+)(?:/(.+))?", path)
        if match:
            skill_id, tail = match.groups()
            table = {
                ("GET", None): self.get,
                ("PATCH", None): self.update,
                ("POST", "test-render"): self.render,
                ("POST", "test-selection"): self.select,
                ("POST", "baseline"): self.baseline,
                ("GET", "workspace"): self.workspace,
                ("POST", "calibrate"): self.calibrate,
                ("POST", "rollback"): self.rollback,
                ("GET", "versions"): self.list_versions,
                ("GET", "package.zip"): self.package_zip,
            }
            handler = table.get((method, tail))
            if handler is not None:
                return lambda r, b: handler(skill_id, r, b)
        match = re.fullmatch(r"/jobs/([^/]+)(?:/(apply))?", path)
        if match:
            job_id, tail = match.groups()
            if method == "GET" and tail is None:
                return lambda r, b: _envelope(self.jobs[job_id])
            if method == "POST" and tail == "apply":
                return lambda r, b: self.apply(job_id)
        return None

    # -- skills --------------------------------------------------------------------------
    def create(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        allowed = {
            "name",
            "owner",
            "content",
            "summary",
            "description",
            "category",
            "tags",
            "skill_metadata",
        }
        extra = set(body) - allowed
        if extra:
            return _error(400, "Extra inputs are not permitted", loc=sorted(extra)[0])
        if not NAME_PATTERN.fullmatch(body.get("name", "")):
            return _error(
                400, "String should match pattern '^[a-z0-9]+(?:-[a-z0-9]+)*$'", loc="name"
            )
        if not body.get("content"):
            return _error(400, "String should have at least 1 character", loc="content")
        if body["name"].startswith("claude"):
            return _error(400, "skill names starting with 'claude' are reserved")
        if "<" in body.get("summary", ""):
            return _error(400, "summary must not contain XML angle brackets (security restriction)")
        if any(s["name"] == body["name"] for s in self.skills.values()):
            return _error(409, f"skill name {body['name']!r} is already in use")
        return _envelope(self._store(body), 201)

    def _store(self, body: dict[str, Any]) -> dict[str, Any]:
        skill_id = f"SK-{len(self.skills) + 1:02d}"
        skill = {
            "skill_id": skill_id,
            "name": body["name"],
            "description": body.get("description", ""),
            "summary": body.get("summary", ""),
            "content": body["content"],
            "owner": "@alice",
            "category": body.get("category", "custom"),
            "tags": body.get("tags", []),
            "skill_metadata": body.get("skill_metadata", {}),
            "status": "active",
            "version": 1,
        }
        self.skills[skill_id] = skill
        self.versions[skill_id] = [{"version_number": 1, "content": skill["content"]}]
        self.baselines[skill_id] = None
        return skill

    def get(self, skill_id: str, request: httpx.Request, body: Any) -> httpx.Response:
        return _envelope(self.skills[skill_id])

    def list_skills(self, request: httpx.Request, body: Any) -> httpx.Response:
        status = request.url.params.get("status", "active")
        return _envelope([s for s in self.skills.values() if status in ("all", s["status"])])

    def update(self, skill_id: str, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        skill = self.skills[skill_id]
        skill.update(body)
        if "summary" in body or "content" in body:
            skill["version"] += 1
            self.versions[skill_id].append(
                {"version_number": skill["version"], "content": skill["content"]}
            )
        return _envelope(skill)

    def render(self, skill_id: str, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        content = self.skills[skill_id]["content"]
        detected: list[str] = []
        for name in re.findall(r"\{\{\s*(\w+)\s*\}\}", content):
            if name not in detected:
                detected.append(name)
        variables = body.get("variables", {})
        rendered = re.sub(
            r"\{\{\s*(\w+)\s*\}\}",
            lambda m: str(variables.get(m.group(1), m.group(0))),
            content,
        )
        return _envelope(
            {
                "skill_id": skill_id,
                "rendered_content": rendered,
                "original_content": content,
                "detected_variables": detected,
                "unresolved_variables": [n for n in detected if n not in variables],
                "variables_applied": variables,
                "word_count": len(rendered.split()),
            }
        )

    def select(self, skill_id: str, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        case = self.expected[body["query"]]
        exclusions = self.skills[skill_id]["skill_metadata"].get("test_triggers", {})
        tightened = TIGHTENED_MARKER in exclusions.get("should_not_trigger", [])
        selected = case["expectations"]["is_selected"]
        if not tightened and case["id"] in STARTER_MISSES:
            selected = not selected
        return _envelope(
            {
                "skill_id": skill_id,
                "skill_name": self.skills[skill_id]["name"],
                "is_selected": selected,
                "selection_score": 7.0 if selected else 0.0,
                "selection_reason": "auto:tag+summary" if selected else "",
            }
        )

    def create_run(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        assert body["kind"] == "selection" and body["results"]
        run_id = f"SKR-{len(self.runs) + 1:02d}"
        self.runs[run_id] = body
        return _envelope({"test_run_id": run_id}, 201)

    def baseline(
        self, skill_id: str, request: httpx.Request, body: dict[str, Any]
    ) -> httpx.Response:
        assert body["test_run_id"] in self.runs
        self.baselines[skill_id] = body["test_run_id"]
        return _envelope({"baseline_run_id": body["test_run_id"]})

    def workspace(self, skill_id: str, request: httpx.Request, body: Any) -> httpx.Response:
        return _envelope({"baseline_run_id": self.baselines[skill_id]})

    def calibrate(self, skill_id: str, request: httpx.Request, body: Any) -> httpx.Response:
        skill = self.skills[skill_id]
        job_id = f"RFN-{len(self.jobs) + 1:02d}"
        self.jobs[job_id] = {
            "job_id": job_id,
            "status": "candidate_ready",
            "candidate": {
                "content": skill["content"] + "\n\n# Refinement added by SkillMetaPrompt\n",
                "baseline_content": skill["content"],
            },
            "eval_results": {
                "simulated": True,
                "provider": "fake",
                "n_examples": 0,
                "gate": {"passed": True},
            },
        }
        self.jobs[job_id]["skill_id"] = skill_id
        return _envelope({"job": {"job_id": job_id}, "item": {"item_id": "FB-01"}}, 201)

    def apply(self, job_id: str) -> httpx.Response:
        job = self.jobs[job_id]
        skill = self.skills[job["skill_id"]]
        skill["content"] = job["candidate"]["content"]
        skill["version"] += 1
        self.versions[skill["skill_id"]].append(
            {"version_number": skill["version"], "content": skill["content"]}
        )
        job["status"] = "applied"
        return _envelope({"job_id": job_id, "status": "applied"})

    def rollback(self, skill_id: str, request: httpx.Request, body: Any) -> httpx.Response:
        skill = self.skills[skill_id]
        prior = self.versions[skill_id][-2]
        skill["content"] = prior["content"]
        skill["version"] += 1
        self.versions[skill_id].append(
            {"version_number": skill["version"], "content": skill["content"]}
        )
        return _envelope(skill)

    def list_versions(self, skill_id: str, request: httpx.Request, body: Any) -> httpx.Response:
        return _envelope(list(reversed(self.versions[skill_id])))

    # -- packages ------------------------------------------------------------------------
    def package_zip(self, skill_id: str, request: httpx.Request, body: Any) -> httpx.Response:
        skill = self.skills[skill_id]
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr(
                f"{skill['name']}/SKILL.md",
                f"---\nname: {skill['name']}\ndescription: {skill['summary']}\n---\n\n"
                f"{skill['content']}\n",
            )
            archive.writestr(f"{skill['name']}/agents/openai.yaml", "interface: {}\n")
        return httpx.Response(
            200, content=stream.getvalue(), headers={"content-type": "application/zip"}
        )

    def import_zip(self, request: httpx.Request, body: Any) -> httpx.Response:
        message = email.message_from_bytes(
            b"Content-Type: "
            + request.headers["content-type"].encode()
            + b"\r\n\r\n"
            + request.content,
            policy=email.policy.HTTP,
        )
        fields: dict[str, Any] = {}
        for part in message.iter_parts():
            assert isinstance(part, Message)
            field = str(part.get_param("name", header="content-disposition"))
            fields[field] = part.get_payload(decode=True)
        archive = zipfile.ZipFile(io.BytesIO(fields["file"]))
        skill_md = archive.read(
            next(n for n in archive.namelist() if n.endswith("SKILL.md"))
        ).decode()
        name = re.search(r"^name: (.+)$", skill_md, re.M).group(1)  # type: ignore[union-attr]
        content = skill_md.split("---\n\n", 1)[1].strip()
        strategy = fields["conflict_strategy"].decode()
        if strategy == "rename":
            name = fields["rename_to"].decode()
        elif any(s["name"] == name for s in self.skills.values()):
            return _error(
                409, f"skill name {name!r} already exists; choose rename or merge explicitly"
            )
        skill = self._store({"name": name, "content": content, "summary": content[:20]})
        return _envelope(skill, 201)
