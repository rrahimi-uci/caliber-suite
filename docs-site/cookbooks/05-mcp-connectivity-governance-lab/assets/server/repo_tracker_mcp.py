"""A small, real MCP server for the lab: an engineering "repo tracker".

It stands in for a hosted MCP server (the lab's real-world variant is GitHub's) so the lab can run with
no network and no credentials. It speaks the actual protocol through the official ``mcp`` SDK, over
streamable HTTP (the transport CALIBER's catalog uses for remote servers) or stdio, and it is
deliberately shaped like a work tracker: read tools, a write tool and an external-action tool.

Tools
-----
``search_repositories``   read             GitHub-style query: free text plus ``language:`` ``topic:`` ``org:``
                                           ``archived:`` ``stars:>N`` qualifiers
``list_issues``           read             issues of one repository, filtered by state
``get_issue``             read             one issue
``scan_dependencies``     read             a deliberately slow upstream call (``delay_seconds``) for the timeout drill
``issue_write``           write            files an issue (in memory)
``create_release``        external_action  "publishes" a release and "notifies" a channel (in memory)

State is a ledger file, not memory, because CALIBER starts a stdio server fresh for every call: pass
``--ledger /absolute/path`` (or set ``REPO_TRACKER_LEDGER``) and every tool that actually executes appends one JSON line
(``{"tool", "ok", "arguments"}``; ``ok`` is whether it succeeded). Issues and releases the write tools create are replayed from that ledger by
``list_issues`` and ``get_issue``. The ledger is how the lab proves, out of band and without trusting
CALIBER's own answer, that a refused call never reached the server (no line was written). With no
ledger nothing is recorded and the write tools only return a result.

Over streamable HTTP, ``GET /stats`` summarises the same ledger and ``GET /healthz`` is the readiness probe.
When ``REPO_TRACKER_TOKEN`` is set, every other request needs ``Authorization: Bearer <token>``; a missing
or wrong token is answered 401 before MCP sees it.

Run it::

    python repo_tracker_mcp.py --transport stdio --ledger /abs/ledger.jsonl    # what the lab registers
    python repo_tracker_mcp.py --transport streamable-http --port 8765        # http://127.0.0.1:8765/mcp
"""

from __future__ import annotations

import argparse
import functools
import hmac
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

DATASET = Path(__file__).resolve().parent.parent / "dataset"
MAX_DELAY_SECONDS = 30
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9 _.-]{0,39}$")

REPOSITORIES: list[dict[str, Any]] = json.loads((DATASET / "repositories.json").read_text())
ISSUES: list[dict[str, Any]] = json.loads((DATASET / "issues.json").read_text())
REPO_NAMES = {repo["full_name"] for repo in REPOSITORIES}

_LOCK = threading.Lock()


def _ledger_path() -> Path | None:
    raw = os.environ.get("REPO_TRACKER_LEDGER", "").strip()
    return Path(raw) if raw else None


def _record(tool: str, arguments: dict[str, Any], *, ok: bool) -> None:
    """Append one executed call to the ledger (a no-op when no ledger is configured)."""
    path = _ledger_path()
    if path is None:
        return
    with _LOCK, path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"tool": tool, "ok": ok, "arguments": arguments}, sort_keys=True) + "\n"
        )


def _ledger() -> list[dict[str, Any]]:
    path = _ledger_path()
    if path is None or not path.exists():
        return []
    with _LOCK:
        lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _created(tool: str) -> list[dict[str, Any]]:
    """Arguments of every call of ``tool`` that executed and succeeded."""
    return [entry["arguments"] for entry in _ledger() if entry["tool"] == tool and entry["ok"]]


def _keyed(tool: str, key: str | None) -> tuple[int, dict[str, Any]] | None:
    """The first successful call of ``tool`` carrying ``key`` (its position among the tool's
    successful calls, and its arguments), or None. This is what makes a retried write safe:
    an MCP client or orchestrator may re-issue a call whose answer was lost, and the second
    call must not file a second issue or publish a second release."""
    if not key:
        return None
    for position, args in enumerate(_created(tool)):
        if args.get("idempotency_key") == key:
            return position, args
    return None


def _all_issues() -> list[dict[str, Any]]:
    """Seed issues plus the ones issue_write filed, numbered the way the write tool numbered them."""
    issues = [dict(issue) for issue in ISSUES]
    for args in _created("issue_write"):
        number = 1 + max([i["number"] for i in issues if i["repo"] == args["repo"]], default=0)
        issues.append(
            {
                "repo": args["repo"],
                "number": number,
                "title": args["title"],
                "state": "open",
                "labels": list(args.get("labels") or []),
                "author": "caliber-lab",
                "body": args.get("body", ""),
            }
        )
    return issues


def _require_repo(repo: str) -> None:
    if repo not in REPO_NAMES:
        raise ToolError(f"repository {repo!r} was not found")


def _parse_query(query: str) -> tuple[list[str], dict[str, str]]:
    words: list[str] = []
    qualifiers: dict[str, str] = {}
    for token in query.split():
        key, sep, value = token.partition(":")
        if sep and key in {"language", "topic", "org", "archived", "stars"}:
            if not value:
                raise ToolError(f"qualifier {key!r} needs a value")
            qualifiers[key] = value
        elif sep and re.fullmatch(r"[a-z]+", key):
            raise ToolError(f"unknown search qualifier {key!r}")
        else:
            words.append(token.lower())
    if "archived" in qualifiers and qualifiers["archived"] not in {"true", "false"}:
        raise ToolError("archived: must be true or false")
    if "stars" in qualifiers and not re.fullmatch(r"(>=|<=|>|<)?\d+", qualifiers["stars"]):
        raise ToolError(f"stars: expects a number such as stars:>100, got {qualifiers['stars']!r}")
    return words, qualifiers


def _stars_match(stars: int, expression: str) -> bool:
    match = re.fullmatch(r"(>=|<=|>|<)?(\d+)", expression)
    assert match is not None  # validated in _parse_query
    operator, number = match.group(1) or "=", int(match.group(2))
    return {
        ">": stars > number,
        ">=": stars >= number,
        "<": stars < number,
        "<=": stars <= number,
        "=": stars == number,
    }[operator]


def _recorded(tool: str) -> Any:
    """Decorator: append every executed call to the ledger, with whether it succeeded."""

    def wrap(fn: Any) -> Any:
        @functools.wraps(fn)
        def inner(**arguments: Any) -> Any:
            try:
                result = fn(**arguments)
            except Exception:
                _record(tool, arguments, ok=False)
                raise
            # A replayed write (same idempotency_key) executed nothing: it is on the ledger under
            # its own name, so the tool's real executions, and the issues replayed from them, are
            # not counted twice.
            replayed = isinstance(result, dict) and result.get("replayed") is True
            _record(f"{tool}.replay" if replayed else tool, arguments, ok=True)
            return result

        return inner

    return wrap


def build_server() -> MCPServer:
    server = MCPServer(
        "repo-tracker",
        instructions=(
            "An engineering repo tracker for the CALIBER MCP governance lab. "
            "Read tools search and list; issue_write and create_release change state."
        ),
    )

    @server.tool()
    @_recorded("search_repositories")
    def search_repositories(
        query: Annotated[
            str,
            Field(
                description="Free text plus qualifiers: language:, topic:, org:, archived:, stars:>N"
            ),
        ],
        per_page: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> dict[str, Any]:
        """Search repositories. Returns {total_count, items: [{full_name, language, stars, ...}]}."""
        if not query.strip():
            raise ToolError("query must not be empty")
        words, qualifiers = _parse_query(query)
        items = []
        for repo in REPOSITORIES:
            haystack = " ".join(
                [repo["full_name"], repo["description"], " ".join(repo["topics"])]
            ).lower()
            if not all(word in haystack for word in words):
                continue
            if (
                "language" in qualifiers
                and repo["language"].lower() != qualifiers["language"].lower()
            ):
                continue
            if "topic" in qualifiers and qualifiers["topic"].lower() not in repo["topics"]:
                continue
            if "org" in qualifiers and not repo["full_name"].startswith(qualifiers["org"] + "/"):
                continue
            if "archived" in qualifiers and repo["archived"] != (qualifiers["archived"] == "true"):
                continue
            if "stars" in qualifiers and not _stars_match(repo["stars"], qualifiers["stars"]):
                continue
            items.append(repo)
        items.sort(key=lambda repo: (-repo["stars"], repo["full_name"]))
        return {"total_count": len(items), "items": items[:per_page]}

    @server.tool()
    @_recorded("list_issues")
    def list_issues(
        repo: Annotated[str, Field(description="Repository as owner/name, e.g. acme/payments-api")],
        state: Literal["open", "closed", "all"] = "open",
    ) -> dict[str, Any]:
        """List the issues of one repository. Returns {repo, state, total_count, issues}."""
        _require_repo(repo)
        issues = [
            issue
            for issue in _all_issues()
            if issue["repo"] == repo and (state == "all" or issue["state"] == state)
        ]
        return {
            "repo": repo,
            "state": state,
            "total_count": len(issues),
            "issues": issues,
        }

    @server.tool()
    @_recorded("get_issue")
    def get_issue(
        repo: Annotated[str, Field(description="Repository as owner/name")],
        number: Annotated[int, Field(ge=1)],
    ) -> dict[str, Any]:
        """Fetch one issue by number."""
        _require_repo(repo)
        for issue in _all_issues():
            if issue["repo"] == repo and issue["number"] == number:
                return dict(issue)
        raise ToolError(f"issue #{number} was not found in {repo}")

    @server.tool()
    @_recorded("scan_dependencies")
    def scan_dependencies(
        repo: Annotated[str, Field(description="Repository as owner/name")],
        delay_seconds: Annotated[
            float,
            Field(ge=0, le=MAX_DELAY_SECONDS, description="Simulated upstream latency"),
        ] = 0,
    ) -> dict[str, Any]:
        """Scan a repository's dependencies (a slow upstream call: use delay_seconds to simulate it)."""
        _require_repo(repo)
        time.sleep(delay_seconds)
        language = next(r["language"] for r in REPOSITORIES if r["full_name"] == repo)
        return {
            "repo": repo,
            "language": language,
            "dependencies_scanned": 40 + len(repo),
            "vulnerable": 0,
        }

    @server.tool()
    @_recorded("issue_write")
    def issue_write(
        repo: Annotated[str, Field(description="Repository as owner/name")],
        title: Annotated[str, Field(min_length=3, max_length=200)],
        body: Annotated[str, Field(max_length=4000)] = "",
        labels: list[str] | None = None,
        idempotency_key: Annotated[
            str | None,
            Field(
                min_length=8,
                max_length=128,
                description=(
                    "Send the same key when you retry: the issue is filed once and the retry "
                    "returns the original number with replayed=true."
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        """File a new issue. A WRITE: it changes the tracker. Safe to retry with an idempotency_key."""
        _require_repo(repo)
        for label in labels or []:
            if not LABEL_RE.match(label):
                raise ToolError(f"invalid label {label!r}")
        earlier = _keyed("issue_write", idempotency_key)
        if earlier is not None:
            position, prior = earlier
            if prior["repo"] != repo or prior["title"] != title:
                raise ToolError(
                    f"idempotency_key {idempotency_key!r} was already used for a different issue"
                )
            same_repo = [a for a in _created("issue_write")[: position + 1] if a["repo"] == repo]
            base = max([i["number"] for i in ISSUES if i["repo"] == repo], default=0)
            return {
                "created": False,
                "replayed": True,
                "repo": repo,
                "number": base + len(same_repo),
                "title": title,
            }
        number = 1 + max([i["number"] for i in _all_issues() if i["repo"] == repo], default=0)
        return {"created": True, "repo": repo, "number": number, "title": title}

    @server.tool()
    @_recorded("create_release")
    def create_release(
        repo: Annotated[str, Field(description="Repository as owner/name")],
        tag: Annotated[str, Field(pattern=r"^v\d+\.\d+\.\d+$")],
        notes: Annotated[str, Field(max_length=4000)] = "",
        idempotency_key: Annotated[
            str | None,
            Field(
                min_length=8,
                max_length=128,
                description=(
                    "Send the same key when you retry: the release is published and announced "
                    "once and the retry returns the original result with replayed=true."
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Publish a release and notify the release channel. An EXTERNAL ACTION: it cannot be undone.

        Safe to retry with an idempotency_key; without one, publishing the same tag twice is refused.
        """
        _require_repo(repo)
        earlier = _keyed("create_release", idempotency_key)
        if earlier is not None:
            _, prior = earlier
            if prior["repo"] != repo or prior["tag"] != tag:
                raise ToolError(
                    f"idempotency_key {idempotency_key!r} was already used for a different release"
                )
            return {
                "published": False,
                "replayed": True,
                "repo": repo,
                "tag": tag,
                "notified": "#releases",
            }
        if any(a["repo"] == repo and a["tag"] == tag for a in _created("create_release")):
            raise ToolError(f"release {tag} already exists in {repo}")
        return {"published": True, "repo": repo, "tag": tag, "notified": "#releases"}

    return server


def _stats() -> dict[str, Any]:
    entries = _ledger()
    calls: dict[str, int] = {}
    for entry in entries:
        calls[entry["tool"]] = calls.get(entry["tool"], 0) + 1
    return {
        "calls": calls,
        "issues_created": len(_created("issue_write")),
        "releases_created": len(_created("create_release")),
    }


class _Gateway:
    """Pure-ASGI wrapper: bearer auth, ``/healthz`` and ``/stats`` in front of the MCP app."""

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token

    async def _respond(self, send: Any, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def _authorized(self, scope: Any) -> bool:
        if not self.token:
            return True
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        supplied = headers.get(b"authorization", b"").decode("latin-1")
        return hmac.compare_digest(supplied, f"Bearer {self.token}")

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["path"] == "/healthz":
            await self._respond(send, 200, {"status": "ok"})
            return
        if not self._authorized(scope):
            await self._respond(send, 401, {"error": "missing or invalid bearer token"})
            return
        if scope["path"] == "/stats":
            await self._respond(send, 200, _stats())
            return
        await self.app(scope, receive, send)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--ledger",
        default="",
        help="absolute path of the call ledger (default: $REPO_TRACKER_LEDGER, else none)",
    )
    args = parser.parse_args()
    if args.ledger:
        os.environ["REPO_TRACKER_LEDGER"] = args.ledger
    server = build_server()
    if args.transport == "stdio":
        server.run(transport="stdio")
        return
    import uvicorn

    app = server.streamable_http_app(json_response=True, stateless_http=True)
    uvicorn.run(
        _Gateway(app, os.environ.get("REPO_TRACKER_TOKEN", "")),
        host=args.host,
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
