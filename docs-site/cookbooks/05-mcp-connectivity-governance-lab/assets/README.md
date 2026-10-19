# SCN-05 assets

Everything the lab runs, from the [README](../README.md). There is nothing to author: you connect the lab's own MCP server and govern the
tools it discovers.

| Folder | Files | Used for |
| --- | --- | --- |
| [`server/`](server/) | `repo_tracker_mcp.py` | the MCP server CALIBER starts over stdio (also speaks streamable HTTP, optionally with a bearer token) |
| [`dataset/`](dataset/) | `repositories.json`, `issues.json` | the data the server serves (24 repositories, 36 issues) |
| [`dataset/`](dataset/) | `tool-classification.jsonl` | one row per tool: `allowed`, `side_effect_level`, `requires_approval`, `rate_limit_per_minute`, `why`. The literal `PATCH .../tools/{tool}/policy` bodies are these fields |
| [`dataset/`](dataset/) | `calibration-cases.jsonl` | 28 cases (`id`, `tags`, `tool_name`, `name`, `input`, `assertion`): 16 for `search_repositories`, 8 for `list_issues`, 4 for `get_issue`. When posting, send `{name, input, assertion}` with `name` as `"<id> <name>"` |
| [`dataset/`](dataset/) | `invoke-checks.jsonl` | 10 refusals and failures: the call, the error it must give (`error_contains`), whether the server executed it (`executed`) and whether it leaves a trace (`trace`) |
| [`workflow/`](workflow/) | `file-issue.workflow.yaml` | a one-tool workflow; fill `__SERVER_ID__`, `__TOOL__`, `__LEVEL__`, `__APPROVAL__` (the script does) |
| [`real-world/`](real-world/) | `github-mcp.server.json` | the body for GitHub's official remote MCP server. **Not run in this lab** (network and credentials) |

## The server's tools

| Tool | Side effect | Classification the lab saves | What it does |
| --- | --- | --- | --- |
| `search_repositories` | read | allowed | GitHub-style query: free text plus `language:`, `topic:`, `org:`, `archived:`, `stars:>N` |
| `list_issues` | read | allowed | the issues of one repository, by state |
| `get_issue` | read | allowed | one issue |
| `scan_dependencies` | read | allowed, **3 calls a minute** | a slow upstream call: `delay_seconds` simulates latency (a value over 10 times out in the Playground) |
| `issue_write` | write | allowed, **requires approval** | files an issue |
| `create_release` | external action | **blocked** (`allowed: false`) | "publishes" a release and "notifies" a channel |

## The ledger

CALIBER starts a stdio server fresh for every call, so the server keeps its state in a file: `--ledger /absolute/path`. Every tool that
actually executes appends `{"tool", "ok", "arguments"}`; `list_issues` and `get_issue` replay the successful `issue_write` lines. A call
CALIBER refuses never starts the server, so it never appears in the ledger. That is the proof the lab uses, and it does not depend on
CALIBER's own answer.

Conventions: the assertion types are the three real ones: `no_error` (the call returned no error), `output_contains` (a substring of the
output rendered as JSON with sorted keys and `": "` separators, e.g. `"total_count": 8`) and `equals` (the whole rendering). A call that
errors fails every assertion before it is evaluated, so there is no "expects an error" case: errors are checked with `invoke-checks.jsonl`
through `invoke-tool`, not through calibration.
