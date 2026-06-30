
# Version Management UX — Design Spec (v2, critique-corrected)

> **Status:** Proposal · **Scope:** cross-artifact · **Target:** CALIBER v1 (single-environment)
> **Decisions locked:** single-environment (no dev→staging→prod ladder); eval gate is **inline & advisory** (verdict shown at promote time, operator may override with an acknowledged, audited reason).
>
> **v2 note:** This revision folds in a code-grounded adversarial critique (40 findings, 36 confirmed). The biggest corrections: the raw prompt-alias path writes **no audit row/checkpoint/actor** (so the timeline + override audit + exact rollback for prompts all need a new audited endpoint); the gate verdict is **not** a thin read (the candidate-vs-baseline comparison isn't stored against a version); prompts have **no save-without-promote path** today; RBAC is **not uniform** across artifacts. Sections below reflect those fixes. Each backed claim cites `file:line`.

---

## 1. Why

Two problems, established by audit:

1. **Backend capability and UI exposure don't line up.** Workflows/KBs/test-sets have real
   version models but the UI hides or omits the controls (the genuine `rollbackWorkflow`
   primitive has *zero* UI call sites; workflow Deployments/Promotions tabs are hidden by
   `SINGLE_ENVIRONMENT`). Conversely the UI sometimes *looks* more capable than the backend:
   **skills have a real rollback path** (`SkillPromoter.rollback` restores `content_before`,
   `promoter.py:458`) **but a checkpoint is written only at promotion** (`apply.py` builders),
   so the UI prose claiming *any* version is one-click rollback-restorable
   (`SkillDetail.tsx:856-858`) is **false for edit-bumped versions** — plain content edits
   write no checkpoint (`skills.py:464-481` only audit-logs the diff) and are unrecoverable.
2. **There is no place to see or undo what is live.** No central hub, nothing in Settings;
   every control is scattered per artifact page, each with its own idiom.

Goal: one coherent mental model — **History → Live → Promote → Roll back → Diff** — applied
uniformly, plus one place that answers *"what is live, and can I undo it?"*

### Non-goals (v1)

- Resurrecting the multi-stage promotion ladder / approval queue (kept dormant behind
  `SINGLE_ENVIRONMENT`; the panel is designed so it slots back in — see §9).
- Hard eval-gating (we ship advisory; see §6). The gate is **not enforced at the rotation
  boundary** in v1 — it is informational + an audited override.
- Re-architecting the 9 backend versioning idioms into one abstraction (tracked separately,
  Phase 3); this spec defines a **UI-facing normalized model** (§3) instead.

### Principles

- **One component, many adapters.** The panel never branches on artifact type in JSX.
- **Truthful affordances.** A control renders only if the backend can perform it *for the
  current user's scope* (§10). No prose promising actions that don't exist.
- **Live is explicit.** Every history view shows which version is live and which *was* live.
- **Reversible by default.** Promotion is one click to undo; the previous-live target is
  *recorded* so rollback is exact — except where the backend can't record it yet, in which
  case rollback is disabled rather than derived (§7.1).
- **Partial versioning is stated, not faked.** Skills/judges/MCP get honest edit-history, not
  a fabricated version timeline (§7.6).

---

## 2. Surfaces (the three tiers)

| Tier | Surface                                     | Home                                                             | Role                                                                         |
| ---- | ------------------------------------------- | ---------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| 1    | **`<VersionPanel>`**                | Each artifact detail/workspace page*with real version history* | Primary, day-to-day version ops                                              |
| 2    | **Releases & Rollback hub**           | New top-level nav (`/releases`)                                | Cross-artifact "what's live" + promotion/rollback timeline + global rollback |
| 3    | **Settings → Versioning & Releases** | `Settings.tsx` new tab                                         | Policy only: retention/GC windows, gate defaults, live-alias label           |

Settings is **policy, never operations** — you configure rules there, you never pick a version there.

---

## 3. UI-facing normalized version model

The backend exposes ≥9 idioms. The UI normalizes them via a per-artifact **adapter** in
`src/api/`. New backend fields are called out in §7.

```ts
// src/api/versioning.ts  (new)

// Deliberately distinct from assistantTypes.ts `ArtifactType`
// (which carries `mcp_server` and LACKS knowledge_base/test_set).
// mcp_server is intentionally excluded here — handled via §7.6 edit-history.
export type VersionedArtifactType =
  | "prompt" | "workflow" | "knowledge_base" | "test_set" | "tool" | "skill";

export type VersionStatus =
  | "draft" | "published" | "active" | "deprecated" | "archived";

export interface GateVerdict {
  // `state` is the AUTHORITATIVE verdict, computed from BOTH gate rules
  // (aggregate floor AND per-dimension regression — gate.py:71-97).
  // The UI MUST NOT infer pass/fail from score-vs-threshold alone.
  state: "pass" | "fail" | "none" | "pending" | "stale";
  score?: number;             // candidate aggregate (display-only for the aggregate rule)
  baselineScore?: number;
  minAggregateScore?: number; // aggregate floor (gate.py min_aggregate_score)
  worstRegression?: number;   // worst per-dimension regression vs baseline
  maxRegressionDelta?: number;// allowed per-dimension regression (gate.py max_regression_delta)
  evalRunId?: string;
  evaluatedAt?: string;
  // Populated only for optimization-originated candidate versions (refinement pipeline).
  // Hand-saved prompt versions, the generic `llm` eval target (subject_ref NULL), and
  // workflows (no eval-run linkage) return state:"none" until §7.2 linkage lands.
}

export interface ArtifactVersion {
  artifactType: VersionedArtifactType;
  artifactId: string;
  artifactName: string;

  versionKey: string;   // opaque to the panel; each adapter parses it into the client
                        // method's type: Number(versionKey) for setPromptAlias/getPromptVersion
                        // (numeric MLflow version), passed as-is for promoteWorkflow/
                        // activateKnowledgeBaseVersion (string ids). Coercion lives in the adapter.
  versionLabel: string; // display, e.g. "v7", "v1.0"
  ordinal: number | null;

  status: VersionStatus;
  isLive: boolean;      // derived per adapter (see below)
  liveAliases: string[];// [] | ["prod"]  (forward-compatible with the ladder)
  wasLiveUntil?: string;// per-version previous-live pointer (see §7.1)

  author?: string;      // prompt/KB: absent (see footnote)
  createdAt?: string;
  publishedAt?: string; // prompt/KB: absent
  label?: string;       // prompt: commit_message; workflow/KB: absent

  gate?: GateVerdict;
  capabilities: VersionCapabilities;
  raw: unknown;
}

export interface VersionCapabilities {
  hasHistory: boolean;
  canPromote: boolean;
  canRollback: boolean; // resolved per adapter against the recorded previous-live AND §10 scope
  canDiff: boolean;
  canEditDraft: boolean;
  canDelete: boolean;
  gating: "advisory" | "none";
}
```

**`isLive` derivation (per adapter):** prompt = `PromptVersionInfo.current`; workflow =
`version_id === ` the `LIVE_ALIAS` deployment's `WorkflowDeployment.version_id` (requires
fetching deployments in `loadVersions`); KB = `version_id === KnowledgeBase.active_version_id`
(requires fetching the KB record).

**Metadata-gap footnote:** prompt = no `author`/`publishedAt` (`label = commit_message`);
KB = no `publishedAt`/`label`; workflow = no `label` (no source anywhere). `VersionRow`
renders `author·time·note` **conditionally**, omitting each absent segment (§4).

### Adapter contract

`loadVersions(artifactId): Promise<ArtifactVersion[]>` + mutators wired to existing/new client methods:

| Artifact       | History source                                       | Promote                                                                                         | Rollback                                                                                                                                                                                                                                 | Diff                                                         |
| -------------- | ---------------------------------------------------- | ----------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| Prompt         | `listPromptVersions(name)` (`caliberApi.ts:991`) | **`promotePrompt(name, LIVE_ALIAS, Number(versionKey), {gate fields})`** (new, §7.x)   | **`rollbackPrompt(name, LIVE_ALIAS, {gate fields})`** (new; server resolves prevLive, §7.1)                                                                                                                                     | `getPromptVersion`×2 (`:996`) → `@/lib/textDiff`     |
| Workflow       | workflow detail / versions                           | `promoteWorkflow(id, LIVE_ALIAS, versionId)` (`:2400`)                                      | `rollbackWorkflow(id, LIVE_ALIAS)` — target-less, server pops stack (`:2412`)                                                                                                                                                       | `diffWorkflowVersions(a,b)` (`:1921`) → `<GraphDiff>` |
| Knowledge base | KB versions list                                     | `activateKnowledgeBaseVersion(id, vId)` (`:2721`)                                           | **Option A:** `activateKnowledgeBaseVersion(id, prevActive)` with `prevActive` from recorded `previous_active_version_id` (§7.1 extended to KB). **Option B (if deferred):** `canRollback=false`, cell `— (n/a)` | manifest/strategy field diff (light)                         |
| Test set       | `version` + examples                               | — (n/a)                                                                                        | — (n/a) — but**restore-as-new-version** (forward; §7.4)                                                                                                                                                                         | example-set diff by`version` filter                        |
| Tool           | `(name,version)` rows (new list, §7.5)            | status transition only (no live pointer;`status` incl. real `deprecated`, `tools.py:281`) | —                                                                                                                                                                                                                                       | signature/spec diff (light)                                  |
| Skill          | version-history list (new, §7.3)                    | bind (not a version op)                                                                         | rollback checkpoint (Phase-0 stopgap → table, §7.3)                                                                                                                                                                                    | content text diff                                            |

> **Prompt note:** plain saves do **not** rotate the alias (§5 draft path). Only `PromoteControl`
> calls `promotePrompt`. The legacy raw route `POST /prompts/{name}/aliases/{alias}`
> (`set_prompt_alias_version`, `prompts.py:1502-1525`) writes no audit/checkpoint and must not
> be used by the panel — grep `setPromptAlias` call sites during migration.

Where a capability is false (unsupported *or* out-of-scope for the user), the control is hidden.

---

## 4. `<VersionPanel>` component spec

Location: `src/components/versioning/VersionPanel.tsx` (+ subcomponents).

**Composition (corrected):** built from the real primitives in `src/components/ui/`
(`badge`, `button`, `input`, `label`, `textarea`, `tooltip`) and the existing diff renderers
(`GraphDiff.tsx`, `@/lib/textDiff`). Two **new** shared pieces are introduced:

- **`VersionStatusBadge`** (`src/components/`, *not* a `ui/` primitive) — `StatusBadge`'s
  `KNOWN_STATUSES` (`StatusBadge.tsx:9-22`) has **none** of `draft|published|active|deprecated| archived`, so reusing it renders undifferentiated gray. Build a dedicated badge mapping each
  of the five statuses to its own tone+label, matching the per-domain pattern (`ToolStatusBadge`,
  `PlanStatusBadge`).
- **`ui/ConfirmDialog`** — there is **no** dialog/modal/confirm primitive in `ui/` and no
  `@radix-ui/react-dialog` dependency; existing confirms are `window.confirm` or hand-rolled
  inline (`Prompts.tsx`/`Workflows.tsx`/`ObjectStore.tsx`). Add a shared `ConfirmDialog`
  (add `@radix-ui/react-dialog` or extract the hand-rolled pattern); used by `RollbackControl`
  + `PromoteControl`.

Follows the repo's `data-testid` convention throughout.

### Props

```ts
interface VersionPanelProps {
  artifactType: VersionedArtifactType;
  artifactId: string;
  artifactName: string;
  adapter?: VersionAdapter;   // resolved from a registry keyed by VersionedArtifactType; override for tests
  defaultOpenDiff?: boolean;
}
```

### Anatomy

```
┌─ Versions ───────────────────────────────────────────  [ Compare ▾ ] ┐
│  ● v7   ⬤ LIVE     reza · 2d ago    "tightened citation rule"          │
│         gate: ✅ PASS  agg 0.91 ≥ 0.85 · worst regression 0.00 ≤ 0.02   │
│                                                              [Roll back]│
│    v6              reza · 5d ago    "added refusal examples"  [Promote] │
│    v5  was live    sam  · 9d ago    "initial prod"           [Promote]  │
│    v4  draft       aria · 9d ago    "wip"              [Edit] [Delete]   │
│  ┌ Compare v5 → v7 ───────────────────────────────────────────────┐    │
│  │ + node "cite_sources"   ~ prompt.system (12 lines changed)      │    │
│  │ gate min_aggregate_score 0.80 → 0.85, max_regression_delta 0.02 │    │
│  └─────────────────────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────────────────────┘
```

> The `author·time·note` row is the **prompt-style maximal case**, not a uniform layout —
> segments are omitted where the adapter yields `undefined`.

### Subcomponents

- **`VersionList`** — ordered by `ordinal` desc; live pinned visually. `data-testid="version-list"`.
- **`VersionRow`** — label, **`LiveBadge`** / "was live" chip, **`VersionStatusBadge`** pill,
  conditional `author·time·note`, action slot. `data-testid="version-row-{versionKey}"`.
- **`LiveBadge`** — single source of "what's serving"; single-env reads **"LIVE"**, multi-env
  `@{alias}`. `data-testid="version-live-badge"`.
- **`PromoteControl`** — §6 (inline advisory gate). `data-testid="version-promote-{versionKey}"`.
- **`RollbackControl`** — `ConfirmDialog` showing **from vX → to vY (was live until …)**;
  for workflows the target is `WorkflowDeployment.rollback_checkpoint[-1]` (new FE field, §7.x);
  hidden when `!capabilities.canRollback`. `data-testid="version-rollback"`.
- **`DiffView`** — two pickers + artifact-specific renderer. `data-testid="version-diff"`.

### States

- **Loading** — skeleton rows (`animate-pulse`, the pattern used in `Overview.tsx:732`).
- **Empty / single version** — Compare hidden until ≥2.
- **No-history artifacts** (judge/MCP, and skill until §7.3) — panel **not mounted**; the page
  shows the §7.6 read-only **Edit history** instead.
- **Error (load)** — inline retry.
- **Conflict (409 on create/restore)** — reload `loadVersions()`, retry the create **once**,
  then a non-destructive toast (`sonner` `toast(msg, { action })` is already available).
- **Permission** — promote/rollback/edit/delete resolved per artifact (§10); hidden (not
  disabled) for scopes that can't perform them.

### Interactions / guarantees

- **Promote** rotates the live pointer via the audited endpoint *and* records the exact
  outgoing version as the new previous-live (§7.1).
- **Roll back** → `ConfirmDialog` → mutator → toast with **Undo** (re-promote).
- **Concurrency (until Phase-1 409s):** `PromoteControl`/`RollbackControl` **refetch-and-confirm
  the current live pointer immediately before mutating**, and the Undo toast **re-resolves the
  current live target at click time** — *no blind optimistic badge move, no captured re-promote*.
  None of the mutate endpoints take an expected-version/etag guard today (`setPromptAlias` takes
  only `{version}`, `promoteWorkflow` only `{version_id}`, `rollbackWorkflow` unconditionally
  pops the server stack, `promoter.py:1714`), so previous-live tracking alone does **not** make
  optimistic Undo safe — hence refetch-before-mutate until the 409 guard (§7.x / Phase 1) lands.
- **Restore-as-draft** (workflows) is a separate, clearly-labeled "Clone to new draft" button —
  never conflated with rollback.

---

## 5. Per-artifact integration

Drop `<VersionPanel/>` onto each detail page **that has true version history** (prompts,
workflows, KBs, eval datasets, tools, and skills once §7.3 lands). Artifacts without versioning
(judges, MCP, skills until §7.3) get the §7.6 read-only Edit-history — the panel is not mounted.

| Page                      | Action                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Prompts.tsx` workspace | **Add a draft path:** in `PromptAuthorStage` (`Prompts.tsx:1522-1545`) replace the single save (which always sends `target_alias=LIVE_ALIAS`, `:1531-1535`) with **"Save draft"** (registers a version, **no** alias rotation; default in single-env → `status:draft`, `canEditDraft:true`) and **"Save & promote"** (rotates via the audited endpoint). Requires the backend to honor `promote:false` (today it coerces `null→prod`, `prompts.py:1364`). Move version history into the workspace; mount the panel. |
| `WorkflowDetail.tsx`    | Replace the sparse Versions tab;**wire `rollbackWorkflow`** via `RollbackControl` (target from `rollback_checkpoint[-1]`); add `LiveBadge`. Keep `<GraphDiff>`.                                                                                                                                                                                                                                                                                                                                                                                 |
| `KnowledgeBases.tsx`    | Re-skin the existing Version History table onto the shared panel (smallest delta). KB rollback per chosen option (§3/§7.1).                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `EvalDatasetDetail.tsx` | Present the version filter through the panel; add**restore-as-new-version** (§7.4). Keep the MLflow Synced/Stale badge **separate and relabeled** ("MLflow sync: up to date / behind" — it's parity, not liveness; see Q2).                                                                                                                                                                                                                                                                                                                       |
| `ToolDetail.tsx`        | Add the`(name,version)` family list (§7.5); make lifecycle stages transitionable.                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `SkillDetail.tsx`       | **Remove the false rollback prose now** (`:856-858`); mount the panel once §7.3 lands.                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `Judges.tsx`            | Add detail page + instruction editor + read-only edit-history (§7.6, Phase 0).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `McpServers.tsx`        | Edit-history list (§7.6).**Block deletion (409) when referenced by a deployment** and show the referencing list; on delete, snapshot the full server definition into the audit row (today only the name, `mcp_servers.py:235`) and surface **Restore/Recreate-from-snapshot**.                                                                                                                                                                                                                                                                   |

---

## 6. Inline advisory gate (locked decision)

At promote time `PromoteControl` resolves the candidate's `GateVerdict` (§7.2) and renders the
**authoritative `state`** (computed from **both** gate rules — never inferred from
score-vs-threshold):

```
[ Promote v6 to LIVE ]
  gate: ✅ PASS   agg 0.91 ≥ 0.85 · worst regression 0.00 ≤ 0.02   →  one-click promote
  gate: ⚠️ FAIL   agg 0.91 ≥ 0.85 BUT dimension regressed -0.04 (max 0.02)  → promote disabled until:
        ☐ I understand v6 failed the eval gate and want to promote anyway
        reason: [__________________________]   then [ Promote anyway ]
  gate: ◷ pending  eval running               →  promote allowed, shows "eval in progress"
  gate: ◌ none / stale                        →  promote allowed, shows "no recent eval"
```

- **Advisory, not blocking:** FAIL requires an acknowledgment + reason via `ConfirmDialog`; it
  never hard-stops. (§6 already discloses no rotation-boundary enforcement; do not relitigate.)
- **Audited (requires §7.x):** the override is recorded — but **today the prompt alias path
  writes no audit row at all** and the workflow promote audit records only
  `{alias, version_id, deploy-gate result}` (`workflow_deployments.py:133-137`). So the audit
  half is **new backend work**: the new prompt promote endpoint writes the row, and both prompt
  and workflow promote audit details are extended with `gate_state, gate_score, overridden, override_reason` from the request body. Until that lands, treat §6/§8/§10's "records …" as
  **future-tense**.
- **Verdict source:** the verdict needs a candidate-vs-baseline comparison and is **not**
  derivable from the scorecard (`CaliberEvalRun` has no baseline/deltas). It is populated only
  for optimization-originated candidate versions — see §7.2.

---

## 7. Backend additions required

Ordered by phase. Each item names the real files it touches.

### 7.1 Exact previous-live recording (Phase 0)

**Real gap (corrected):** the approval/bundle checkpoint builders derive
`version_before = version_after − 1` (`apply.py:_build_checkpoint:97`, `_build_bundle_checkpoint:148`)
— wrong when intermediate versions didn't rotate the alias — and the **direct prompt alias-set
route writes no checkpoint at all**. (Workflows already record the outgoing target on
`CaliberWorkflowDeployment.rollback_checkpoint`, `promoter.py:1498-1508`, and pop on rollback,
`:1711-1716`; the assistant publisher already captures the prior live version via
`_load_prompt_info`, `publisher.py:148-151`.) **Fix:** read the actual outgoing live alias
version *before* rotation (the publisher's mechanism) and record it as `version_before`,
surfaced as `wasLiveUntil`/`previousLiveVersionKey`. **For workflows: no backend change** —
only expose `rollback_checkpoint[-1]` in the FE type (§7.x-FE). **Extend to KBs (Option A):**
in `activate_version` capture the outgoing `active_version_id` *before* overwriting it
(`service.py:1180`) and persist `previous_active_version_id` (KB model or audit details).
Previous-live tracking alone does **not** make optimistic Undo safe under concurrency (§4).

### 7.x Audited prompt promote/rollback endpoint (Phase 0) — *the keystone fix*

Add `promote_prompt`/`rollback_prompt` routes mirroring `workflow_deployments.py` promote.
In one transaction: (a) read the alias's current version (exact outgoing target); (b) rotate
the MLflow alias; (c) write a `CaliberRollbackCheckpoint` with `version_before=<outgoing>`
(reuse `apply.py` builders); (d) `audit_record(action='promote_prompt'|'rollback_prompt', entity_type='prompt', entity_id=name, details={alias, from_version, to_version, gate_state, gate_score, overridden, override_reason, previous_live_version})`. Thread a DB session + actor
into the route (it currently only calls `require_scopes`). **This is the single data source**
for the prompt half of §6 override audit, §8 "since/by", §10 timeline, and §7.1 exact rollback.
Also extend the workflow promote audit details with `overridden`/`override_reason`.

### 7.x Save-without-promote for prompts (Phase 0)

`create_prompt_version` (`prompts.py:1351-1382`) reads `promote:bool` (default `true`); defaults
`target_alias` to `'prod'` **only when promoting**; forwards `set_prod_alias=promote` so the
existing no-promote branch (`register_prompt_version`, `prompts.py:289-290`) becomes reachable.
Remove the unconditional `null→'prod'` coercion at `:1364`.

### 7.2 Gate verdict persistence + read endpoint (Phase 0) — *NEW work, not a read*

`apply_gate` (`gate.py:51`) needs an `EvalComparison` (candidate + baseline + per-dimension
deltas). The version-addressable scorecard `CaliberEvalRun` (`subject_ref='<name>@<version>'`
for prompts/skills only, NULL for the generic `llm` target) stores `overall_score`/`pass_rate`
but **no baseline, no deltas**, and the standalone run path (`routes/evaluations.py:448`) never
calls `apply_gate`. The comparison *is* persisted — on `CaliberRefinementJob.eval_results`,
`CaliberApprovalRequest.eval_results`, `CaliberRegressionRun` (`baseline_scores`/`deltas`/`gate`)
— but **keyed by `agent_id`/`job_id`/`approval_id`, with no version field**. **Fix (committed
choice — Option a):** persist a per-`(artifactType, versionKey)` verdict at promotion/eval time
(snapshot candidate `overall`, `baselineScore`, `minAggregateScore`, `worstRegression`,
`maxRegressionDelta`, `evalRunId`); expose `GET /…/{id}/versions/{versionKey}/gate`. *(Option b:
add a nullable `version_key` to `CaliberRegressionRun` stamped at promotion/approval and read
it.)* Populated only for optimization-originated candidates; `state:'none'` for hand-saved
prompt versions, the `llm` target, and workflows (no eval-run linkage).

### 7.x Concurrent version-create 409 contract (Phase 0)

`create_version` (`workflow_versions.py:399-411`) and `restore_version_route` (`:450-462`)
compute `MAX(version_number)+1` against `UniqueConstraint("workflow_id","version_number")`
(`models.py:923`) with no `IntegrityError` handling → uncaught **500**. Wrap inserts in
`except IntegrityError` (mirror `tools.py:117`/`skills.py:190`): bounded `MAX+1` retry for
create (transparent), 409 for restore on budget exhaustion. Audit prompt/KB version creation
for the same racy idiom.

### 7.x-FE Surfacing existing backend data (Phase 0, no backend change)

The backend already serializes `WorkflowDeploymentSchema.rollback_checkpoint:list[dict]`
(`schemas.py:1953`) but the FE `WorkflowDeployment` type (`workflowTypes.ts:852-861`) **omits
it**. Add `rollback_checkpoint:{version_id;deployed_at;deployed_by}[]`; the workflow adapter
derives `wasLiveUntil` from `rollback_checkpoint[-1]` (stack top = last element).

### 7.3 Skill versioning (Phase 0 stopgap → Phase 1 table)

- **Phase 0 (b):** write a `CaliberRollbackCheckpoint` with `content_before` on **every** content
  edit (`skills.py:464-481`, today only promotion does) so edits are recoverable; remove the
  false prose.
- **Phase 1 (a):** add an immutable `caliber_skill_versions` row table (true history + rollback);
  mount the panel.

### 7.4 Test-set restore-as-new-version (Phase 1)

Re-append a prior version's example set as the new head (forward restore; backend already
reconstructs "as of version N"). Not destructive rollback.

### 7.5 Tool family list + lifecycle transitions (Phase 2)

`GET /tools/{name}/versions` → the `(name,version)` rows; expose lifecycle stage transitions
(Draft→…→Published) as actions, not just a display badge.

### 7.6 Edit-history + delete recoverability (Phase 0, cross-cutting)

For artifacts without true versioning the panel's fallback is a read-only **edit-history** from
the audit log. For **judges**, capture old/new **instruction values** in the audit row (today
only field *names* are logged) and snapshot the judge definition onto eval runs. For **delete of
any non-versioned entity** (MCP server, judge — both hard-delete today, e.g. `mcp_servers.py:237`),
snapshot the **full definition** into the audit details (not just the name) so the row is
recreatable; surface **Restore/Recreate-from-snapshot** (or add soft-delete + restore mirroring
`CaliberWorkflowFile`, `models.py:1215`).

### 7.7 Releases aggregate endpoints (Phase 3)

- `GET /releases/live` → "what's live now" across prompts/workflows/KBs/skills.
- `GET /releases/timeline?from&to&type` → promotion/rollback events from **per-artifact sources**:
  prompt/agent from `CaliberRollbackCheckpoint`, workflow from
  `CaliberWorkflowDeployment.rollback_checkpoint`, KB activations from the audit log — all unioned
  with the audit log. *Note:* `CaliberRollbackCheckpoint.agent_id` is a **non-nullable FK to
  `caliber_agent_config`** (`models.py:296`) and cannot back workflow/KB rows; do **not**
  generalize it for v1 (read-only union instead).

---

## 8. Releases & Rollback hub

New top-level nav **Releases** (under *Observe*, beside Audit Log), route `/releases`
(`src/App.tsx`), entry in `src/components/Sidebar.tsx`.

```
Releases & Rollback                                            [ type: all ▾ ]
┌─ What's live now ────────────────────────────────────────────────────────┐
│  prompt   support-triage   v7   since 2d   by reza            [ Roll back ]│
│  workflow incident-router  v3   since 5d   by sam             [ Roll back ]│
│  kb       policy-corpus     v12 since 1d   by reza            [ Roll back ]│
└────────────────────────────────────────────────────────────────────────────┘
┌─ Timeline ───────────────────────────────────────────────────────────────┐
│  2d ago  PROMOTE  prompt support-triage v6→v7   gate ✅ 0.91   reza         │
│  4d ago  PROMOTE  workflow incident-router v3→v4  gate ⚠️ overridden  sam   │
└────────────────────────────────────────────────────────────────────────────┘
```

- **v1: read-only** board + timeline. Data is sourced from the audit log (plus per-artifact
  checkpoint/deployment records) **once §7.1/§7.x prompt-audit emission lands** — prompt
  promotions write no audit row today, so this hub is blocked on Phase 0, not a thin read over
  current state.
- Per-row **Roll back** reuses `RollbackControl` + the same mutators.
- Overridden-gate promotions badged; rows deep-link to the artifact's `<VersionPanel>`.
- `data-testid`: `releases-live-board`, `releases-timeline`, `releases-rollback-{artifactId}`.

---

## 9. Single-environment & forward-compat

The panel reads `SINGLE_ENVIRONMENT`/`LIVE_ALIAS`/`DEPLOYMENT_ALIASES` from
`src/lib/environment.ts`. Single-env: `LiveBadge` shows **"LIVE"**, `PromoteControl` targets
`LIVE_ALIAS`. `liveAliases: string[]` already in the model → flipping `SINGLE_ENVIRONMENT=false`
lights up per-alias badges + an alias picker with no component changes; the hub's board becomes
a per-alias matrix.

---

## 10. RBAC (per-artifact — not uniform)

The blanket "promote/rollback=operator; delete=admin" is **wrong**. Real route scopes:

| Artifact       | Promote                                                | Rollback                                                   | Edit                           | Delete                                          |
| -------------- | ------------------------------------------------------ | ---------------------------------------------------------- | ------------------------------ | ----------------------------------------------- |
| Prompt         | operator (`prompts.py:1530`)                         | operator (new endpoint matches`routes/rollback.py:122`)  | operator                       | **admin** (`prompts.py:1393`)           |
| Workflow       | operator (empty`GATED_ALIASES`, `promoter.py:100`) | **admin today** (`workflow_deployments.py:273`) † | operator                       | admin                                           |
| Knowledge base | operator                                               | operator                                                   | operator                       | **operator** (`knowledge_bases.py:147`) |
| Skill / Tool   | n/a / operator                                         | (per route)                                                | per route (skill edit = admin) | admin                                           |

`canPromote`/`canRollback`/`canDelete` are resolved **per adapter** against the real route
scope, so a control renders only if the viewer can perform it (truthful affordances).

† **Decision:** lower workflow rollback `SCOPE_ADMIN → SCOPE_OPERATOR` (derive like promote)
so an operator who can promote can also undo — a Phase-0 backend change to
`workflow_deployments.py:273` + docstring. If product prefers admin-only rollback, keep it and
the FE adapter must require admin. Pick one; don't let §10 and the route disagree.

**Audit & telemetry:** every promote/rollback emits an audit row keyed `entity_type='prompt'| 'workflow'|…`, `entity_id=<name|id>` (so §8's board can resolve "since/by") — this *is* the
timeline source (§7.7). The existing assistant-flow row keyed to `refinement_job/job_id` is
**not** usable for a prompt-keyed board.

**Accessibility:** real list/table; LIVE conveyed by text+icon, not color alone; diff
add/remove labeled, not color-only.

---

## 11. Open questions

1. **Q1 — Skill history model:** Phase-0 checkpoint-on-edit stopgap → Phase-1
   `caliber_skill_versions` table. *(Recommendation: yes, both.)*
2. **Q2 — Test-set wording:** rename MLflow "Synced/Stale" → "MLflow sync: up to date / behind"
   so it stops reading as version activation. *(Recommendation: yes.)*
3. **Q3 — Hub placement:** *Observe* (beside Audit) vs *Platform*. *(Recommendation: Observe.)*
4. **Q4 — Retention defaults:** GC windows per artifact (prompt/workflow/KB versions currently
   unbounded). Also fix `prune_workflow_runs` (`promoter.py:1044-1045`): it filters
   `started_at < cutoff`, but queued/waiting runs keep `started_at` NULL (migration 0030), so
   they're **never pruned** (unbounded run-index growth) — prune on
   `COALESCE(started_at, queued_at) < cutoff`.
5. **Q5 — Workflow rollback scope:** operator (recommended) vs admin (§10 †).
6. **Q6 — Gate verdict source:** persist per-`(type, versionKey)` (Option a, recommended) vs
   `version_key` on `CaliberRegressionRun` (Option b). The FE adapter must match whichever ships.

---

## 12. Final plan (phased; backend + FE aligned)

### Phase 0 — Trust (truthful affordances, decoupled authoring, real audit)

| Item                                                            | Backend                                                                                                                               | Frontend                                                                                                                                                   |
| --------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Prompt save-without-promote**                           | `create_prompt_version` honors `promote:bool`; drop `null→prod` coercion (`prompts.py:1364`)                                 | `createPromptVersion` payload `promote?:boolean`; "Save draft" + "Save & promote" in `PromptAuthorStage`; draft → `status:draft`/`canEditDraft` |
| **Audited prompt promote/rollback + exact previous-live** | New`promote_prompt`/`rollback_prompt` (thread session+actor; rotate + checkpoint + audit); fix `apply.py` derive-by-subtraction | `promotePrompt`/`rollbackPrompt` client methods carrying gate/override; repoint Prompt adapter; only `PromoteControl` calls them                     |
| **Gate verdict persistence + read**                       | Persist per-`(type, versionKey)` verdict; `GET …/versions/{key}/gate`; populate only for optimization candidates                 | `GateVerdict` adapter; `PromoteControl` renders authoritative `state` (both rules); honest `none`                                                  |
| **Advisory-gate audit fields**                            | Extend prompt+workflow promote audit with`gate_state/gate_score/overridden/override_reason`                                         | FAIL path sends`overridden+reason` via `ConfirmDialog`                                                                                                 |
| **Per-artifact RBAC**                                     | Decide workflow rollback scope (Q5)                                                                                                   | Resolve`canPromote/canRollback/canDelete` per adapter; hide non-permitted controls                                                                       |
| **Concurrent-create 409**                                 | `except IntegrityError` + retry/409 on `create_version`/`restore`                                                               | reload+retry once, then non-destructive toast                                                                                                              |
| **VersionStatusBadge + ui/ConfirmDialog**                 | —                                                                                                                                    | new badge (5 statuses) + shared confirm dialog                                                                                                             |
| **VersionedArtifactType + adapter registry**              | —                                                                                                                                    | new union in`versioning.ts`; panel props + registry                                                                                                      |
| **Surface existing data**                                 | — (already serialized)                                                                                                               | add`rollback_checkpoint` to FE `WorkflowDeployment`; derive `isLive`/`wasLiveUntil`; `Number(versionKey)` coercion                               |
| **Skill prose removal + checkpoint-on-edit**              | checkpoint with`content_before` on edit                                                                                             | remove false prose; mount panel after Phase 1                                                                                                              |
| **KB previous-active (if KB rollback in P0)**             | capture outgoing`active_version_id` before overwrite; persist `previous_active_version_id`                                        | KB`canRollback` from recorded prevActive (else `false`)                                                                                                |
| **Judge/MCP edit-history + delete recoverability**        | judge old/new instruction audit + snapshot on runs; MCP full-definition snapshot on delete + 409 reference guard                      | read-only Edit-history; Restore-from-snapshot; referencing list                                                                                            |
| **Mount panel**                                           | —                                                                                                                                    | prompts, workflows, KBs (+ optimistic-safe refetch-before-mutate)                                                                                          |

### Phase 1 — Reproducibility

- Skill version table (§7.3a) + mount panel · Test-set restore-as-new-version (§7.4) ·
  KB per-run version pin · expected-version/If-Match **409 guard** on the three mutate paths
  (removes the refetch-before-mutate workaround) · generalize 409 conflict UX.

### Phase 2 — Coverage

- Tool family list + lifecycle transitions (§7.5) · MCP edit-history + 409 guard completion.

### Phase 3 — Platform

- Releases & Rollback hub (read-only → rollback), built on Phase-0 audit emission ·
  Settings "Versioning & Releases" policy tab (retention/GC, gate defaults, live-alias label) +
  `prune_workflow_runs` `COALESCE` fix · schema hardening.

---

## 13. Alignment risks to watch

- **Gate-verdict source:** FE and BE must agree on Option a vs b (Q6) or `baselineScore`/
  `worstRegression` are null for the demoed prompts; FE must render `none` gracefully.
- **Legacy `setPromptAlias` call sites:** any un-migrated call silently bypasses audit +
  previous-live again — grep and remove; deprecate/lock the raw route.
- **RBAC drift:** workflow-rollback scope must match between route and adapter (Q5).
- **Concurrency:** ship refetch-before-mutate until Phase-1 409s; don't ship blind optimistic UI.
- **Two-threshold gate:** FE derives PASS/FAIL from `state`, never from `score ≥ minAggregateScore`.
- **KB rollback consistency:** §3 adapter, §5 affordance, §7.1 scope, §12 Phase 0 must all agree
  or the FE falls back to the forbidden ordinal−1 derivation.
- **Workflow `rollback_checkpoint` element order:** stack top is the **last** element.
- **Audit keying:** prompt promotions keyed `entity_type='prompt', entity_id=name` so the board
  can resolve them; do not depend on the `refinement_job`-keyed row.

