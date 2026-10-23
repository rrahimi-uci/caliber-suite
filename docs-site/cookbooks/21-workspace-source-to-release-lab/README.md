# Cookbook 21 · Workspace Source-to-Release

> **Flags:** `CALIBER_FEATURE_RELEASE_CANDIDATES=true` for the release stages (workspace releases
> share the flag with cookbook 11's record-only candidate signoff; without it, `/projects/{id}/releases*`
> answers 404 `feature_disabled`). Revisions, snapshots, diffs, change requests, environments and
> version tags are **ungated**. `CALIBER_FEATURE_COOKBOOKS=true` only for the gallery step.
>
> **This surface has no UI in the alpha.** The journey is API/SDK-first, and the lab says so
> rather than inventing screens that do not exist.

Pin a workspace's entities into an immutable revision, diff two revisions, carry one through a
reviewed change request, and take it to the release gate — no Git, no object store — knowing
exactly where the alpha's boundary is and what every refusal beyond it says.

"What exactly is running?" needs an answer that survives edits. A **managed snapshot** pins named
versions of named entities (a prompt at v2, a test set at v1) into a revision with a content hash.
A **change request** carries that revision through a second person's review. A **release** binds
it to an environment and demands evaluation evidence — and in the alpha, that is where the line
is: release evaluations have no executor, so the evaluation stays `queued` and everything beyond
it refuses with a documented reason. This lab proves the whole chain, including the boundary.

## What this lab demonstrates

- **A caliber-managed workspace.** A fresh project's source mode is `caliber_managed` with no
  provider connection; the Git import path (`revision-imports`) refuses with
  `workspace_source_not_configured`. Four environments are seeded; only `dev` starts active.
- **Managed snapshots.** `POST /projects/{id}/revisions:snapshot` resolves each
  `(resource_type, resource_id, version_ref)` pin through a registered adapter and stores an
  immutable, content-hashed revision (`source_kind: managed`, `status: ready`, null Git fields).
  Identical pins return the identical revision — idempotent by content. An unsupported type
  (`knowledge_base`) refuses with `resource_type_adapter_unavailable`. A viewer cannot snapshot.
- **A deterministic diff.** R2-vs-R1 names exactly the pin that changed (the prompt, v1→v2),
  nothing added, nothing removed; a diff without a base is a 400.
- **A reviewed change request.** Creation reserves the semver claim; submission requires an
  assigned reviewer; the snapshot's author cannot review their own revision
  (`self_review_forbidden`); the second person's approval makes it `technically_approved`.
- **The release gate, and the alpha's honest boundary.** A draft release into `dev` (digests are
  content addresses; the same idempotency key returns the same release) moves to `evaluating` on
  `evaluate` — and its evaluation stays **`queued`**, because workspace release evaluations have
  no executor in the alpha (`docs/start/alpha-scope.md`, known limitations). Consequently:
  change-request **accept** → 409 `qa_go_decision_required`; **quality signoff** → 409 *machine
  gate evidence is missing*; **apply** → 409 *only an approved release can be applied*; and
  **version tags stay empty**, because only an accept mints one.
- **The audit trail**: `snapshot_workspace_revision`, `create_workspace_change_request`,
  `submit_workspace_change_request`.

**You are done when:**

- the identical snapshot returned the identical revision id, and the changed pin made a new one;
- the diff listed exactly one changed pin;
- self-review was refused and the second person's approval made the CR `technically_approved`;
- the release reached `evaluating` with its evaluation `queued` — and still `queued` after a wait;
- accept, signoff and apply each refused with the documented status and reason.

## Prerequisites and setup

```bash
export CALIBER_OPERATOR_USERS=admin,lab21-reviewer
scripts/cookbook-env.sh up lab21 --flags release_candidates,cookbooks
```

No stand-in server, no object store, no `--s3`: managed snapshots store no files. The reviewer
needs an editor project role **and** the platform operator scope (both are review-eligibility
requirements), which the script assigns and `CALIBER_OPERATOR_USERS` grants.

Run it:

```bash
source .tmp/cookbook-env/lab21/env.sh      # CALIBER_URL, CALIBER_API, CALIBER_STATE_DIR, ...
# mint an admin token (Settings > API tokens does the same in the browser):
curl -s -c /tmp/lab21.jar -X POST $CALIBER_API/auth/login -H 'content-type: application/json' \
  -d '{"user_id":"admin","password":"admin"}' >/dev/null
export CALIBER_TOKEN=$(curl -s -b /tmp/lab21.jar -X POST $CALIBER_API/auth/tokens \
  -H 'content-type: application/json' -d '{"name":"lab21"}' | jq -r .data.token)
export CALIBER_BASE_URL="$CALIBER_URL"
(cd sdk/caliber-sdk && PYTHONPATH=. ../../caliber/.venv/bin/python \
   examples/cookbooks/cookbook_21_workspace_source_to_release.py)
```

## Dataset and sample files

None, by design — the lab's subject *is* the machinery that pins and reviews entities, so every
artifact (the prompt's two versions, the test set, the revisions, the change request, the release)
is created live through the product. See [`assets/README.md`](assets/README.md) for the one
convention worth knowing: the release request's three digests are content addresses (any 64
lowercase hex characters) that a real pipeline would compute from its environment config, lockfile
and policy.

## Configuration and environment

| Variable | Lab value | Why |
| --- | --- | --- |
| `CALIBER_FEATURE_RELEASE_CANDIDATES` | `true` | gates `/projects/{id}/releases*` (and cookbook 11's candidates); everything else in this lab is ungated |
| `CALIBER_OPERATOR_USERS` | `admin,lab21-reviewer` | snapshots need the operator scope; so does review eligibility |
| `LAB21_SUFFIX` | unset | set it (`_2`) to re-run on a stack that already holds an earlier run |

## Run it through the API

The transcript below is a real captured run (`cookbook_21_workspace_source_to_release.py`)
against a fresh stack:

```text
  created account lab21-reviewer
  created account lab21-viewer
== 1. the flag, and a workspace with seeded environments
  ok  the release_candidates preview flag is on (off by default; releases 404 without it)
  ok  four environments are seeded with the workspace
  ok  only dev starts active; promoting further is an explicit, owner-gated act
== 2. the source of truth is caliber-managed; the Git import path refuses
  ok  a fresh workspace is caliber-managed with no source connection
  ok  and no provider connection exists
== 3. entities, then a snapshot that pins them
  ok  revision WSR-38cecb12 pins prompt v1 and the dataset, no Git involved
  ok  the identical snapshot returns the identical revision -- idempotent by content
  ok  a changed pin makes a new revision
== 4. the diff names exactly what changed
  ok  one changed pin (the prompt), nothing added or removed
  ok  a diff without a base revision is refused -> HTTP 400
  ok  an unsupported resource type is refused -> HTTP 409
  ok  with its own reason code, resource_type_adapter_unavailable
  ok  a viewer cannot snapshot -> HTTP 403
== 5. the change request: authored, reviewed by a second person
  ok  the snapshot's author cannot review their own revision -> HTTP 403
  ok  a second person's approval makes it technically_approved
  ok  accepting still refuses: a QA go decision is required first -> HTTP 409
  ok  and names why
  ok  version tags stay empty: only an accepted change request mints one, and acceptance is gated on the QA decision the alpha cannot produce
== 6. the release gate, and the alpha's honest boundary
  ok  release WSREL-56bee3e8 created as a draft against dev
  ok  replaying the same idempotency key returns the same release
  ok  evaluate moves the release to evaluating
  ok  and the evaluation is queued
  ok  where it stays: workspace release evaluations have no executor in the alpha (docs/start/alpha-scope.md names this limitation; do not wait on it)
  ok  quality signoff refuses -> HTTP 409
  ok  because machine gate evidence is missing
  ok  and apply refuses: only an approved release can be applied -> HTTP 409
== 7. the audit trail
  ok  audit log has snapshot_workspace_revision
  ok  audit log has create_workspace_change_request
  ok  audit log has submit_workspace_change_request
== 8. the gallery starter
  ok  the gallery lists 10 step(s) for this lab
== done
{
  "change_request_id": "WSCR-90028a72",
  "project_id": "PRJ-d43e92f1",
  "release_id": "WSREL-56bee3e8",
  "revisions": [
    "WSR-38cecb12",
    "WSR-85ab9c56"
  ]
}
```

By hand, the spine is six calls:

```bash
API=$CALIBER_URL/ajax-api/2.0/mlflow/caliber
AUTH="Authorization: Bearer $CALIBER_TOKEN"
H='content-type: application/json'

curl -s -H "$AUTH" -X POST "$API/projects/$PID/revisions:snapshot" -H "$H" \
  -d '{"resources":[{"resource_type":"prompt","resource_id":"release-notes","version_ref":"2"},
                    {"resource_type":"eval_dataset","resource_id":"'$DATASET'","version_ref":"1"}]}'
curl -s -H "$AUTH" "$API/projects/$PID/revisions/$R2/diff?base=$R1" | python3 -m json.tool
curl -s -H "$AUTH" -X POST "$API/projects/$PID/change-requests" -H "$H" \
  -d '{"title":"Release notes v2","head_revision_id":"'$R2'","base_revision_id":null,
       "semantic_version":"1.0.0","review_backend":"caliber","reviewer_user_ids":["lab21-reviewer"]}'
# ... :submit as the author, POST /reviews {"decision":"approve"} as the reviewer ...
curl -s -H "$AUTH" -X POST "$API/projects/$PID/releases" -H "$H" \
  -d '{"revision_id":"'$R2'","environment_id":"'$DEV'","request_idempotency_key":"rel-1",
       "environment_config_sha256":"'$(printf 'a%.0s' {1..64})'",
       "runtime_dependencies_sha256":"'$(printf 'b%.0s' {1..64})'",
       "policy_sha256":"'$(printf 'c%.0s' {1..64})'"}'
curl -s -H "$AUTH" -X POST "$API/projects/$PID/releases/$RID/evaluate" -H "$H" \
  -d '{"idempotency_key":"eval-1","evaluation_plan_sha256":"'$(printf 'a%.0s' {1..64})'",
       "input_sha256":"'$(printf 'b%.0s' {1..64})'"}'
curl -s -H "$AUTH" "$API/projects/$PID/releases/$RID/evaluations" | python3 -m json.tool  # queued, and stays queued
```

## Recipe: run it through the API

1. **Check the flag and create the workspace.** `GET /capabilities` must show
   `release_candidates` enabled (off by default: releases would 404 `feature_disabled`). Create
   the project and read `GET /projects/{id}/environments`: four seeded environments, only `dev`
   active.
2. **Confirm the source of truth is caliber-managed.** `GET .../source` →
   `caliber_managed`, null source; `GET .../source/connection` → none. `POST .../revision-imports`
   (the Git path) refuses with 409.
3. **Author the entities.** A prompt with two versions and a test set — ordinary creations
   through their own APIs or pages.
4. **Snapshot revision R1** with two pins (prompt `"1"`, test set `"1"`), then repeat the
   identical request: the identical revision id comes back. Idempotent by content.
5. **Pin the prompt's v2 into revision R2** — a new revision id, `revision_number` + 1.
6. **Diff R2 against R1.** Exactly one changed pin, nothing added or removed. A
   `knowledge_base` pin refuses with `resource_type_adapter_unavailable`; a viewer's snapshot
   is 403.
7. **Open a change request on R2** (`semantic_version 1.0.0`, `review_backend caliber`, the
   reviewer assigned) and `:submit` it. The semver claim is reserved at creation.
8. **Review as a second person.** The author's own review is 403 `self_review_forbidden`; the
   assigned reviewer's `approve` makes it `technically_approved`.
9. **Create a draft release into dev and evaluate it.** Replaying the idempotency key returns
   the same release; `evaluate` moves it to `evaluating` with the evaluation `queued` — where it
   stays. **Do not wait on it** (the SDK's `wait_for_evaluation` would sit its full timeout):
   workspace release evaluations have no executor in the alpha.
10. **Read the refusals that guard the rest.** CR `:accept` → 409 `qa_go_decision_required`;
    `quality-signoff` → 409 *machine gate evidence is missing*; operations `apply` → 409 *only an
    approved release can be applied*; `GET /version-tags` stays empty. The audit log carries the
    journey.

## Verify it worked

| Claim | Where to look | What proves it |
| --- | --- | --- |
| Snapshots are idempotent by content | `revisions:snapshot`, twice | the identical pins return the identical `revision_id`; the changed pin returns `revision_number` + 1 |
| The diff is exact | `revisions/{R2}/diff?base={R1}` | `changed` = the prompt pin alone; `added` and `removed` empty |
| Review is a second person's act | the CR's reviews | author's review 403 `self_review_forbidden`; reviewer's approval → `technically_approved` |
| The boundary holds | the release's evaluations | `queued` on creation and still `queued` after a wait; signoff/apply/accept refuse with their documented reasons |
| Nothing was minted past the gate | `GET /version-tags` | empty — only an accepted change request mints a tag |
| The journey is recorded | **Audit Log** | `snapshot_workspace_revision`, `create_workspace_change_request`, `submit_workspace_change_request` |

## The trust boundary, honestly

- **Workspace release evaluations never execute in the alpha.** The claim/complete/settle
  functions exist in the service layer with no caller; the lab's backend test pins that fact so
  the lab fails loudly the day an executor lands. Everything downstream of evaluation evidence —
  quality signoff, CR accept, version tags, apply — is unreachable on a live server and is
  demonstrated as the refusal it is.
- **This is a different feature from cookbook 11.** Lab 11's *release candidates* are a
  record-only signoff ledger; this lab's *workspace releases* bind a revision to an environment.
  They share one feature flag and nothing else.
- **No UI exists for this surface** (revisions, change requests, workspace releases,
  environments). The Releases page's candidate section belongs to lab 11's feature.
- **`PUT /projects/{id}/source` is one-way.** Flipping a project to `git_managed` has no reverse,
  and managed revisions can then no longer be released beyond dev — this lab deliberately never
  calls it.
- **Snapshot pins resolve what exists now.** `version_ref` must name a real, resolvable version
  (a published workflow version, an existing prompt version); the snapshot refuses rather than
  pinning dangling references.

## Common errors and troubleshooting

| You see | Why | Fix |
| --- | --- | --- |
| `404 feature_disabled` on `/projects/{id}/releases*` | the release_candidates flag is off (the default) | start with `--flags release_candidates` and restart |
| `403` on `revisions:snapshot` | the caller lacks the operator scope or an owner/editor role | snapshot as the admin/operator; viewers read |
| `409 reviewer_assignment_required` on `:submit` | the CR has no assigned reviewer | pass `reviewer_user_ids` at creation (or assign one) before submitting |
| `403 self_review_forbidden` | the reviewer created the head revision | a second person reviews; the author waits |
| the evaluation never leaves `queued` | expected: no executor in the alpha | do not wait on it; the lab asserts this state deliberately |
| `409 qa_go_decision_required` on accept | acceptance needs a QA go decision no alpha server can produce | expected; the refusal is the demonstration |
| `400` on a release create digest | a digest is not 64 lowercase hex characters | hash something real, or use the lab's placeholder form |
| `409 workspace_source_not_configured` on `revision-imports` | the Git import path needs a configured source | expected on a caliber-managed workspace; snapshots are the managed path |

## Cleanup and reset

```bash
(cd sdk/caliber-sdk && PYTHONPATH=. ../../caliber/.venv/bin/python \
   examples/cookbooks/cookbook_21_workspace_source_to_release.py --cleanup)
```

archives the workspace (revisions, the change request and the release go with it; accounts and
audit entries stay). An archived workspace keeps its name, so a re-run on the same stack needs
`LAB21_SUFFIX`. For a clean slate:

```bash
scripts/cookbook-env.sh down lab21 && rm -rf .tmp/cookbook-env/lab21
```
