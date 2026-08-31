# caliberctl

Non-interactive operator commands for CALIBER, on top of `caliber-sdk`.

```bash
pip install caliber-cli
export CALIBER_BASE_URL=https://caliber.example.com
export CALIBER_TOKEN=calpat_...

caliberctl whoami
caliberctl workflow run --workflow-id WF-123 --input '{"claim_id": "C-1"}'
```

## Exit codes are the interface

A CLI that only ever exits 0 or 1 forces every caller to parse its output. This
one has six codes, and the third is the reason there are more than two:

| Code | Meaning |
| --- | --- |
| `0` | The command did what was asked. |
| `1` | Failure — API error, missing resource, refused permission. |
| `2` | The invocation was wrong, or a confirmation flag was missing. |
| `3` | **The work stopped because a person has to act.** Not an error. |
| `4` | A quality gate said no. The command worked; the answer was no. |
| `5` | The wait deadline passed with the work still in progress. |
| `6` | No usable credential. |

CALIBER stops and asks people things: a refinement job produces a candidate and
waits, an Aria plan pauses on a question, a release candidate sits unsigned. None
of those is a failure and none is a success. Collapsing them into either one
produces a specific bad outcome — reported as success, a pipeline proceeds as
though a human approved something; reported as failure, it flags a broken build
for a system working correctly.

```bash
caliberctl job wait RFN-42
case $? in
  0) echo "applied" ;;
  3) echo "candidate ready — someone needs to review it" ;;
  5) echo "still running" ;;
  *) exit 1 ;;
esac
```

## `--json` means only JSON

In `--json` mode stdout carries JSON and nothing else. Progress notes, warnings,
and errors all go to stderr, so this works with no filtering step:

```bash
TOKEN=$(caliberctl token create ci --json | jq -r .token)
```

## Nothing prompts

A command that would do something irreversible requires `--yes` and fails
without it, rather than asking. A tool that blocks on a TTY hangs a CI job
instead of failing it, which is far harder to diagnose.

```bash
caliberctl token revoke PAT-9            # exits 2, explains it needs --yes
caliberctl token revoke PAT-9 --yes      # revokes
```

## Commands

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
| `release list \| sign` | Release candidates and go / no-go. |
| `cookbook list \| install` | Example workflows, with readiness checked first. |
| `prompt list \| show` | Governed prompts and their live versions. |
| `service show \| publish \| unpublish` | Publish a workflow as an HTTP service, or withdraw it. |
| `plugin list` | Optimizers, and plugins installed but not enabled. |

### Notes on specific commands

**`whoami` exits 6 on an anonymous identity.** `GET /me` reports rather than
requires, so an unusable credential returns a 200 with `user_id: anonymous`.
That is right for the API and wrong for a script that ran `whoami` to confirm its
credential — exit 0 for "nobody" would let it proceed on a false premise.

**`workflow run` waits by default.** The non-waiting form is only useful to
something that will poll later, and a deploy script that forgot to wait reports
success for work that has not happened. Pass `--no-wait` when you mean it.

**`--idempotency-key` is passed through, never generated.** Submission is the one
mutating call the SDK will not retry on its own. A key this tool invented would
be different on your retry, which is the opposite of what the key is for.

**`cookbook install` refuses an unready recipe.** Readiness is checked before
installing so the failure names every unmet check at once, rather than one 400
per attempt. `--force` exists because readiness is computed from the live
environment and you may know something it does not.

**`release sign --rationale` is required.** By the API, and therefore here. A CLI
that defaulted it to "signed via caliberctl" would manufacture exactly the record
the requirement exists to prevent.

**`workflow rollback` requires `--yes`.** It changes what a live deployment
alias serves right now, by popping that alias's checkpoint stack — the same
irreversible-action guard as `token revoke` and `service unpublish`.

**`workflow promote` may not promote immediately.** On a gated alias the
server creates a pending promotion instead of rotating the alias — check the
response, or `workflow promotions`, rather than assuming the alias moved.
Act on a pending one with `promotion approve` / `promotion reject`.

**`gate-verdict record --state fail` exits 4 (gate failed); `show` never does.**
`record` produces a decision the same way `release sign` does, so its answer
travels in the exit code. `show` is a plain read of whatever was last
recorded — it exits 0 regardless of `state`, and leaves interpreting the
value to the caller. Verdicts are advisory in v1: CALIBER never blocks alias
rotation on one by itself (see `ARCHITECTURE.md` §4's "Gate semantics"
column); this is release evidence, not an enforced gate.

## Configuration

| Variable | Meaning |
| --- | --- |
| `CALIBER_BASE_URL` | Deployment URL. |
| `CALIBER_TOKEN` | Personal access token. |
| `CALIBER_PROJECT` | Active project, sent as `X-CALIBER-Project`. |

Prefer the environment variables over `--token`: an argument is visible in the
process list.

## Development

```bash
pip install -e ../caliber-sdk -e ".[dev]"
pytest
mypy
ruff check .
python -m build
```
