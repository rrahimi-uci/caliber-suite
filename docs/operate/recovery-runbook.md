---
audience:
  - operator
doc_type: runbook
product_area: operations
stability: beta
prerequisites:
  - A CALIBER deployment running the reference profile
  - An operator-scoped token, and an admin-scoped one for the legacy effect resolve
  - Read access to the CALIBER database for the read-only queries
reviewed_on: 2026-10-28
version_applicability: current main branch docs contract
tags:
  - recovery
  - reference-profile
  - failover
  - effects
  - runbook
---

# Reference profile recovery runbook

Use this page when something in a reference-profile deployment stopped halfway: the process that was running the
background loops died, an external effect has no recorded outcome, a callback or webhook arrived twice, events are not
being delivered, or a run ended `failed` with an effect in doubt. Each section says what the failure looks like, how to
tell, what is safe to do, and what is never safe.

It collects what [Reference profile](reference-profile.md) and [ADR 0002](../architecture/adr/0002-execution-ownership.md)
explain in pieces, and it adds no behavior: every state, code and endpoint below is the one the code uses, and the
scenarios V07 (a dispatcher killed at any moment), V08 (a timeout) and V09 (outbox and inbox) in
`caliber/tests/egi_scenarios/` exercise the effect and event paths end to end. The older [Operations runbook](../runbook.md)
covers recoveries that do not depend on the lease (releases, queues, rollbacks) and is still the page for those.

All HTTP paths are relative to `/ajax-api/2.0/mlflow/caliber`; this page writes them as `/caliber/...`.

## What is wired today

Read this first: it decides which sections you will meet in practice.

| Piece | On main | What it means for you |
| --- | --- | --- |
| Execution-owner lease, failover, `execution_role` in readiness | merged (B13.2, B13.3) | Failover below applies as written |
| Outbox, inbox, `process_once` | merged (B13.4, B13.5a) | Backlog and redelivery sections apply as written |
| Receipt callback | merged (B05.4b, B13.5b) | The one consumer of the inbound inbox |
| Effect proposals, receipts, reconciler, operator view | merged as modules and a read-only view (B05.1 to B05.6) | Rows appear in the view only once something writes proposals |
| The dispatcher that writes and dispatches proposals, and the owner loop that schedules the reconciler | **not merged** (dispatcher B04.1, #724; the owner loop, same-token stuck dispatches and operator resolution are tracked in #997) | No production path creates proposal rows today and no loop runs `reconcile_pass`; a real run's effects go through the **legacy effect ledger** |
| An audited operator action for an `unknown` proposal | **not merged** (#997) | The view offers none (`safe_actions` is empty) |
| Parking a gated run and resuming it after reconciliation | **not merged** | A gated run ends `failed` and stays failed |

So today the effect you will actually meet is a **legacy ledger claim** (`caliber_effect_ledger`, status `in_progress`);
the proposal states below are what the same procedures become when the dispatcher lands, and they are written down now so
that the first `unknown` proposal is not the first time anyone reads them.

## Rules that never bend

1. **Never send a dispatched effect again by hand**, and never re-run something to "make it go through". A dispatched
   effect may already have happened. The only way back from `unknown` is a question to the downstream by the original key
   (there is no transition from `unknown` to `dispatched`).
2. **Never mark an effect failed, skipped or reconciled without the downstream's own evidence.** The platform records
   `failed` for an `unknown` effect only by moving it through `reconciled` first, and only when the connector proves
   absence final (`absence_proven`); a definitive rejection is recorded `failed` only from `dispatched`, on the first and
   only dispatch. A missing record is delayed visibility until proven otherwise. For a legacy claim, `skip`
   and `retry` are statements about the outside world: make them only after you looked at the remote system.
3. **Never edit or delete ledger, proposal, receipt, manifest, outbox or inbox rows.** They are the evidence. On
   PostgreSQL the immutable tables refuse it; on SQLite (development only) they do not, which is a reason to stay off it,
   not a way around.
4. **Never edit a shipped migration**, and never downgrade the schema to fix a stuck row: a downgrade drops tables
   together with their rows (0116 drops `caliber_execution_owner`, 0119 the outbox and its inbox, 0122 the inbound inbox,
   0123 the proposals, 0125 the receipts; 0126 and 0127 drop the dispatch token and the approval binding columns), and
   the evidence goes with them. Schema changes are a new revision.
5. **Never start a second owner by hand.** Do not write the `caliber_execution_owner` row and do not run loops outside the
   lease. A secondary acquires the lease by itself when the old one lapses.
6. **Capture before you act.** Save the output of the first-look commands below with the time; a restart destroys some of
   it.

## First look

```bash
# Is this replica ready, which profile is it under, and is it the owner or a secondary? (the body is wrapped in "data")
curl -s -H "Authorization: Bearer $CALIBER_TOKEN" "$CALIBER_BASE_URL/ajax-api/2.0/mlflow/caliber/readiness" | jq '.data | {ready, profile, execution_role}'

# Is the workflow-run queue draining, and are workers alive?
curl -s -H "Authorization: Bearer $CALIBER_TOKEN" "$CALIBER_BASE_URL/ajax-api/2.0/mlflow/caliber/system/queue"

# Effects that need a person: legacy ledger claims and effect proposals, labelled apart
curl -s -H "Authorization: Bearer $CALIBER_TOKEN" "$CALIBER_BASE_URL/ajax-api/2.0/mlflow/caliber/system/effects"

# Outbound webhooks that were never delivered
curl -s -H "Authorization: Bearer $CALIBER_TOKEN" "$CALIBER_BASE_URL/ajax-api/2.0/mlflow/caliber/system/webhook-dead-letters"
```

The readiness body (under `data`; `curl` does not unwrap it, and neither does it unwrap the 2xx answers of the callback)
carries `profile` and `execution_role`. Ask **every** replica: exactly one should answer `owner` and the rest
`secondary`. The lease owner's id is `host:pid:random`, one per process incarnation, so it names the machine that holds
it. The read-only queries used below (PostgreSQL; `expires_at` is naive UTC database time):

```sql
-- the execution-owner lease: who holds it, which fencing token, when it lapses, and whether it already has
SELECT lease_name, owner_id, fencing_token, acquired_at, renewed_at, expires_at,
       expires_at <= timezone('utc', clock_timestamp()) AS lapsed
FROM caliber_execution_owner;

-- outbox depth: pending, delivered and dead rows
SELECT status, count(*) AS n, min(created_at) AS oldest FROM caliber_event_outbox GROUP BY status;

-- outbox rows that ran out of attempts, with the consumer and exception class that failed (never a payload)
SELECT event_id, event_type, attempts, last_error, created_at
FROM caliber_event_outbox WHERE status = 'dead' ORDER BY created_at;

-- inbound inbox reservations that never reached processed
SELECT consumer, source, event_id, status, received_at FROM caliber_inbox_events WHERE status = 'received';
```

## Which section

| What you see | Go to |
| --- | --- |
| No `owner` anywhere, two `owner`s, runs not draining after a crash or deploy | [Owner crash and failover](#owner-crash-and-failover) |
| A legacy claim `in_progress`, or a proposal `dispatched`, `unknown` or `reconciled` | [A stuck or unknown effect](#a-stuck-or-unknown-effect) |
| A run `failed` with `UnknownEffectError` or an effect that "was claimed but never recorded an outcome" | [A gated or failed run](#a-gated-or-failed-run) |
| A callback or webhook delivered twice, `inbox_payload_mismatch`, an inbox row stuck at `received` | [Callback and webhook redelivery](#callback-and-webhook-redelivery) |
| Events not arriving, dead outbox rows, dead letters | [Outbox backlog and dead letters](#outbox-backlog-and-dead-letters) |
| An admission refusal (`bundle_unavailable`, `rule_bundle_refused`), or a run `failed` with `pins_changed` or `context_denied` | [A run refused for its pins or its rule bundle](#a-run-refused-for-its-pins-or-its-rule-bundle) |
| A restore | [After a restore](#after-a-restore) |

## Owner crash and failover

**What it looks like.** The process holding the lease was killed, lost its database, or hung. Loops stop; runs that were
`running` sit until their own lease lapses; `/readiness` on the survivors still says `secondary` for up to one lease.

**How the takeover works.** The lease (`caliber_execution_owner`) expires by the **database's clock**, so a paused or
skewed process cannot keep it. A secondary tries to acquire every `CALIBER_EXECUTION_LEASE_SECONDS / 3` (default lease
30 s, minimum 3 s). When it wins, the fencing token grows by exactly one, it starts the loops and `/readiness` says
`owner`. The takeover scenario (V14) asserts that it happens within twice the lease, SIGKILL included. An owner that
cannot renew, or cannot reach the database, stops its own loops after two thirds of the lease and reports `secondary`:
losing the lease fails closed.

**Steps.**

1. Read the lease row (query above) and `/readiness` on every replica. Note `fencing_token` and `expires_at`. The process
   logs (`caliber.execution_role`) say `execution-owner lease acquired (owner=... token=N)` on a takeover and
   `execution-owner lease lost (...); stopping the owner-only loops` on a loss; grep them to see a failover.
2. If the holder is dead and `expires_at` is in the past or within one lease: **wait** one lease plus a tick and read again.
   `fencing_token` should have grown by one and exactly one replica should say `owner`. Do nothing else.
3. After the takeover, work left behind is picked up by the loops themselves, each by its own rule. The per-loop table is
   [Recovery by stage](../architecture/adr/0002-execution-ownership.md#recovery-by-stage) in ADR 0002; the ones you will
   notice:

   | Left behind | What happens | When |
   | --- | --- | --- |
   | A workflow run `running` | requeued (`error_code` `lease_recovered`, event `workflow.run.recovered`) and run again **from the beginning**, unless its summary already carries a resume checkpoint (then from that same checkpoint); a run parked at an approval or wait is not `running` and is never recovered this way | when its run lease lapses (`CALIBER_WORKFLOW_RUN_LEASE_SECONDS`, 60 s) |
   | Webhooks accepted but not delivered | become dead letters of kind `shutdown`; replay them (below) | when their accept lease lapses (at least 60 s) |
   | Outbox rows claimed by the dead publisher | delivered again by the new publisher; the consumer's inbox drops a repeat | at lease expiry (30 s) |
   | A refinement job `running` | marked `failed` by the janitor, **not** requeued | heartbeat older than `CALIBER_JANITOR_STALE_THRESHOLD_SECONDS` (300 s) |
   | A calibration job `running` | nothing, on purpose: abandon it or submit a new linked job | see [P4](../runbook.md#p4-a-refinement-or-calibration-job-vanished) |
   | A workspace import `running` | moved to `reconcile_required` (not retryable; queue a new import job for the same content, which is safe by digest) | after `CALIBER_WORKSPACE_IMPORT_LEASE_SECONDS` (300 s) |
   | An effect `dispatched` under the old lease | becomes **abandoned**; see the next section | once the new owner holds a higher token |

4. **A recovered run is not a safe run.** It starts again from the beginning. Only webhook and API-request nodes go through
   the effect ledger: an effect of those nodes that it already claimed is not repeated (the ledger replays it, or reports
   the claim `indeterminate`). Effects of other nodes (MCP tools, OpenAPI tools, knowledge builds, output buckets) are not
   ledgered and can happen again. Before you let a recovered run proceed, or retry it, read its `workflow.run.recovered`
   event and check the downstream for any non-ledgered effect it could already have made.

**No owner after two leases.** Every process is a secondary when the lease store is unreachable (the lease calls fail closed
and `/readiness` reports the database), or when the loops fail to start after an acquisition (the error is logged by
`caliber.execution_role`, the lease is released and the next tick tries again). Fix the cause (the database, the failing
loop); do not try to force an owner.

**Two replicas say `owner`.** This must not persist. The loops are not yet fenced per transition, so after a lease loss the
old owner may finish the transition it was in, bounded by `CALIBER_SHUTDOWN_GRACE_SECONDS` (30 s): read `/readiness`
again after that long. If two replicas still both say `owner`, stop one and treat it as an incident: capture both
readiness bodies and the lease row first. SQLite gives no fencing guarantee at all and the reference profile refuses it.

**Clean restart.** A deliberate shutdown stops the loops while it keeps renewing the lease, then releases it, so the
successor starts without waiting for expiry. A rolling deploy therefore hands over cleanly; a SIGKILL costs one lease.

**Never.** Do not shorten the wait by writing the lease row, by restarting the survivors "to make one win", or by
re-running the work the dead owner was doing.

## A stuck or unknown effect

An effect is stuck when nobody has the final word on whether it reached the downstream. Two records exist and the view
never mixes them (`source` and `safe_actions` on every row):

```bash
curl -s -H "Authorization: Bearer $CALIBER_TOKEN" \
  "$CALIBER_BASE_URL/ajax-api/2.0/mlflow/caliber/system/effects?source=proposal&proposal_state=open&limit=100"
```

```python
from caliber_sdk import CaliberClient

client = CaliberClient()  # CALIBER_BASE_URL and CALIBER_TOKEN come from the environment
view = client.system.effects(source="proposal", proposal_state="unknown")
for row in view["proposals"]:
    print(row["proposal_id"], row["state"], row["reason"], row["abandoned"], row["blocks_dependents"])
print(view["lease"])  # the stored execution-owner lease (also returned when source is "all"), or None
```

### A proposal (`source: effect_proposal`)

States: `proposed`, `approved`, `dispatched`, `committed`, `failed`, `unknown`, `reconciled`. The open ones, which block
what depends on them, are `dispatched`, `unknown` and `reconciled`.

| You see | It means | Do |
| --- | --- | --- |
| `dispatched`, `abandoned: true` | the lease that dispatched it was replaced; the attempt may have been sent | Nothing by hand. A reconciler pass under the new lease records `unknown` (`abandoned_dispatch`) and asks the downstream by key; until a loop schedules that pass (not merged), it stays `dispatched` and keeps blocking its dependents |
| `dispatched`, `abandoned: false` | the dispatcher's own attempt in flight, **or** its lease lapsed and nobody has taken over yet | Read `lease` in the same response: `expired: true` means failover has not completed, go to [Owner crash](#owner-crash-and-failover). Otherwise wait, but see the next paragraph if it does not clear |
| `dispatched`, `abandoned: null` | no recorded token or no lease to compare | Treat as unknown; never abandon it by hand |
| `unknown` | no recorded outcome (a timeout after the send, an ambiguous answer, an abandoned dispatch) | Wait for the reconciler or a verified callback. It is never sent again |
| `reconciled` | the reconciler began settling it and has not finished | Wait; the next pass or the callback finishes it |
| `committed` | a verified receipt exists | Do not repeat it. (The platform makes at most one local claim per effect and verifies a receipt; it does not promise exactly-once delivery by the downstream) |
| `failed` | a definitive rejection, not sent, or proven absence | Final. A new run is a new effect |

**How an unknown effect gets settled.** Only the owner's reconciler asks, by the original logical key, through the
connector's own lookup, at the `base_url` of the `CALIBER_EFFECT_CALLBACK_CONNECTORS` binding for that connector and
project. What comes back:

| The downstream says | Result |
| --- | --- |
| a stored request that is this proposal's effect | verified receipt, `committed` |
| 404 from a connector that proves absence final | `failed` (`absence_proven`) |
| 404 from any other connector (document-request included) | still `unknown`: **delayed visibility** or a request still in flight |
| 5xx, 401 or any other status | still `unknown` (detail `http_<code>`) |
| a 2xx whose body is not this proposal's effect (unreadable, or another request under the key) | still `unknown`, reported `unverified` |
| cannot be reached, or no binding for the connector and project | still `unknown` (`lookup_failed`, `not_bound`) |

**A `dispatched` row that does not clear.** A row that is old, with `abandoned: false` and `lease.expired: false`, is
dispatched under the lease the owner still holds. The reconciler never abandons such a row (it is the dispatcher's own
attempt), so if the dispatcher's settle failed while it kept its lease, waiting does not clear it until the lease turns
over (#997). Do **not** resend. Capture the row (`proposal_id`, `logical_key`, `owner_id`, `dispatch_token`, `dispatched_at`)
and the lease, ask the downstream by key, and escalate.

**When to wait, and for how long.** An `unknown` that survives a pass is not an error: a request that is still in flight, or a
read that does not yet see its own write, looks exactly like a request that never arrived. Wait while the downstream's own
people can still say it may land. The V07 scenario shows the case: a dispatcher killed before the send leaves the effect
`unknown`, the reconciler's lookup finds nothing, and only when the request lands late from elsewhere does the next pass
commit it, with the downstream counting one mutation throughout.

**What you can do that is safe.**

- Ask the downstream's owner whether the request exists, by the proposal's `logical_key` (the idempotency key, in the view).
  That is evidence; keep it with the incident.
- Ask the connector to **redeliver its receipt callback** (see [Callback and webhook redelivery](#callback-and-webhook-redelivery)).
  The platform asks the downstream itself before it records anything, so a callback can only confirm what the downstream
  confirms. This is today's way to settle an `unknown` proposal, since no loop schedules the reconciler.
- Nothing else. No operator procedure runs the reconciler today (#997): `reconcile_pass` is called only by code inside the
  owner process that holds the lease (see
  [Reconciler](reference-profile.md#reconciler-only-the-owner-asks-about-unknown-and-abandoned-effects-b055b-742)). Do not
  acquire the lease yourself to call it.

**What is never safe.** Re-sending the request; creating the same effect from a new run; editing the proposal row;
deleting it; moving it with SQL. There is no operator action that fails an `unknown` proposal without the downstream's
evidence, by design (an audited one is #997).

### A legacy ledger claim (`source: legacy_ledger`)

The queued-run worker claims `caliber_effect_ledger` before an effectful node performs its effect. A claim that stays
`in_progress` after a crash means the process died between the claim and the recorded outcome, or the request timed out
after it may have left: whether it reached the remote system is unknown.

1. List: `GET /caliber/system/effects` (defaults to `status=in_progress`). Note the `effect_key`, `workflow_run_id` and
   `node_id`.
2. Decide **at the remote system** whether the effect happened. Do not infer it from CALIBER.
3. Record the decision (admin scope, audited): `POST /caliber/system/effects/{effect_key}/resolve`.

   ```bash
   curl -s -X POST -H "Authorization: Bearer $CALIBER_TOKEN" -H "Content-Type: application/json" \
     -d '{"resolution": "skip", "reason": "request id 8841 exists at the downstream"}' \
     "$CALIBER_BASE_URL/ajax-api/2.0/mlflow/caliber/system/effects/$EFFECT_KEY/resolve"
   ```

   `skip` (the effect **did** happen) marks the row `completed`: a later attempt of the same run replays it and does not
   repeat it. `retry` (it did **not**) marks the row `failed`, which a later attempt of the same run may claim again. A row
   that is not `in_progress` is refused (`400`), and so is an unknown key and any other resolution.
4. The decision is on the row and in the audit log. It does **not** restart a run: see the next section.

Apply a proposal's key to the legacy resolve endpoint and it is refused (`400`) and changes nothing.

## A gated or failed run

**What it looks like.** A run ended `failed`. For a proposal the error starts
`UnknownEffectError: effect <proposal> (node <n>, unknown) has no final outcome: reconcile it ...` (error code
`runtime_error`); for a legacy claim the message says the effect `was claimed ... but never recorded an outcome` and names
the resolve endpoint.

**Why.** Before every node, at the top of every retry attempt, the runtime checks the effects the node and everything it
depends on asked for. While one is `dispatched`, `unknown` or `reconciled`, the node does not run and nothing absorbs the
refusal: no fan-out item, loop iteration or error boundary turns it into a handled error. This is the guarantee, not a
fault: no write that depends on an effect nobody can vouch for is made.

**A failed run is not resumed.** `failed` is terminal: no transition leaves it, reconciling the effect does not restart the
run, and `POST /caliber/workflow-runs/{run_id}/retry` creates a **new** run with a new id. Effect identity includes the run
id, so the new run is a **new logical effect**: it is not gated by the old run's open effect and may ask the downstream for
the same thing again.

**Steps.**

1. Find the effect (view above, `workflow_run_id` filter) and its state.
2. Settle it first: proposal by the reconciler or a verified callback, legacy claim by the resolve decision with evidence.
3. Only then decide about a retry, with the owner of the downstream:
   - effect `committed` (or legacy `skip`): the work is done. Do **not** retry the run; if later nodes must still run, that
     is a design question for the workflow owner, not a button.
   - effect `failed` with proof (or legacy `retry`): a retry is safe with respect to this effect.
   - effect still `unknown`: do not retry. A retry would ask for the effect again.
4. A run that lease recovery requeued runs again from the beginning. If an effect it claimed is still `in_progress` (or a
   proposal of it is open), it ends `failed` at that node; it needs no manual retry, and a manual retry does not help it
   (that is a new run, see above). Read its `workflow.run.recovered` event (`GET /caliber/workflow-runs/{run_id}/events`)
   and apply step 3 before anything else.

**Never.** Retry the run "to see whether it works now" while its effect is open.

## Callback and webhook redelivery

**The receipt callback** (`POST /caliber/effects/callbacks/receipt`, signed, no session) is the one consumer of the inbound
inbox (`caliber_inbox_events`, key `(consumer, source, event_id)`; consumer `effect-callback`, the source a digest of the
connector and project, the event id the `X-Caliber-Delivery` header). What each answer means for the sender:

| Answer | Meaning | The sender should |
| --- | --- | --- |
| `200` `status: duplicate` | this delivery id was already consumed with this key; nothing applied | stop: it succeeded earlier |
| `200` `status: already_committed` | the proposal is committed (a new delivery id, or it lost a race to the commit) | stop |
| `200` `status: committed` with `receipt_id` | recorded now | stop |
| `202` `status: unknown` | the downstream holds nothing (yet) under the key, or something that is not this effect; **nothing consumed** | try again later with the same or a new delivery id |
| `202` `status: in_flight` | still `dispatched`: the dispatcher, or the reconciler once abandoned, owns it | try again later |
| `409` `inbox_payload_mismatch` | the same delivery id with a **different** key | send the new message under a new delivery id; never reuse an id |
| `409` with a reason code (`receipt_conflict`, `stale_version`, ...) | the commit was refused and **rolled back whole**, the reservation too | retry the same delivery |
| `409` `proposal_failed`, `not_dispatched` | the proposal is `failed`, or was never dispatched | do not retry; it is not waiting for a receipt |
| `502` `lookup_failed`, `503` `busy` | the downstream could not be asked (or four lookups for this connector and project are in flight) | retry |
| `404` `no such proposal` | not this connector's, not this project's, or no such key | check the key |
| `401` `callback rejected` | unknown connector or project, unreadable secret, bad signature or a timestamp more than 300 s off: one answer for all | check the binding, the secret and the clock |

A delivery answered `5xx`, or never answered because the process died before the commit, left **no** reservation: the
sender's retry is processed. A failed transaction is never treated as already processed.

**An inbox row stuck at `received`.** A reservation and its `processed` marker commit in one transaction, so a committed
`received` row can only come from an `apply` that committed or rolled back the session it was handed
(`InboxContractError`). Every later delivery of that id is then answered `duplicate`. Steps:

1. List the rows (query above) and note the `consumer`, `source`, `event_id`.
2. For `effect-callback`, the proposal's own state is the truth: read it in the view by `logical_key`. If it is `committed`,
   nothing is lost; if it is `unknown`, ask the connector to send the callback again under a **new** delivery id (a new
   id is a new inbox key, so it is processed and the platform asks the downstream again).
3. Do not edit or delete the row. Report it with the row and the proposal state: it indicates a defect in the consumer.

**The GitHub webhook** is not a consumer of this table. It has its own signed delivery inbox
(`caliber_workspace_source_events`): a repeat delivery is `202` with `duplicate: true` and no second row, and a reused
delivery id with different evidence gets the same generic `401` as any other rejection (deliberately not a `409`). Nothing
to recover; the sender's retry is safe.

## Outbox backlog and dead letters

**Business events (the outbox).** Run events are written to `caliber_event_outbox` in the transaction of the transition that
caused them, then delivered by `OutboxPublisher`, an owner-only loop. Delivery is at least once; consumers deduplicate on
`(consumer, event_id)`. Read the depth with the query above, or from Python:

```python
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from caliber.events.outbox import outbox_depth

factory = sessionmaker(create_engine(os.environ["CALIBER_DATABASE_URL"]))
with factory() as session:
    # {"pending": ..., "delivered": ..., "dead": ..., "oldest_pending": ...}: the last is the creation time of the
    # oldest row that is still pending, the lag of the slowest undelivered event
    print(outbox_depth(session))
```

| You see | Cause | Do |
| --- | --- | --- |
| `pending` grows, `oldest_pending` is old, no `owner` | the publisher runs only on the lease holder | [Owner crash and failover](#owner-crash-and-failover) |
| `pending` grows with an owner | a consumer keeps failing; `last_error` names the consumer and the exception class, never a message or payload | fix the consumer; rows retry with exponential backoff (cap 300 s) |
| `dead` rows | a row ran out of `CALIBER_EVENT_OUTBOX_MAX_ATTEMPTS` (default 20), or every earlier handoff attempt never finished (`abandoned: ...`) | keep them: they are the record. There is **no** replay tool yet; report the rows with `event_id` and `last_error` |

Delivered rows are pruned after `CALIBER_EVENT_OUTBOX_RETENTION_DAYS` (7); dead rows are never pruned. The default registers
no consumer, so a healthy default deployment shows `pending` near zero and no growth in `dead`.

**Outbound webhooks (dead letters).** An event the dispatcher could not deliver is recorded in
`caliber_webhook_dead_letters`, with a `kind`: `exhausted` (the receiver is broken), `overflow` (CALIBER shed load),
`shutdown` (accepted and still queued when the owner stopped or died) or `accept_failed` (the database could not record
the acceptance, so the event was deliberately not sent).

1. `GET /caliber/system/webhook-dead-letters` (defaults to `status=open`).
2. Fix the receiver first. Delivery is at least once by design, so a receiver must tolerate a repeat.
3. `POST /caliber/system/webhook-dead-letters/{dead_letter_id}/replay` (operator). A replay that fails leaves the row
   `open` with the reason; two concurrent replays are arbitrated by the database (the loser gets `409`). `503` means no
   dispatcher is configured or delivery is disabled.
4. For an event that must not be sent: `POST .../acknowledge` with a note. It marks the row handled; it deletes nothing.

Nothing redelivers a dead letter by itself, and a replay is a new send: do it only when the receiver is healthy.

## A run refused for its pins or its rule bundle

A run admitted in the reference profile executes exactly what its manifest pinned, and fails closed when that is no longer
true. Two different things happen, and they are told apart by where you see them. These are explained, with every code
and its recovery, in [Reference profile](reference-profile.md); the short version for the operator.

**Refused at admission** (no run is created; the HTTP `409` body carries only a message, not a code, so match the message
text):

| The message | Recovery |
| --- | --- |
| "this workflow version has no sealed deployment bundle", "failed its integrity check", or "unresolved dependencies" (internally `bundle_unavailable`) | compile and publish the version again |
| starts `rule_bundle_refused:` (internally a manifest refusal) | restore the bundle file or update the pinned digest deliberately (path and digest together) |

**A run that ended `failed`** (the run has an error code and a summary):

| The run's error code | Recovery |
| --- | --- |
| `pins_changed`, `pins_mismatch`, `model_route_changed` | the run stays failed; restore what changed or **submit a new run** |
| `context_denied`, the summary naming `rule_bundle_changed` or `rule_bundle_unavailable` | the run stays failed even if you restore the file; resubmit |
| `authority_denied` (`grant_expired`, `grant_revoked`, `not_a_member`) | submit a new run as someone who may |

The same rule as for effects applies: a refused run is not resumed, and a new run is a new run. For a run that had an
effect in doubt, settle the effect first ([A gated or failed run](#a-gated-or-failed-run)).

## After a restore

Backup and restore are in [Backup and recovery](backup-and-recovery.md): back up the CALIBER database and the MLflow
backend store together, restore the secret encryption key separately, run `caliber-migrate` against the restored database,
then verify `/readiness` reports `ready: true` including the `schema` check, and run **Reconcile releases** on the
Releases page. The effect, receipt, manifest, outbox and inbox tables live in the CALIBER database and are restored with
it, back to the snapshot time. What that means for effects, which that page does not cover:

1. Stop every replica before you restore.
2. Start the restored deployment with `CALIBER_BACKGROUND_TASKS_ENABLED=false`: no loop runs and every replica is a
   `secondary` (there is no lease to take), so nothing executes yet.
3. Compare the downstream with the runs and effects created after the snapshot time. An effect made after that point has no
   ledger or proposal row in the restored database, and a restored `queued` or `running` run will perform it again. The
   restored lease row and every `dispatch_token` are from snapshot time too, so a restored token can be lower than tokens
   issued after it.
4. Only when that comparison is done, enable background tasks again.

Do not hand-edit rows to reconcile them with the downstream; use the sections above.

## Related

- [Reference profile](reference-profile.md): the lease, the outbox and inbox, effect proposals, the reconciler, the
  operator view and the receipt callback, with every code.
- [ADR 0002: execution ownership](../architecture/adr/0002-execution-ownership.md): who may run each loop and what each
  loop leaves behind when its owner dies.
- [Operations runbook](../runbook.md): releases, queues, rollbacks and SLO incidents.
- [Backup and recovery](backup-and-recovery.md) and [Health and readiness](health-and-readiness.md).
