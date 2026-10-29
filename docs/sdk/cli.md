---
audience:
  - developer
  - operator
doc_type: how-to
product_area: sdk
stability: ga
prerequisites:
  - A running CALIBER deployment
reviewed_on: 2026-10-15
version_applicability: current main branch docs contract
tags:
  - sdk
  - cli
  - async
  - automation
---

# caliberctl and the async client

Two things built on the Python SDK: a command-line tool for operators, and an
asynchronous client for the case where being synchronous costs you a thread.

## caliberctl

```bash
# From the 0.1.0a1 release wheels; the packages are not on PyPI yet. caliber-cli needs
# caliber-sdk (>=0.1.0a1,<0.2), which pip finds in the same directory.
pip install --find-links ./wheels caliber-cli
export CALIBER_BASE_URL=https://caliber.example.com
export CALIBER_TOKEN=calpat_...
export CALIBER_PROJECT=PRJ-1   # commands that write into a project need one

caliberctl whoami
caliberctl workflow run --workflow-id WF-123 --input '{"claim_id": "C-1"}'
```

**Where the first token comes from.** `caliberctl token create` needs a token to run, so the first
one is issued in the browser: sign in, open **Settings → API tokens**, name it, choose the scopes it
needs, and copy the secret when it is shown (it is shown once). Use it as `CALIBER_TOKEN` or pass
`--token`. The same page lists your tokens and can rotate or revoke them; a revoked token stops
working immediately and `caliberctl whoami` then exits with the unauthenticated code.

`caliberctl` is a thin wrapper. It invents no backend semantics: where a command
appears to decide something, it is translating a state the server reported into
an exit code.

### Exit codes are the interface

A CLI for CI jobs that only ever exits 0 or 1 forces every caller to parse its
output. This one has ten codes:

| Code | Meaning |
| --- | --- |
| `0` | The command did what was asked. |
| `1` | Failure — API error, missing resource, refused permission. |
| `2` | The invocation was wrong, or a confirmation flag was missing. |
| `3` | **The work stopped because a person has to act.** Not an error. |
| `4` | A quality gate said no. The command worked; the answer was no. |
| `5` | The wait deadline passed with the work still in progress. |
| `6` | No usable credential. |
| `7` | A Change Request reviewer asked for changes (`changes_requested`). The fix is a new head, not a retry. |
| `8` | A Change Request fell behind its accepted base (`out_of_date`) and needs `workspace cr rebase`. |
| `9` | An import job or release operation landed on `reconcile_required`; it needs an explicit reconcile/observe call, not more waiting. |

Code 3 is why the others exist. CALIBER stops and asks people things — a
refinement job produces a candidate and waits, an Aria plan pauses on a question,
a release candidate sits unsigned. None of those is a failure and none is a
success, and collapsing them into either one produces a specific bad outcome:

- reported as **success**, a pipeline proceeds as though a human approved
  something;
- reported as **failure**, it flags a broken build for a system working
  correctly.

```bash
caliberctl job wait RFN-42
case $? in
  0) echo "applied" ;;
  3) echo "candidate ready — someone needs to review it" ;;
  5) echo "still running" ;;
  *) exit 1 ;;
esac
```

### `--json` means only JSON

In JSON mode stdout carries JSON and nothing else. Progress notes, warnings, and
errors all go to stderr, so this works without a filtering step:

```bash
TOKEN=$(caliberctl --json token create ci | jq -r .token)
```

The sharpest case is `token create`: it prints a human-facing warning that the
secret is shown only once, and that warning must not corrupt the stream a script
is reading the secret from.

### Nothing prompts

A command that would do something irreversible requires `--yes` and fails
without it, rather than asking. A tool that blocks on a TTY hangs a CI job
instead of failing it, which is much harder to diagnose.

```bash
caliberctl token revoke PAT-9         # exits 2, explains it needs --yes
caliberctl token revoke PAT-9 --yes   # revokes
```

### Commands

Global options go before the command: `--base-url` (default `$CALIBER_BASE_URL`),
`--token` (default `$CALIBER_TOKEN`; prefer the variable, because an argument is
visible in the process list), `--project` (default `$CALIBER_PROJECT`), `--json`,
`--quiet` and `--version`. A command that writes into a project's scope (`workflow run`, `gate-verdict record`,
`cookbook install`, `service publish`, and every `workspace` command) needs one, from `--project` or
`$CALIBER_PROJECT`, and exits 2 without it rather than writing into an unscoped library.

| Command | What it does |
| --- | --- |
| `whoami` | Identity behind the current credential. Exits 6 if anonymous. |
| `capabilities` | What the deployment supports. |
| `token list \| create \| rotate \| revoke` | Access token lifecycle. |
| `workflow list \| run \| status` | Run a workflow and wait for it. |
| `workflow deployments \| promote \| rollback \| promotions` | Deployment-alias governance: what's live, point an alias at a version, restore the prior one, and list pending/historical promotions. |
| `promotion approve \| reject` | Act on a pending gated promotion (see `workflow promote`). |
| `gate-verdict show \| record` | Advisory per-version evaluation verdicts (`prompt`/`workflow`/`skill`). |
| `job list \| wait` | Background jobs, including the ones that wait for you. |
| `release list \| sign` | Release candidates and go / no-go. **Record-only, off by default** (enable with `CALIBER_FEATURE_RELEASE_CANDIDATES=true` on the server): a signoff records a decision and does not deploy or roll back anything. |
| `cookbook list \| install` | Example workflows, with readiness checked first. **Preview, off by default** (enable with `CALIBER_FEATURE_COOKBOOKS=true` on the server). |
| `prompt list \| show` | Governed prompts and the registry coordinate each resolves to. |
| `service show \| publish \| unpublish` | A workflow's external HTTP service. |
| `plugin list` | Optimizers, and plugins installed but not enabled. |
| `workspace import \| package \| cr \| release` | Git-backed Workspace lifecycle: import jobs, revision packages, Change Request review, and environment releases. Every leaf requires `--project`. **Experimental:** when `CALIBER_FEATURE_RELEASE_CANDIDATES=true` and the owner worker/provider are enabled, a complete release-local pinned evaluation executes; incomplete plans fail closed. Evidence rows, expired-attempt recovery and governed apply remain later Beta slices. |

A command whose area is switched off on the server (`release`, `cookbook`, `workspace release`) fails with
exit code `1` and the server's message: a 404 whose `reason_code` is `feature_disabled` and whose `detail` names
the variable to set. `caliberctl capabilities` shows the same flags under `features`. See
[Feature flags](../start/alpha-scope.md#feature-flags).

Choices worth knowing about:

**`whoami` exits 6 on an anonymous identity.** `GET /me` reports rather than
requires, so an unusable credential returns 200 with `user_id: anonymous`. That
is right for the API and wrong for a script that ran `whoami` to confirm its
credential — exit 0 for "nobody" would let it proceed on a false premise.

**`workflow run` waits by default.** The non-waiting form is only useful to
something that will poll later, and a deploy script that forgot to wait reports
success for work that has not happened.

**`--idempotency-key` is passed through, never generated.** Submission is the one
mutating call the SDK will not retry on its own. A key this tool invented would
differ on your retry, which is the opposite of what the key is for.

**`service publish` and `service unpublish` both need `--yes`.** Publishing moves
invocation to an external endpoint authenticated by per-service tokens rather
than a user credential; unpublishing breaks whoever was calling it. Neither is
something to do by accident.

**`release sign --rationale` is required.** By the API, and therefore here. A CLI
that defaulted it to "signed via caliberctl" would manufacture exactly the record
the requirement exists to prevent.

**`workflow rollback` requires `--yes`.** It changes what a live deployment
alias serves right now, by popping that alias's checkpoint stack — the same
irreversible-action guard as `token revoke` and `service unpublish`.

**`workflow promote` may not promote immediately.** On a gated alias the
server creates a pending promotion instead of rotating the alias — check the
response, or `workflow promotions`, rather than assuming the alias moved. Act
on a pending one with `promotion approve` / `promotion reject`.

**`gate-verdict record --state fail` exits 4 (gate failed); `show` never does.**
`record` produces a decision the same way `release sign` does, so its answer
travels in the exit code. `show` is a plain read of whatever was last
recorded — it exits 0 regardless of `state`, and leaves interpreting the value
to the caller. Verdicts are advisory in v1: CALIBER never blocks alias
rotation on one by itself; this is release evidence, not an enforced gate.

## The async client

```python
import asyncio
from caliber_sdk.aio import AsyncCaliberClient


async def main() -> None:
    async with AsyncCaliberClient(token="calpat_...") as caliber:
        async for line in caliber.events.stream():
            print(line)


asyncio.run(main())
```

No extra dependency — `httpx` provides both clients. Construction, environment
fallbacks, and credential precedence are identical to the synchronous client.

### Why it exists

**Streaming.** An ordinary request reads its whole body before returning. An SSE
endpoint never ends, so consuming it synchronously occupies a thread for as long
as you want events. This is the case async is actually for.

**Concurrency.** Submitting forty workflow runs and awaiting them together is one
coroutine per run instead of forty threads:

```python
runs = await asyncio.gather(*(
    caliber.workflows.submit(workflow_version_id=version, input=claim)
    for claim in claims
))
```

### What it covers, and what it does not

`AsyncCaliberClient` gives you the transport, `raw`, async waiters, and the typed
surfaces where async changes the outcome: `me`, `capabilities_info`,
`projects`/`workspaces` (membership, files, imports, releases, release
operations), `workflows` (runs), `jobs`, and `events`.

It does **not** mirror all twenty-odd typed resource modules, and that is a
decision rather than an omission. Two hand-written copies of the same resource
tree is two places for a path to be renamed and one place for it to be forgotten.
This repository's history includes several wrong paths caught only by driving the
SDK against the real server; a second, less-exercised copy would carry exactly
that risk with none of that coverage.

Nothing is unreachable either way — `client.raw` reaches any endpoint with the
same auth, retries, envelope handling, and typed errors, and
`caliber_sdk.models.decode` works on any payload:

```python
from caliber_sdk.models import Prompt, decode

payload = await caliber.raw.get("/prompts")
prompts = [decode(Prompt, item) for item in payload]
```

### One policy, not two

The async transport necessarily restates the *shape* of the request loop — an
`await` cannot be injected into a synchronous function. It must not restate the
*decisions*, so it imports them: which methods may be retried, which statuses are
worth retrying, how backoff grows, how `Retry-After` is read, how a body is
decoded, when an envelope is unwrapped, what a CSRF failure looks like, and what
the waiter defaults are.

`test_async_parity.py` asserts that by **identity**, not equality — two equal sets
today can diverge on the next edit and nothing would notice. It also asserts that
`time.sleep(` appears nowhere in the async modules, because blocking the loop is
the one thing an async client must not do.

Practically, this means `except WaitTimeout` catches both clients, a script ported
from one to the other keeps the same timeout, and both authenticate identically.
