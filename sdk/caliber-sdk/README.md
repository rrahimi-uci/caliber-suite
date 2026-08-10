# caliber-sdk

A typed Python client for the [CALIBER](https://github.com/rrahimi-uci/caliber-suite)
management API.

Installing it does **not** install the CALIBER server. No `mlflow`, no
`sqlalchemy`, no `starlette` — just `httpx`. That is the reason this is a
separate distribution rather than a module inside the server package.

## Install

```bash
pip install caliber-sdk
```

## Quick start

```python
from caliber_sdk import CaliberClient

with CaliberClient("https://caliber.example.com", token="calpat_...") as caliber:
    print(caliber.whoami())
```

Configuration falls back to the environment, so a CI job needs no argument
plumbing of its own:

| Variable | Meaning |
| --- | --- |
| `CALIBER_BASE_URL` | deployment URL |
| `CALIBER_TOKEN` | personal access token |
| `CALIBER_PROJECT` | active project/workspace |
| `CALIBER_USER` | trusted-header identity (only in `trusted_header` deployments) |

## Getting a token

Personal access tokens are issued by the API and returned **once**:

```python
created = caliber.raw.post("/auth/tokens", json={"name": "ci", "scopes": ["caliber.operator"]})
print(created["token"])  # calpat_... — store it now; it is never shown again
```

Scopes are a **ceiling**, not a grant: the effective authority is what the token
requested intersected with what its owner holds at request time. A token cannot
exceed its owner, and demoting the owner narrows the token immediately. Omit
`scopes` to inherit the owner's.

## What the client handles for you

- **Envelopes.** Responses wrapped as `{"data": ...}` are unwrapped; genuinely
  unenveloped bodies (the OpenAPI document) are passed through untouched.
- **Errors.** Non-2xx becomes a typed exception carrying status, detail, method,
  URL, and a request id. Structured validation failures name the offending field.
- **Retries.** Transient failures are retried with capped exponential backoff —
  on idempotent methods only. A `POST` is never retried, because the SDK cannot
  know whether the failure happened before or after the write took effect.
- **CSRF.** A write refused for want of a token bootstraps one and replays
  exactly once. A genuine permission failure is not mistaken for CSRF.
- **Project scoping.** `X-CALIBER-Project` on every request when set.

## Long-running operations

Runs, jobs, and evaluations are asynchronous. The waiters poll with capped
backoff rather than a bare loop, and never sleep past your deadline:

```python
from caliber_sdk import wait_for_terminal_state

run = wait_for_terminal_state(
    lambda: caliber.raw.get(f"/workflow-runs/{run_id}"),
    timeout=600,
)
```

A waiter never decides what "finished" means when you care about the
distinction — pass your own predicate to `wait_for` if a failed run is a
success for your script.

## Stability

The server publishes which API tags are `ga`, `beta`, or `internal`, and the
client reports them, so a script can check rather than assume:

```python
if "knowledge-bases" in caliber.stability["ga"]:
    ...
```

## Anything not yet modelled

`caliber.raw` reaches any endpoint under `/ajax-api/2.0/mlflow/caliber`:

```python
caliber.raw.get("/prompts")
caliber.raw.post("/cookbooks/01/install", json={"name": "My install"})
for item in caliber.raw.paginate("/workflows", limit=50):
    ...
```

This is deliberate and permanent. A typed façade that lags the server would
otherwise make new endpoints unreachable until the SDK catches up.

## Development

```bash
pip install -e ".[dev]"
pytest
mypy
ruff check .
python -m build
```

## Status

Alpha. The transport, auth, error, and waiter contracts above are stable;
typed resource modules are being added milestone by milestone.

## License

Apache-2.0
