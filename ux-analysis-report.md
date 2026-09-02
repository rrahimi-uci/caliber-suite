# CALIBER UX Analysis Report

**Scope:** Evidence-based UX review of the CALIBER web UI and its supporting
implementation, conducted per-persona and per-journey against the code as it
exists on `main` at commit `70c4e82345`.

**Method:** Every finding in this report is grounded in a specific file and line
in this repository. Where a claim is quantitative (step counts, page sizes,
adoption rates), the measurement command or file reference is given so it can be
re-run. Nothing here is inferred from general UX heuristics alone; where the
implementation contradicts a plausible assumption, the implementation wins.

**Review stance:** critical product reviewer, not feature advocate. The
objective is a lower-friction product, not a larger one. Recommendations follow
a fixed decision order:

> **Simplify → Consolidate → Improve defaults → Progressive disclosure → Remove → Add**

"Add" appears only where the absence of a capability is itself the friction.

---

## 1. Executive Summary

### The core finding

**CALIBER's backend is materially more honest, more complete, and better
designed than its user interface — and the fastest route to a much better
product is not new features, but rendering the signal the platform already
computes.**

In **sixteen verified cases** (§7.1) the data a user needs to make a decision is
already computed server-side, already typed in TypeScript, and often already in
the browser — and **no component renders it**. Validation errors carry
per-field detail that is thrown away, so every 4xx in the product reads
*"request body validation failed"*. The count of pending approvals is fetched on
**every page load** and displayed nowhere. Judge/human disagreement is computed
example-by-example and then reduced to a single number. A knowledge-base build
failure is fetched to drive a poll and never shown, so a failed build renders as
*"0 chunks · 0 entities"* — indistinguishable from an empty success.

This has a direct consequence for planning: **most of the high-value work in
§13 is small.** Six of the seven Critical issues are XS or S.

### What is genuinely strong

The evidence and honesty layer is the product's differentiator and is
implemented with unusual rigour (§6): the Evidence panel discloses sampling
bounds, digests, and per-scorer denominators; incomplete runs cannot masquerade
as clean ones; baseline comparison *refuses* to compare mismatched suites and
discloses differences rather than hiding them; AGE retrieval states when it fell
back and why; the MCP tool policy denies by default; the health indicator is
real and its comment explains why it refuses to overclaim. Two surfaces —
`Cookbooks.tsx` and `planView.tsx` — are good enough to be templates for the
rest of the app.

### What is wrong

| Symptom | Measure |
| --- | --- |
| **Feedback loops that do not close** | **4 of 4** core loops (prompt regression, evaluation capture, judge trust, KB calibration) dead-end (§7.2) |
| **Nothing tells a human something needs them** | **1** attention badge exists for **12** distinct attention items; pending approvals have **no UI at all** (§7.3) |
| **Permission affordances** | **3** competing models; **~33** mutating controls visible to a viewer who will 403; the assistant labels **every admin "Viewer"** (§7.4) |
| **Concentration of complexity** | **4 of 34** pages hold **47.5%** of all page code; the largest is **9,006 lines** with ~55 feature areas (§8.1) |
| **Dead, duplicated, and lying UI** | **20** dead/unreachable items; **19** helper functions copy-pasted verbatim; ~**7,000** lines removable with zero user-visible loss (§8.2–8.3) |
| **Vocabulary** | **6** names for a test set, **5** for a grader, **5** for "ship it"; *"Calibrate"* means **three different things**, one of which queues an LLM to rewrite your asset (§7.5) |
| **Governance depends on which button you found** | **5** prompt promote paths, only **2** gate-aware; release scores are arithmetic on numbers the gated party typed (§5.1.5, §5.6.3) |
| **Wayfinding** | **31** addressable routes; **0** global search, **0** command palette, **0** global shortcuts; breadcrumbs are **built and used once** (§7.7) |
| **First run** | An empty install opens on **two red 0% tiles and one amber**, and the one good on-ramp is **linked from nowhere** (§4.6) |

### Measured against the product's own intent

ARCHITECTURE.md commits to ten canonical design principles, enforced across four
surfaces by a contract test. Two are directly relevant:

- **#9 "Developer & User Friendly — … for both developers *and business
  users*"**, discharged by *"the SPA surfaces and the Cookbook examples"*. The
  business-user personas the docs themselves recognise (`system-user`,
  `decision-maker`) are not served today: their destinations are a review form
  that shows no evidence, a raw-JSON compliance surface with self-attested
  scores, and a page with no URLs. The Cookbook examples, named as half the
  mechanism, are unlinked.
- **#4 "Evaluation by Design"** is contradicted by the most-used scoring surface
  in the product bypassing the evaluation engine, the judge registry, and the
  evidence contract in favour of a judge prompt hardcoded in the browser bundle.

### Direction

The recommendation is **not to add capability**. Applying
*Simplify → Consolidate → Improve defaults → Progressive disclosure → Remove →
Add*, the target model (§11) takes 21 in-app destinations to 19 — but the real
wins are consolidations: **one** admin destination instead of two, **one**
"tools I can call" catalog instead of four mental models, **one** scoring engine
instead of two, **one** run-monitoring component instead of two mounts, and the
two 8,000–9,000-line pages split along the seams the code already suggests.

Fourteen new capabilities are proposed (§10) and every one clears a hard bar:
**eight are rendering data that already exists**, four are one-field backend
additions, and only two (a command palette and bulk dataset import) are
genuinely new UI.

The seven Critical issues share a shape worth naming: **each one silently
produces a wrong result or destroys work while the interface looks like it is
working.** A button labelled "Save as New Version" that promotes to production; a
"New version" action that versions a different knowledge base than the header
names; a signoff form that submits another candidate's rationale; an editor that
accepts edits it can never save. These are not polish items, and they are almost
all small. That is where to start.

---

## 2. Personas & Jobs-to-be-Done

### 2.1 The persona set is already defined in the repository

CALIBER does not need invented personas — the documentation set declares its own
audience taxonomy in frontmatter. Counting `audience:` values across `docs/`:

| Audience | Docs tagged | Maps to auth scope? |
| --- | --- | --- |
| `developer` | 61 | — (no scope; the default authenticated user) |
| `architect` | 28 | — (no scope) |
| `evaluator` | 26 | — (**no scope**) |
| `operator` | 25 | `caliber.operator` |
| `system-user` | 14 | `caliber.viewer` |
| `decision-maker` | 9 | `caliber.approver` (partially) |

The authorization model in [auth.py:85-106](caliber/src/caliber/auth.py#L85-L106)
recognises exactly four scopes, in an implication hierarchy:

```text
caliber.admin → caliber.approver → caliber.viewer
caliber.operator → caliber.viewer
```

**Finding P-1 — the persona model, the permission model, and the UI disagree
about how many kinds of user exist.** Documentation recognises six audiences;
authorization recognises four scopes; the UI gates on approximately *one and a
half* (`me.is_admin`, plus an ad-hoc scope string check on two pages). Evidence
and impact are developed in §7.4.

### 2.2 Personas used in this report

Five personas, each with a primary job-to-be-done. These are the personas the
implementation actually serves — a distinct surface exists for each.

```mermaid
flowchart LR
  subgraph Builders
    DEV["<b>Developer</b><br/>61 docs · no scope<br/><i>Ship a governed AI feature</i>"]
    ARCH["<b>Architect</b><br/>28 docs · no scope<br/><i>Decide what CALIBER guarantees</i>"]
  end
  subgraph Quality
    EVAL["<b>Evaluator</b><br/>26 docs · <b>no scope</b><br/><i>Prove quality before release</i>"]
  end
  subgraph Runtime
    OPS["<b>Operator</b><br/>25 docs · caliber.operator<br/><i>Keep it running, fix failures</i>"]
    APPR["<b>Approver / Decision-maker</b><br/>9 docs · caliber.approver<br/><i>Gate risky changes</i>"]
    ADMIN["<b>Admin</b><br/>caliber.admin<br/><i>Configure the deployment</i>"]
  end

  DEV --> EVAL --> APPR --> OPS
  ADMIN -.->|enables all| DEV
  ARCH -.->|reviews| APPR
```

| Persona | Primary job-to-be-done | Primary surfaces today |
| --- | --- | --- |
| **Developer** | Author a prompt / workflow / tool and get it working and measured | Prompts, Workflows, Tools, Skills, Cookbooks |
| **Evaluator** | Prove a change is better, and that the judge can be trusted | Test Sets, Judges, Evaluations, Review Queues |
| **Operator** | Detect, diagnose, and recover failures in production | Observability, Workflows (runs), Review Queues, Audit Log |
| **Approver** | Approve or reject a gated change or a paused run | Releases, Workflow run approvals, Plans |
| **Admin** | Configure providers, identity, storage, and platform policy | Administration, Settings, LLM Gateway |

**Finding P-2 — the Evaluator is a first-class documented persona with no
permission scope and no home page.** The evaluator's job spans four separate
nav destinations (`/eval-datasets`, `/judges`, `/evaluations`, `/review-queues`)
plus a duplicate testing surface inside `/prompts`. There is no "quality" entry
point that assembles them. Developed in §7.2.

---

## 3. Current Information Architecture

### 3.1 Navigation as implemented

The IA is declared as data in
[Sidebar.tsx:199-261](caliber/caliber-ui/src/components/Sidebar.tsx#L199-L261) —
6 collapsible groups, 21 in-app destinations, plus a Dashboard and an external
Docs link.

```mermaid
flowchart TD
  ROOT["CALIBER"] --> DASH["Dashboard /"]
  ROOT --> B["<b>Build</b>"]
  ROOT --> R["<b>Resources</b>"]
  ROOT --> I["<b>Integrations</b>"]
  ROOT --> E["<b>Evaluate</b>"]
  ROOT --> O["<b>Operate</b>"]
  ROOT --> A["<b>Admin</b>"]
  ROOT --> D["Docs ↗ external"]

  B --> B1["Workflows"]
  B --> B2["Cookbooks"]
  B --> B3["Agents"]
  B --> B4["Plans 🔔"]

  R --> R1["Prompts"]
  R --> R2["Tools"]
  R --> R3["Skills"]
  R --> R4["Knowledge Bases"]
  R --> R5["Object Store"]

  I --> I1["LLM Gateway"]
  I --> I2["MCP Servers"]
  I --> I3["OpenAPI Integrations"]

  E --> E1["Test Sets"]
  E --> E2["Judges"]
  E --> E3["Evaluations"]

  O --> O1["Releases"]
  O --> O2["Observability"]
  O --> O3["Review Queues"]
  O --> O4["Audit Log"]

  A --> A1["Administration"]
  A --> A2["Settings"]

  classDef badged fill:#fef3c7,stroke:#f59e0b
  class B4 badged
```

Only one destination in the entire navigation carries a badge — Plans, driven by
a 30-second poll for paused Aria plans
([Sidebar.tsx:320-339](caliber/caliber-ui/src/components/Sidebar.tsx#L320-L339)).
Everything else requires the user to visit the page to learn whether it wants
them. See §7.3.

### 3.2 Routes vs. navigation: the unreachable-by-URL surfaces

[App.tsx:294-383](caliber/caliber-ui/src/App.tsx#L294-L383) declares 35 route
entries. Nine are detail routes not present in navigation:

| Detail route | Reached from |
| --- | --- |
| `/tools/:toolId` | Tools list |
| `/agents/:agentId` | Agents list |
| `/skills/:skillId` | Skills list |
| `/eval-datasets/:datasetId` | Test Sets list |
| `/evaluations/:runId` | Evaluations list |
| `/workflows/:workflowId` | Workflows list |
| `/workflows/:workflowId/editor/:versionId` | Workflow detail |
| `/workflow-versions/:versionId` | Workflow detail |
| `/workflow-runs/:runId` | redirect shim |

**Finding IA-1 — the two multi-step creation wizards have no URL at all.**
`ToolWizard` (976 lines) and `SkillWizard` (1,415 lines) are page-sized
components rendered *inside a tab* of another page:

- [ToolRegistry.tsx:269](caliber/caliber-ui/src/pages/ToolRegistry.tsx#L269) — `<ToolWizard onClose={() => setActiveTab("registry")} />`
- [Skills.tsx:753](caliber/caliber-ui/src/pages/Skills.tsx#L753) — `<SkillWizard onClose={() => onCreated?.()} />`

2,391 lines of multi-step authoring flow is therefore not linkable, not
bookmarkable, and not recoverable: a refresh or an accidental tab switch
discards in-progress work with no draft persistence. Every other creation
surface in the product (workflow editor, prompt workspace) *does* have a URL.

### 3.3 Page weight as a proxy for feature density

Measured with `wc -l` over `caliber/caliber-ui/src/pages/` on this branch;
**57,373 lines across 34 page components** (plus the `pages/knowledge/` subdir).

| Page | Lines | Note |
| --- | --- | --- |
| `KnowledgeBases.tsx` | **9,006** | largest single page in the product |
| `Prompts.tsx` | **8,192** | the journey named as most important in this review |
| `WorkflowEditor.tsx` | 5,274 | |
| `WorkflowDetail.tsx` | 4,788 | |
| `McpServers.tsx` | 3,519 | |
| `Skills.tsx` | 2,570 | |
| `OpenApiIntegrations.tsx` | 2,413 | |
| `ToolRegistry.tsx` | 2,352 | |
| `ObjectStore.tsx` | 2,067 | |
| `SkillDetail.tsx` | 1,642 | |
| `SkillWizard.tsx` | 1,415 | no route (§3.2) |
| `ToolWizard.tsx` | 976 | no route (§3.2) |
| *(22 more)* | ≤1,144 each | |

Four pages account for **27,260 lines — 47.5% of all page code** — and each is a
single navigation destination. Page size is not itself a UX defect, but at this
scale it reliably indicates that one destination is being asked to host many
distinct jobs. §8 tests that hypothesis per page and finds it holds.

> **Note on measurement drift.** `Prompts.tsx` is 8,192 lines here; it was 7,732
> on the branch checked out when this review began, having grown 460 lines in
> commit `a9829841b7` ("Add review-first UX for prompt calibration Apply").
> All line citations in this report were re-verified against `70c4e82345`.
> The growth rate is itself relevant to §8: the largest pages are still
> accreting features.

### 3.4 Consistency of shared chrome

Adoption of shared primitives across the 34 pages:

| Primitive | Pages using it | |
| --- | --- | --- |
| `PageHeader` | 18 / 34 | 53% |
| `SearchInput` | 12 / 34 | 35% |
| `FilterBar` | 10 / 34 | 29% |
| `FilterSelect` | 9 / 34 | 26% |
| `PageTabs` | 8 / 34 | 24% |
| `CopyButton` | 5 / 34 | 15% |
| `ProblemsPanel` | 2 / 34 | 6% |
| `EmptyState` | 1 / 34 | **defined locally, not shared** |

**Finding IA-2 — the shared header component's own contract is not met.**
[PageHeader.tsx:1-5](caliber/caliber-ui/src/components/PageHeader.tsx#L1-L5)
states it is *"Used by every top-level page so the header looks identical
everywhere."* It is used by 18 of 34.

**Finding IA-3 — breadcrumbs are built and unused.** `PageHeader` accepts a
`crumbs` array for a real trail
([PageHeader.tsx:15-16](caliber/caliber-ui/src/components/PageHeader.tsx#L15-L16)),
and defaults to a flat `Dashboard › <title>` when omitted
([PageHeader.tsx:36](caliber/caliber-ui/src/components/PageHeader.tsx#L36)).
Exactly one page passes `crumbs`: `ObjectStore.tsx`. So on all nine detail
routes — including `/workflows/:id/editor/:versionId`, three levels deep — the
breadcrumb claims the user came from the Dashboard and offers no way back up to
the parent object. The capability to fix this already exists and is a one-line
change per page.

**Finding IA-4 — `EmptyState` is a local function inside a 4,788-line page.**
Defined at
[WorkflowDetail.tsx:177](caliber/caliber-ui/src/pages/WorkflowDetail.tsx#L177)
and used four times within that same file. No other page can reach it, so empty
states elsewhere are hand-rolled or absent — the highest-leverage moment for a
new user is the least standardised surface in the product.

### 3.5 Global wayfinding: what does not exist

Verified absent across `caliber/caliber-ui/src` (no matching component, and no
supporting dependency in `package.json`):

- **No command palette / global search.** No `cmdk`, `fuse`, or equivalent
  dependency; no palette component. With 21 destinations and nine detail routes,
  every navigation is a manual sidebar traversal.
- **No global keyboard shortcuts.** Six page-local `keydown` handlers exist
  (`WorkflowEditor.tsx:3026`, `Prompts.tsx:219`, `Workflows.tsx:307`,
  `ObjectStore.tsx:395`, and two in the assistant panel) — so shortcuts are a
  per-page dialect rather than a product-level contract.
- **No cross-object search.** A user who remembers an object's name but not its
  type has no way to find it.

---

## 4. End-to-End User Journey Maps

Each journey below was traced by reading the implementing components, not by
reasoning about what the product probably does. Step counts are discrete user
interactions (clicks, typed fields, tab switches, modal opens), counted along
the *fastest* verified path — they are floors, not averages.

### 4.1 Journey: Prompt Management (Developer → Evaluator)

The named journey for this review. Six stages are declared "in pipeline order"
at [Prompts.tsx:71](caliber/caliber-ui/src/pages/Prompts.tsx#L71) with the intent
recorded in a source comment at
[Prompts.tsx:67](caliber/caliber-ui/src/pages/Prompts.tsx#L67) — a comment the
user never sees.

```mermaid
flowchart LR
  subgraph W["Prompt Workspace — 6 tabs, all clickable in any order"]
    direction LR
    A["1 · Author"] --- P["2 · Playground"] --- T["3 · Test Sets"] --- R["4 · Runs"] --- C["5 · Calibration"] --- B["6 · Bind"]
  end

  A -.->|"promote (5 different paths)"| LIVE(["Live @prod"])
  T -.->|"'Save to Test Sets'<br/>does NOT pin the dataset"| X1{{"BROKEN LOOP"}}
  R -.->|"needs a pinned dataset"| X1
  C -.->|"no completion callback →<br/>header never reaches 'Calibrated'"| X2{{"STALE STATE"}}
  W -.->|"observe / monitor:<br/>1 outbound link in 8,192 lines"| X3{{"MISSING"}}

  classDef bad fill:#fee2e2,stroke:#dc2626,color:#991b1b
  class X1,X2,X3 bad
```

**The declared pipeline is not the reachable pipeline.** Three of the twelve
journey steps named in the review brief are absent or broken in the
implementation:

| Journey step | Status | Evidence |
| --- | --- | --- |
| Create | Over-built — 4 on-ramps, only 1 has a stepper | `PromptBuilder.tsx:63`, `:1009` |
| **Configure model** | **Not in Author at all** — a per-tab `AssistantConfig` mutation | `Prompts.tsx:3163`, `:4790` |
| Configure variables | Create-time only; lost after creation | `PromptBuilder.tsx:2214`, `:2234` |
| Author / version | Works well | `Prompts.tsx:1720`, `:1917` |
| Test | Duplicative (two runners) | `Prompts.tsx:4620` and `:1936` |
| Evaluate / baseline / diff | **Strongest surface in the product** | `Prompts.tsx:2342-2472` |
| Calibrate | Overloaded; stale state | `Prompts.tsx:5719` |
| Bind | Works, partly non-functional pickers | `Prompts.tsx:2570` |
| Deploy / publish | 5 competing paths, inconsistent governance | §5.1.5 |
| **Monitor** | **Absent** — one outbound link in 8,192 lines | `Prompts.tsx:5388` |
| **Maintain** | **Absent** — delete / delete-all only | `Prompts.tsx:1307` |

### 4.2 Journey: Workflow authoring & recovery (Developer → Operator)

The workflow journey spans **~700 KB of TSX across 35 components**
(`Inspector.tsx` alone is 6,611 lines with ~45 distinct panels).

```mermaid
flowchart TD
  CB["/cookbooks<br/>install recipe"] -->|"auto-navigates to editor"| ED["/workflows/:id/editor/:vid"]
  ED -->|"workflow is PAUSED<br/>(backend sets it so)"| P1{{"editor has no resume control<br/>AND no back link"}}
  P1 -->|"exit via sidebar"| WD["/workflows/:id"]
  WD -->|"Resume"| WD
  WD -->|"Runs tab → Queue Run"| RUN(["run"])

  NEW["New Workflow"] --> ED
  ED -->|"Validate → Publish<br/>(3-step drawer, step 2 always empty)"| PUB["published version"]
  PUB -->|"Versions tab → Promote"| LIVE(["live alias"])

  RUN -->|fails| DIAG["Runs tab:<br/>17 stacked panels"]
  DIAG -->|"'Open in Editor'<br/>(header button)"| WRONG{{"opens the PUBLISHED version,<br/>silently editable, Save disabled,<br/>work lost on navigate"}}
  DIAG -->|"correct: Versions → Restore as draft"| ED

  classDef bad fill:#fee2e2,stroke:#dc2626,color:#991b1b
  class P1,WRONG bad
```

| Journey | Interactions | Pages | Notes |
| --- | --- | --- | --- |
| Install cookbook → running workflow | 8–10 | 3 | includes one forced wrong turn (the pause/resume split) |
| Author from scratch → published run | 12–14 | 2 | includes a mandatory click through a permanently-empty publish step |
| **Debug a failed run → deployed fix** | **14–18** | **3+** | the primary header button leads to silent data loss |

Three structural findings, developed in §5.2: publishing is **not required to
run** (the editor posts a draft, or even an unsaved manifest snapshot); the only
**Compile/Export** affordance in the product sits on a route nothing links to;
and **19 helper functions are copy-pasted verbatim** between the two largest
files in the journey.

### 4.3 Journey: Evaluation & judge trust (Evaluator)

Both evaluator loops are *open* — they do not return to their starting point.

```mermaid
flowchart LR
  subgraph A["Loop A — regression evidence"]
    A1["Test Set"] --> A2["examples<br/>(2 raw JSON textareas each)"] --> A3["Run evaluation<br/>(blocking, no progress)"] --> A4["Scorecard"]
    A4 -.->|"rows carry no trace_id;<br/>no row actions exist"| AX{{"cannot capture a failing<br/>row back into the set"}}
  end
  subgraph B["Loop B — judge trust"]
    B1["Author judge"] --> B2["Try it"] --> B3["use as Judge.&lt;id&gt; scorer"]
    B3 --> B4["Review Queue<br/>collect human labels"] --> B5["Cohen's κ"]
    B5 -.->|"per_example computed<br/>server-side, dropped by UI"| BX{{"can see HOW MANY<br/>disagreements, never WHICH"}}
    BX -.->|"judge instructions<br/>are not editable"| BY{{"no corrective action<br/>even if you could"}}
  end

  classDef bad fill:#fee2e2,stroke:#dc2626,color:#991b1b
  class AX,BX,BY bad
```

**Step count from nothing to a first scored evaluation: 16 interactions**
minimum; **~46 for a realistic 5-example set with one judge**, of which **10 are
hand-authored JSON blobs**. There is no wizard, no template, no seeded example,
and no bulk import — despite the backend already modelling file-backed dataset
examples (`CaliberEvalDatasetFile`, `db/models.py:1949-1957`) with **zero UI
references**.

**The single most severe finding in the whole review sits here:** the human
reviewer is never shown the thing being reviewed. `ReviewForm` renders
`item.trace_id` and the questions and nothing else
([ReviewQueues.tsx:575-604](caliber/caliber-ui/src/pages/ReviewQueues.tsx#L575-L604)).
A person is asked *"Pass or Fail?"* about `tr-abc123`. The backend proves the
data is available — it calls `fetch_trace_detail(item.trace_id)` in the
alignment path ([review_queues.py:476](caliber/src/caliber/routes/review_queues.py#L476))
— but `GET /review-queues/{id}` returns only ids (`:218-235`). **Every human
label, every κ, and every gate decision downstream is built on labels produced
without evidence.**

### 4.4 Journey: Knowledge base build & retrieval tuning (Developer)

One route, **9,006 lines, ~55 distinct feature areas, 66 `useState` hooks**.
About 20 areas are core; ~33 are expert-only; one is dead code.

```mermaid
flowchart TD
  L["Library"] --> B["Build stage"]
  B -->|"true required decisions: 2<br/>(pick sources, type a name)"| OK(["defaults are sane"])
  B -->|"decision SURFACE presented:<br/>~26 control groups, 45+ options"| NOISE{{"blueprint panel restates<br/>advanced config ABOVE it"}}
  B --> RUN["Build run"]
  RUN -->|"Chunk + Embed steps<br/>can never render 'active'"| S1{{"stepper promises granularity<br/>the events don't provide"}}
  RUN -->|"failed build on the library page<br/>renders as '0 chunks · 0 entities'"| S2{{"a failure is pixel-identical<br/>to an empty success"}}
  L --> E["Explore ▸ Query / Chunks / Graph"]
  E -->|"Graph is a PEER tab,<br/>not disclosed"| G{{"dense-only KB shows 1,385 lines<br/>of AGE machinery for a graph<br/>that does not exist"}}
  L --> C["Calibrate"]
  C -->|"requires eval_dataset_id<br/>with input.question +<br/>expected.sources"| CX{{"contract documented ONLY<br/>in Python docstrings"}}

  classDef bad fill:#fee2e2,stroke:#dc2626,color:#991b1b
  classDef good fill:#dcfce7,stroke:#16a34a,color:#166534
  class NOISE,S1,S2,G,CX bad
  class OK good
```

**The defaults are genuinely good and the product never says so.** The submit
gate requires only a bucket, ≥1 source, and a name; chunking, embedding, and the
entire graph config are pre-seeded from server defaults. Nothing on the page
tells the user *"defaults are fine — pick files and go."*

**The worst defect in this journey is a cross-page schema contract with no UI
expression.** KB calibration requires `input["question"]`
([service.py:5278-5282](caliber/src/caliber/knowledge/service.py#L5278-L5282))
and `expected = {"sources": [...], "answer": "..."}`
([calibration.py:99-131](caliber/src/caliber/knowledge/calibration.py#L99-L131)).
The dataset editor defaults to `{"input": ""}` / `{"expected": ""}`
([EvalDatasetDetail.tsx:523-528](caliber/caliber-ui/src/pages/EvalDatasetDetail.tsx#L523-L528))
and the strings `"question"` and `"sources"` **appear nowhere in that file**.
Wrong key → HTTP 400 *"eval dataset 'X' has no examples"* — a message that is
actively false, since the dataset visibly does have examples.

### 4.5 Journey: Tool / integration governance (Developer → Admin)

**There are four different doors to a callable tool, split across two nav
groups, with four different default governance postures.**

```mermaid
flowchart TD
  subgraph RES["nav group: Resources"]
    T["/tools<br/>native registry"]
  end
  subgraph INT["nav group: Integrations"]
    M["/mcp-servers"]
    O["/openapi-integrations"]
  end
  B["builtin / demo tools<br/>(no nav entry at all)"]

  T --> CAT[["CaliberToolRegistry"]]
  O --> CAT
  B --> CAT
  M --> MCP[["CaliberMcpServer.discovered_tools<br/>separate table"]]

  CAT -->|"ToolDefinition has NO provenance field:<br/>no source, no execution_backend"| BLIND{{"a Stripe HTTP tool, a Python<br/>callable, and a seeded builtin<br/>render as identical rows"}}

  M -->|"deny-by-default ✅"| G1(["clear before wiring"])
  O -->|"approval forced on, never shown"| G2{{"strongest enforcement,<br/>completely silent"}}
  T -->|"defaults read / no-approval,<br/>no coupling to side-effect"| G3{{"one unchecked box from an<br/>unguarded external-action tool"}}

  classDef bad fill:#fee2e2,stroke:#dc2626,color:#991b1b
  classDef good fill:#dcfce7,stroke:#16a34a,color:#166534
  class BLIND,G2,G3 bad
  class G1 good
```

| Journey | Steps | Page contexts | Key friction |
| --- | --- | --- | --- |
| A · Tool: register → schema → test → cases → calibrate → bind | ~12 | 3 | wizard step 4 of 5 is **structurally unreachable**; 3 competing Calibrate entry points; **bind has no home in the tool surface at all** |
| B · Skill: author → render → trigger-test → package → calibrate → bind | ~15 | 2–3 | packaging lives only on a route with **no inbound links**; wizard step 4 is write-only; workflow bind needs a **hand-typed `node_id`** |
| C · MCP: connect → test → discover → policy → invoke → calibrate | ~13 | 1 | most coherent flow in the product — but the workflow binding **discards the policy you just set** |
| D · OpenAPI: import → publish → govern | ~16 | 1 | best-staged pipeline; the `requires_approval` outcome is never surfaced |

**Three parallel 6-stage workspaces exist for Prompts, Skills, and Tools with the
same skeleton and three different vocabularies:**

| # | Prompts | Skills | Tools |
| --- | --- | --- | --- |
| 1 | author | author | **spec** |
| 2 | playground | **render** | **sandbox** |
| 3 | test-sets | **trigger** | **fixtures** |
| 4 | runs | **scenarios** | runs |
| 5 | **calibration** | runs | **hardening** |
| 6 | bind | bind | **publish** |

Their lifecycle ladders are the same five rungs renamed twice
(`Draft → Has test set/scenarios/fixtures → Tested → Calibrated/Hardened →
Bound/Published`), and the tool ladder awards its **top** rung to a tool that is
bound to nothing ([tools.py:1133-1134](caliber/src/caliber/routes/tools.py#L1133-L1134)).

### 4.6 Journey: First run & operate (all personas)

**There is no onboarding.** No first-run detection exists anywhere in the
codebase — no `hasData` / `isEmpty` / `firstRun` branch, no checklist, no tour,
no dismissible getting-started card.

What a brand-new user sees on an empty deployment at `/`:

```mermaid
flowchart LR
  NEW(["fresh install"]) --> DASH["Dashboard /"]
  DASH --> T1["Open work: 0 ✅"]
  DASH --> T2["Fleet coverage: 0% ⚠️ AMBER"]
  DASH --> T3["Execution success: 0% 🔴 RED"]
  DASH --> T4["Publish success: 0% 🔴 RED"]
  DASH --> ACT["4 action tiles:<br/>Prompts · Tools · Skills · Workflows"]
  ACT -.->|"Cookbooks — the ONE good<br/>on-ramp — is absent"| MISS{{"/cookbooks is linked from<br/>nowhere but the sidebar"}}

  classDef bad fill:#fee2e2,stroke:#dc2626,color:#991b1b
  class T3,T4,MISS bad
  classDef warn fill:#fef3c7,stroke:#f59e0b,color:#92400e
  class T2 warn
```

**A fresh install opens on two red 0% reliability tiles and an amber 0% coverage
tile — three false alarms before the user has done anything.** The tone logic
has no zero-denominator branch
([Overview.tsx:191-196](caliber/caliber-ui/src/pages/Overview.tsx#L191-L196),
`:233-244`), and the two "Awaiting signal" fallbacks that would have prevented
this are **dead code**: `assistant_slo` is non-optional, so the ternary always
takes the truthy branch and prints `0/0 completed`.

Every empty state in the operate/admin surfaces is a terminal statement.
Verbatim: *"No recent events yet."* · *"No workflow calibration jobs have been
recorded yet."* · *"Nothing deployed yet in your visible projects."* · *"No
audit entries recorded yet."* **Exactly one** contains a call to action —
Observability's *"No traces yet. Run a workflow or agent with tracing enabled,
then refresh."* ([Observability.tsx:388-395](caliber/caliber-ui/src/pages/Observability.tsx#L388-L395)).

---

## 5. Detailed Workflow Analysis

### 5.1 Prompt Management

#### 5.1.1 The happy path costs 16 interactions — and the cheap path is hidden

Fastest verified route to "author a prompt and get a scored regression result",
via the paste on-ramp:

| # | Interaction | Evidence |
| --- | --- | --- |
| 1 | Sidebar → Prompts | `Sidebar.tsx:214` |
| 2 | **New prompt** | `Prompts.tsx:793` |
| 3 | Choose **Write / paste** | `PromptBuilder.tsx:1179` |
| 4 | Type template | `PromptBuilder.tsx:1157` |
| 5 | Type name | `PromptBuilder.tsx:1193` |
| 6 | **Create** *(silently promotes to live)* | `PromptBuilder.tsx:1268`, promote at `:766-772` |
| 7 | Tab → Test Sets | `Prompts.tsx:102` |
| 8 | **Generate Test Cases** *(LLM wait)* | `Prompts.tsx:5258` |
| 9 | **Run Tests & Judge** *(10 sequential LLM calls at N=5)* | `Prompts.tsx:5311`, runner at `:4315-4358` |
| 10 | Tab → Runs | `Prompts.tsx:116` |
| 11 | **Set as baseline** | `Prompts.tsx:2284` |
| 12 | Tab → Author | `Prompts.tsx:74` |
| 13 | Edit template | `Prompts.tsx:1824` |
| 14 | **Save & promote** | `Prompts.tsx:1906` |
| 15 | Tab → Runs | — |
| 16 | **Run tests** *(LLM wait)* | `Prompts.tsx:2241` |

**16 interactions, 4 tab switches, 3 LLM waits.** The template on-ramp — the
visually primary one — costs **28–35 interactions** for the same outcome
(Step 1 search + goal facets + method multi-select + template card; Step 2
modifiers + 5 element overrides + 3 variable textareas; Step 3 goal + methods +
name + commit: `PromptBuilder.tsx:1583-1615`, `:2214-2253`, `:2344-2485`).

Step 9 issues **two sequential LLM round-trips per test case** (one agent
session, one judge session) with no concurrency and **no cancel control**
([Prompts.tsx:4315-4358](caliber/caliber-ui/src/pages/Prompts.tsx#L4315-L4358)).
At the default N=5 that is 10 serial model calls behind one button.

#### 5.1.2 The core regression loop is broken by a state bug — and the error message prescribes the fix that does not work

> **Persona:** Developer / Evaluator
> **Job-to-be-done:** "Prove my new prompt version didn't regress."
> **Current experience:** Build a test set on the Test Sets tab → click *Save to
> Test Sets* → go to Runs → click *Run tests* → get told to go build a test set.
> **Problem:** `optimizer_config.dataset_id` is written in exactly one place in
> the entire backend — `enqueue_prompt_optimization_run`
> ([routes/prompts.py:473](caliber/src/caliber/routes/prompts.py#L473)) — i.e.
> **only a calibration run pins a dataset.** `saveToEvalDataset`
> ([Prompts.tsx:4996](caliber/caliber-ui/src/pages/Prompts.tsx#L4996)) creates
> the dataset and fires `onDatasetSaved`, which in Workspace mode only calls
> `refreshWorkspace()` ([Prompts.tsx:1676](caliber/caliber-ui/src/pages/Prompts.tsx#L1676))
> — nothing persists the id. So `PromptRunsStage`'s
> `datasetId = workspace?.dataset_id ?? null`
> ([Prompts.tsx:1948](caliber/caliber-ui/src/pages/Prompts.tsx#L1948)) stays
> `null` forever.
> **Evidence:** the dead-end message reads *"No test set is pinned and no
> previous run exists. **Build one on the Test Sets tab first.**"*
> ([Prompts.tsx:2110](caliber/caliber-ui/src/pages/Prompts.tsx#L2110)).
> Following that instruction does not clear it. The only escapes are to run
> tests *on the Test Sets tab* so a prior run exists to scavenge cases from
> (`:2054-2093`), or to start a calibration run. Consequently the lifecycle
> status `Has test set`
> ([prompt_targets.py:125](caliber/src/caliber/prompt_targets.py#L125)) is
> **unreachable** except via Calibration.
> **User impact:** the single most important loop in the product — regression
> testing a prompt — dead-ends on first use, and the remediation text is wrong.
> A new user reasonably concludes the feature is broken.
> **Recommendation:** have `saveToEvalDataset` write `optimizer_config.dataset_id`,
> **or** give Runs an explicit dataset picker (preferred: it removes the hidden
> coupling entirely and makes the dependency visible).
> **Expected improvement:** removes a hard dead-end from the primary journey and
> makes one of five lifecycle states reachable.
> **Priority: Critical**

#### 5.1.3 Long-running work loses its status the moment you look away

Polling exists in exactly **one** place in this 8,192-line page: the Calibration
tab polls its job and the assistant operation every 2s
([Prompts.tsx:6541-6575](caliber/caliber-ui/src/pages/Prompts.tsx#L6541-L6575),
`:6357-6389`). Both are unmounted on tab switch.

`PromptOptimizationTab` is passed **no completion callback** — only
`prompts`/`loading`/`lockedPrompt`
([Prompts.tsx:1690-1694](caliber/caliber-ui/src/pages/Prompts.tsx#L1690-L1694)).
Every sibling stage has one: Author `onSaved` (`:1659`), Test Sets
`onDatasetSaved` (`:1676`), Runs `onAfterRun` (`:1687`), Bind `onBound` (`:1701`).

Consequences, all verified:

1. After a calibration Apply succeeds, the workspace header **does not** flip to
   `Calibrated`; `has_applied_job` is only re-read by `GET /workspace`
   ([routes/prompts.py:2204](caliber/src/caliber/routes/prompts.py#L2204)). The
   user must navigate away and back.
2. Leave the Calibration tab while a job is `running` and **there is no queued-job
   indicator anywhere in the product** — the poll is gone and no badge exists.
3. Everything else is fire-and-forget behind a manual **Refresh** link (Runs
   history `:2472`, Recent Prompt Runs `:7463`).

**The backend already solved half of this and the UI discards it.**
`PromptWorkspaceResponse` returns `last_run` and `baseline_run` summaries
([routes/prompts.py:2280-2290](caliber/src/caliber/routes/prompts.py#L2280-L2290))
with a comment stating they exist so *"the Runs tab can mark/compare against it
without a second round-trip"* (`:2262`). **No `.tsx` file reads either field**;
Runs re-fetches both details itself (`:2004`, `:2028`).

#### 5.1.4 Validation errors are captured, typed, and thrown away

The backend returns structured field errors:
`{"detail": "request body validation failed", "errors": [{loc, msg, type}]}`
([routes/_errors.py:39-68](caliber/src/caliber/routes/_errors.py#L39-L68)).
The client **models them** — `ApiErrorBody.errors?: Array<{loc, msg, type}>`
([api/types.ts:52](caliber/caliber-ui/src/api/types.ts#L52)) — and `ApiError`
carries the parsed body
([api/caliberApi.ts:321-327](caliber/caliber-ui/src/api/caliberApi.ts#L321-L327)).
But the throw keeps only `detail`
([caliberApi.ts:731-732](caliber/caliber-ui/src/api/caliberApi.ts#L731-L732)),
and **no component anywhere reads `.errors`**. Every renderer is
`err instanceof Error ? err.message : "…"` (`Prompts.tsx:544`, `:1804`, `:2135`,
`:2153`, `:2653`, `:4878`, `:5027`, `:6522`, `:6662`, `:6729`).

**Net effect:** any malformed prompt / bind / baseline / calibration payload
shows the literal string **"request body validation failed"** — no field, no
reason, no fix. This is a product-wide defect surfaced here, and the fix is one
shared error component; the data is already on the wire. **Priority: High.**

#### 5.1.5 "Promote to live" has five paths and three different governance rules

| Path | Gate-aware? | Evidence |
| --- | --- | --- |
| Author → *Save & promote* | No — hardcodes `overridden: true` | `Prompts.tsx:1906` → `:1789-1794` |
| `VersionPanel` → *Promote* | **Yes** — reads verdict, requires typed override reason | `VersionPanel.tsx:151`, `:215-218` |
| Versions modal → *Make live* (per row) | No | `Prompts.tsx:1047` |
| Compare diff → *Promote v{n}* | No | `Prompts.tsx:1160-1175` |
| Calibration → *Apply candidate live* | **Yes** — hard-blocks a failed gate | `Prompts.tsx:7933` |

Only two of five paths honour the release gate. The other three hardcode
`overridden: true`, and the backend treats the verdict as advisory by design
([docs/02-prompts/architecture.md:302](docs/02-prompts/architecture.md#L302)), so
nothing catches it. **Governance is therefore a function of which button the
user happened to find.**

Worse, one path promotes without saying so: the Edit modal's button reads
**"Save as New Version"** ([Prompts.tsx:943](caliber/caliber-ui/src/pages/Prompts.tsx#L943))
but `submitEditPrompt` also calls `promotePrompt` with
`override_reason: "direct prompt edit activation"` (`:530-534`). **It goes
live.** A button that ships to production without using the word "live",
"promote", or "deploy" is a correctness problem, not a copy problem.
**Priority: Critical.**

**Rollback is available on exactly one surface** — `VersionPanel`, buried under
Author's Version history (`adapters.ts:52-54`). The modal literally named
"Versions" has **no rollback**, so rolling back from there means *promoting* an
older version — which emits a `promote_prompt` audit event instead of
`rollback_prompt` ([Releases.tsx:38-39](caliber/caliber-ui/src/pages/Releases.tsx#L38-L39)),
corrupting the audit trail for the operator persona downstream.

#### 5.1.6 The page scores prompts with an unregistered judge, so its numbers can't be compared to the product's own evaluation surface

The Prompts page never touches `/judges` — **zero references**. It scores with a
judge prompt hardcoded in the frontend bundle
([Prompts.tsx:4331-4346](caliber/caliber-ui/src/pages/Prompts.tsx#L4331-L4346),
invoked at `:4351`), returning `{verdict, score, reasoning}`.

| Capability | Prompts page | `/evaluations` |
| --- | --- | --- |
| Score a prompt version on a test set | inline ad-hoc judge | `predictTarget="prompt"`, subject `name@version` (`Evaluations.tsx:323`, `:332`, `:348`) |
| Grader choice | 1 hardcoded string | `AVAILABLE_SCORERS` + registered judges (`Evaluations.tsx:355-400`) |
| Registered LLM judges | **unreachable** | first-class (`Evaluations.tsx:377-399`) |

So *"is v3 better than v2 on this test set?"* has **two answers computed by two
different scoring stacks**, and the prompt-page answer is unversioned,
unregistered, and unauditable. This directly contradicts canonical design
principle #4, "Evaluation by Design."

**And the inline judge manufactures false regressions.** Unparseable judge output
becomes a **`fail` with score 0** and `reasoning: "Judge response was not valid
JSON"` (`:4370`, `:4381`) — indistinguishable in the scorecard from a genuine
behavioural failure. A flaky judge therefore produces fake entries in the
`Vs. baseline` regression list, which is the one panel the user is most likely
to trust. **Priority: High.**

#### 5.1.7 Cognitive load: 17 concurrent concepts, and the vocabulary collides

A user must hold: prompt · prompt_name · agent_id · agent_name · hidden prompt
target · alias · version · artifact_ref · test case · test set · eval dataset ·
test run · baseline run · refinement job · candidate · scorer · judge — plus
optimizer ("Calibration Strategy"), gate thresholds, and a 5-value lifecycle
status.

**One thing, two names:**

| Concept | Name A | Name B | Evidence |
| --- | --- | --- | --- |
| Eval dataset | "Test Sets" | "Eval Dataset" / "Existing Dataset" | `Prompts.tsx:102`, `Sidebar.tsx:238` vs `Prompts.tsx:7089` |
| Optimizer | "Calibration Strategy" | `optimizer_type` | `:7069` vs `:6675` |
| Calibration | "Calibration" (tab) | "Optimization" (component + endpoint) | `:130` vs `:5719`, `/prompts/optimization/runs` |
| Promote | "Save & promote" / "Make live" / "Apply candidate live" / "Promote" | — | `:1906`, `:1047`, `:7946`, `VersionPanel.tsx:188` |

**One name, two things:** "Test Sets" is both a workspace stage (`:102`) and the
top-level page (`Sidebar.tsx:238`); "Runs" is both prompt *test* runs (`:116`)
and calibration *jobs* ("Recent Prompt Runs", `:7461`) in adjacent tabs;
"Judge" is both the inline string (`:4351`) and the registered entities on
`/judges`; "baseline" is both the pinned test run (`:1949`) and the
pre-optimization score in the Apply dialog (`:7615`).

**A documented implementation detail is surfaced as a user-facing label.**
`agent_id` is described in
[prompt_targets.py:29-38](caliber/src/caliber/prompt_targets.py#L29-L38) as
*"an implementation detail, never something an operator manages directly."* It
is displayed on every prompt card (`:3931`), keys the Edit modal's switcher
(`:838`), and populates a picker whose `aria-label` is "Select a prompt"
(`:4538`) with agent ids.

**Alias exposure is exactly inverted against its own rule.**
`lib/environment.ts:12` sets `SINGLE_ENVIRONMENT = true` and instructs at
`:16-19` to *"never surface it to users; show neutral wording like
'Live'/'Deployed'."* Author and Edit correctly hide their selectors behind
`!SINGLE_ENVIRONMENT` (`:897`, `:1850`) — but three no-op alias dropdowns remain
unconditionally visible (Playground `:3379`, Test Sets `:5121`, Calibration
`:7046`), each offering a single option, `@prod`: the exact string the rule
forbids showing. **Priority: Medium** (trivial fix, pure noise removal).

#### 5.1.8 Dead controls and stale copy

- **A checkbox that does nothing.** "Open Prompt Calibration after save"
  (`PromptBuilder.tsx:1250`, default `true` at `:197`) passes
  `{ openCalibration }` (`:724`), but the call site drops it —
  `onCreated={(created) => onPromptCreated(created)}`
  ([Prompts.tsx:1654](caliber/caliber-ui/src/pages/Prompts.tsx#L1654)). You
  always land on Author (`:1537`). It only changes the button label to
  "Create and Open" (`:1278`). **The button lies.**
- **~150 lines of dead code with the stepper the page needs.**
  `PromptCalibrationTab` (`:4469-4618`) renders a real numbered stepper
  (`CalibrationStep` / `StepConnector`, `:4580-4615`). **Nothing renders it** —
  zero references outside its own definition, including tests. The import at
  `:35` is reachable only through dead code. The affordance that would fix
  §5.1.9 already ships, unused.
- **Copy referring to a tab that does not exist:** "Create a prompt on the
  Create Prompt tab" (`:3320`, `:5063`, `:6777`) — the entry point is the
  *New prompt* button (`:793`).
- **`Delete all (N)`** (`:1307-1329`) — a single-confirm loop over every deployed
  prompt (`:592-629`), sitting in the browse toolbar next to the view toggle.

#### 5.1.9 Stage order is neither enforced nor discoverable

`PageTabs` renders plain buttons — no `disabled`, no step numbers, no completion
marks, no progress
([components/PageTabs.tsx:24-51](caliber/caliber-ui/src/components/PageTabs.tsx#L24-L51)).
All six stages are clickable at all times, in any order. The only ordering
signals are the array order, an invisible source comment (`:67`), and a status
pill whose five values *do* encode the pipeline (`PROMPT_STATUS_TONES`, `:169`)
but which never maps back to a tab and never marks one "next".

#### 5.1.10 Prompts: Keep / Improve / Consolidate / Hide / Remove

| Verdict | Item | Justification |
| --- | --- | --- |
| **Keep** | `Vs. baseline` panel (`:2342-2472`) | Best surface in the product: net delta, explicit regression definition (`:2404`), per-case output diff, input-text fallback alignment (`:2171-2178`) |
| **Keep** | `CalibrationApplyReviewDialog` (`:7654-7954`) | Only place a production change is gated on evidence; Apply disabled on failed gate (`:7933`) |
| **Keep** | `VersionPanel` promote/rollback (`:1917`) | Only gate-aware, override-audited release path |
| **Keep** | Auto-provisioned hidden prompt targets | Correctly removes an agent-registration step — wrong only in its exposure |
| **Improve** | Pin the dataset on save (§5.1.2) | Unblocks the primary loop |
| **Improve** | Add `onApplied` callback at `:1690` | Header reaches `Calibrated` without navigating away |
| **Improve** | Render `ApiErrorBody.errors` (§5.1.4) | Data already on the wire; kills "request body validation failed" |
| **Improve** | Consume `workspace.last_run`/`baseline_run` | Backend added them for exactly this (`routes/prompts.py:2262`); removes 2 round-trips |
| **Improve** | Stop swallowing errors (`:1988`, `:4788`, `:6178`, `:2603`, `:3129`) | Auth failures currently render as cheerful empty states |
| **Improve** | Rename "Save as New Version" (`:943`) | It promotes to live |
| **Improve** | Show stage order — reuse the shipped `CalibrationStep` | Fixes §5.1.9 with existing code |
| **Consolidate** | Two run surfaces → one (keep Runs) | `:4620` and `:1936` share `runPromptTestCases` (`:4298`) and one endpoint |
| **Consolidate** | Two run histories → one (`:2465`, `:5536`) | Same endpoint, neither aware of the other |
| **Consolidate** | Dataset authoring → `/eval-datasets` | Page mints a new dataset per click (`:4998-5003`), accumulating date-named near-duplicates |
| **Consolidate** | Three diff renderers → one (`:1060`, `:7869`, unused `PromptDiff.tsx`) | Same job, three implementations |
| **Consolidate** | Five promote paths → two | Author-time save+promote, release-time gated promote |
| **Consolidate** | Adopt registered `/judges` (§5.1.6) | Makes prompt scores comparable and auditable |
| **Consolidate** | Reuse shared `CalibrationPanel` as Tools/MCP do | Prompts hand-rolls ~1,800 equivalent lines |
| **Hide** | Assistant-Guided Calibration (`:6804-7014`) | Currently the *first* thing in the tab, above the real form; leaks `plan_id`/`operation_id`/`trace_id`/`correlation_id` (`:6947-7009`) |
| **Hide** | Per-scorer weight + JSON config (`:7217-7265`) | Expert control with server defaults |
| **Hide** | Gate thresholds (`:7276-7297`) | Free-text numerics, no validation/units; defaults exist (`:6456-6460`) |
| **Hide** | Test-case count UI (`:5140-5202`) | Spinner + stepper + 7 chips + hint, for one integer 1–50 |
| **Hide** | Playground file attachment (48 extensions, `:3019-3069`) | A chat feature inside a prompt-QA tool |
| **Remove** | `PromptCalibrationTab` (`:4469-4618`) | Dead code |
| **Remove** | "Open Prompt Calibration after save" checkbox | Non-functional; mislabels the button |
| **Remove** | Stale "Create Prompt tab" copy (`:3320`, `:5063`, `:6777`) | Refers to nothing |
| **Remove** | `Delete all (N)` from the browse toolbar (`:1307`) | Destructive bulk action in a filter bar; belongs in Administration |
| **Remove** | In-modal "Switch prompt" dropdown (`:825-849`) | Changes the edit subject behind a `window.confirm` (`:482`) |
| **Remove** | Duplicate `/prompts/optimization/*` route aliases (`routes/prompts.py:2427-2438`) | Same handlers; the direct source of the Calibration/Optimization vocabulary split |

### 5.2 Workflow authoring & recovery

#### 5.2.1 The primary header button causes silent data loss

> **Persona:** Developer / Operator
> **Job-to-be-done:** "Fix the workflow that just failed."
> **Current experience:** From a failed run, click the header's **Open in
> Editor** — the obvious next action. Edit the graph. Watch the save chip turn
> amber, reading **"Unsaved"**. Find **Save** greyed out with no tooltip.
> Navigate away. Lose everything.
> **Problem:** "Open in Editor" resolves `latest.version_id` as
> `versions.find(published) ?? versions[0]`
> ([WorkflowDetail.tsx:1404-1405](caliber/caliber-ui/src/pages/WorkflowDetail.tsx#L1404-L1405))
> — i.e. the **published, immutable** version, not the run's version and not a
> draft. The editor computes `published`
> ([WorkflowEditor.tsx:2072](caliber/caliber-ui/src/pages/WorkflowEditor.tsx#L2072))
> and uses it to disable Undo, Redo, Save, and Publish (`:4103`, `:4114`,
> `:4123`, `:4205`) — but **`patchManifest` has no `published` guard at all**,
> so every canvas drag, edge wire, and Inspector edit still mutates state and
> sets `dirty`. Autosave correctly refuses (`:3076`, `:3083`).
> **Evidence:** there is no read-only banner anywhere — a grep for
> `read-only|readOnly|immutable|cannot be edited` in `WorkflowEditor.tsx`
> returns nothing. The backend is unambiguous: `409 "only draft versions can be
> edited (published is immutable)"`
> ([workflow_versions.py:633-636](caliber/src/caliber/routes/workflow_versions.py#L633-L636)).
> **User impact:** uncommitted work is destroyed, reached from the most
> prominent button on the page, with the UI actively signalling "Unsaved" —
> i.e. it tells the user there is something to save while making saving
> impossible and never saying why.
> **Recommendation:** guard `patchManifest` on `published`; add a read-only
> banner with a one-click **Restore as draft** (the mutation already exists at
> `WorkflowDetail.tsx:1416-1426`).
> **Expected improvement:** removes the highest-severity data-loss path in the
> product and shortens the debug journey by the wrong turn it currently forces.
> **Priority: Critical**

#### 5.2.2 The only Compile and Export affordance is on a route nothing links to

`WorkflowVersionDetail` is the sole surface with a **Compile** button
([WorkflowVersionDetail.tsx:202-210](caliber/caliber-ui/src/pages/WorkflowVersionDetail.tsx#L202-L210)),
a compiler report (`:319-331`), generated Python (`:333-343`), and YAML/Python
export (`:211-228`). **Nothing in the app links to `/workflow-versions/:id`** —
the only references to that path are the route declaration and API helpers. The
Versions tab links every row to the *editor* instead (`WorkflowDetail.tsx:3236-3241`).

Consequence: `compile_version_route` returns a structured
`{detail, status_code, report}` on `CompileError`
([workflow_versions.py:686-699](caliber/src/caliber/routes/workflow_versions.py#L686-L699))
and the only renderer for it is unreachable. In practice compile errors reach
users as an opaque publish failure — `setMessage("Publish failed: " +
err.message)` (`WorkflowEditor.tsx:2105`) in a small grey toolbar pill, wrapping
the backend's `"version does not compile: {exc}"`.

#### 5.2.3 Publishing is not required to run, and the state machine is not visible

`create_workflow_run` gates on queue-enabled, MCP preflight, paused, and
archived — but **never checks `version.status`**
([workflow_runs.py:602-635](caliber/src/caliber/routes/workflow_runs.py#L602-L635)).
The editor posts `workflow_version_id` with `source: "editor"` and an inline
`manifest` when dirty (`WorkflowEditor.tsx:2125-2135`). So a draft — or even an
unsaved snapshot — produces a real, persisted run, and `WorkflowDetail`'s
"manual (latest version)" alias resolves to `versions[0]` when nothing is
published, meaning it will run a **draft** while labelled "latest version".

| State | Visible where | Invisible where |
| --- | --- | --- |
| Workflow `active`/`paused`/`archived` | detail header, list chips | **all of `WorkflowEditor`** |
| Version `draft`/`published` | editor chip, Versions table | **the Graph tab** — renders `latest.manifest` with no version label |
| Live / deployed | the hidden Deployments tab, `VersionPanel` badge | header, Graph tab, Runs tab |

**"Why can't I run this yet?"** is computed with five branches
(`WorkflowDetail.tsx:1492-1507`) and surfaced **only as a native `title=`
tooltip** on the disabled button (`:3531-3532`). The single most likely
blocker — `!queueRunsEnabled` — gets no inline banner at all.

Worse, that blocker names a setting the UI cannot change.
`workflow_run_queue_enabled`, `..._runtime_approvals_enabled`, and
`..._checkpointing_enabled` all default to `False`
([config.py:539-559](caliber/src/caliber/config.py#L539-L559)) and are flipped
on only by the dev/e2e scripts. `Settings.tsx` has five tabs and **no run-queue
toggle**; `Administration.tsx` has none either. So the message *"Enable the run
queue to execute workflows"* names an action no page can perform — and on
library defaults **every run button in the product is disabled**.

**The backend already ships the remedy and the UI drops it.**
`capabilities.py:110-127` returns `approval_readiness.blockers` plus a
`settings_path`, and its own code comment admits the gap: *"Latent so far
because the type declares the field and no component renders it yet — the same
shape as the Cookbook readiness dead link."* Confirmed: `approval_readiness`
exists in `api/workflowTypes.ts:692-694` and in **no component**.

#### 5.2.4 An approval-gated run is undiscoverable without its URL

| Discovery route | Exists? | Evidence |
| --- | --- | --- |
| Cross-workflow run list | **No** | runs are per-workflow; filters accept only `search` and `artifact_persistence` — **no `status` filter** (`workflow_versions.py:1576-1598`) |
| Global approval queue | **No** | approvals are per-run only (`workflow_runs.py:95`, `:1420`) |
| Sidebar badge | **No** | `badgeKey` is typed as the single literal `"plans"` (`Sidebar.tsx:181`) |
| Dashboard `approvals_pending` | **Different thing** | counts `CaliberApprovalRequest` refinement approvals, not runs (`dashboard.py:118-123`) |
| Runs-tab filters | **No** | `all` / `upload_failed` / `artifacts_stored` only (`WorkflowDetail.tsx:4180-4229`) |

So an approver's only paths are (i) already be looking at that exact run when
the SSE frame lands — and the handler early-returns if a different run is
selected (`WorkflowDetail.tsx:1925-1933`) — or (ii) be handed the URL by a
human. Quorum approvals make it worse: the backend emits
`workflow.run.approval.partial` with `remaining_approvals`
([workflow_runs.py:1507-1564](caliber/src/caliber/routes/workflow_runs.py#L1507-L1564)),
and that event type is absent from both surfaces' subscription lists, so a
partial approval refreshes nothing.

#### 5.2.5 `/agents` is the clearest IA mistake in the product

The page's own banner
([Agents.tsx:123-127](caliber/caliber-ui/src/pages/Agents.tsx#L123-L127)) says:

> "These records bind an MLflow experiment to CALIBER evaluation and refinement
> settings. Runtime model, prompt, and orchestration behavior remains versioned
> in workflow manifests."

Its create form asks for `agent_id`, MLflow experiment ID, `artifact_types`,
`optimize_for`, `required_approvals` — an **optimizer-fleet record**, not a
runnable agent. Meanwhile the thing that actually executes is a `type: "agent"`
node in a workflow manifest, and the palette's node group is *also* called
"Agents". The sidebar lists Workflows · Cookbooks · Agents · Plans as four
peers with no disambiguation. **The destination named "Agents" is not where you
build agents.**

#### 5.2.6 Duplication: 19 helpers copy-pasted between the two largest files

Run monitoring is implemented **twice** — `WorkflowDetail`'s Runs tab and
`WorkflowEditor`'s Run Monitor both render `WorkflowRunRecoveryPanel`,
`WorkflowRunLineagePanel`, `TraceReplayGraph`, `WorkflowRunDebugger`,
`WorkflowRunCheckpointPanel`, `WorkflowSessionMemoryPanel`, and `RunFilePanel`,
plus the resume-by-event form. Nineteen helper functions are duplicated
verbatim across the two files, including `workflowRunResumeCorrelationMatches`,
`workflowRunCheckpointIdentityIssue`, and all five run-action failure-message
builders — roughly 600 lines of copy-paste in the two files most likely to
diverge.

#### 5.2.7 Workflows: Keep / Improve / Consolidate / Hide / Remove

| Verdict | Item | Justification |
| --- | --- | --- |
| **Keep** | Per-node validation + execution badges (`CaliberNode.tsx:72-87`, `:220-227`) | Error/warning/setup counts drawn on the canvas itself |
| **Keep** | `ProblemsPanel` issue → node → **field** focus (`ProblemsPanel.tsx:71-90`) | One click from a validation issue to the offending field |
| **Keep** | Debounced autosave + SPA-unmount flush (`WorkflowEditor.tsx:1704`, `:3082-3101`) | |
| **Keep** | Optimistic locking with a legible 409 (`workflow_versions.py:637-644`) | |
| **Keep** | `?tab=` / `?run=` deep links + cross-workflow resolution (`WorkflowDetail.tsx:2208-2267`) | |
| **Keep** | Cookbook readiness checks with `Configure` deep links (`Cookbooks.tsx:24-51`) | Best onboarding artifact in the product |
| **Improve** | Guard `patchManifest`; add read-only banner (§5.2.1) | Critical data loss |
| **Improve** | Render `error_summary` on the selected-run card | Currently search-only (`:357-358`); the editor already proves the pattern |
| **Improve** | Render `approval_readiness.blockers` + `settings_path` | Backend ships it; nothing consumes it |
| **Improve** | Add a `status` filter to the runs endpoint + `failed`/`waiting_approval` chips | Makes §5.2.4 discoverable |
| **Improve** | Breadcrumb + workflow-status chip + Pause/Resume in the editor toolbar | Removes the cookbook flow's forced page bounce |
| **Improve** | Rename "Run Pipeline" (`:2924-2931`) | It only switches tabs — it runs nothing |
| **Improve** | Add run-history triage columns; stop titling rows with `trace_id` (`:4274-4324`) | Three columns, no timestamp or duration |
| **Consolidate** | Extract the 19 duplicated helpers into `lib/workflowRunActions.ts` | Highest-leverage single change in this journey |
| **Consolidate** | One run-monitoring component, two mounts (`density="full\|compact"`) | ~700 duplicated lines |
| **Consolidate** | Merge the two Versions-tab lists (`:3220` + `:3221-3279`) | Same data, two visual languages, four disjoint verbs |
| **Consolidate** | Fold `WorkflowVersionDetail`'s Compile/Export into the Versions tab | Recovers the only compile affordance |
| **Consolidate** | Merge Cookbooks into the New-Workflow gallery as "Curated examples" | Two galleries, one destination; carries readiness checks to templates |
| **Consolidate** | Collapse Plan + Copilot into one "Describe" surface | Their docstrings say they differ only by whether the graph is empty |
| **Hide** | Calibrate panel in the page header (`:2964-3146`) | Epsilon, candidate budget, deploy-gate dataset — optimizer research controls in a page header |
| **Hide** | Resume-by-event correlation form (`:3862-3925`) | Raw JSON + cross-run correlation, always expanded |
| **Hide** | Files & Artifact Lineage + artifact-persistence filters | Object-store concerns dominating run triage |
| **Remove** | Publish drawer step 2 | `changeSummary={[]}` is hardcoded (`:5253`), so it always reads "First version." — a mandatory click on permanently-empty content |
| **Remove** | `runMut`'s non-queue sync branch (`:1588-1593`) | Unreachable behind the disable |
| **Remove** | Deployments + Promotions tab bodies | Dead under `SINGLE_ENVIRONMENT = true` and `GATED_ALIASES = frozenset()` — and keeping them dormant is what produced the Service-tab dead end |
| **Remove** | Legacy palette components + toggle (`NodePalette.tsx:96-107`) | The node vocabulary has outgrown itself and shipped deprecation UI |
| **Move** | `/agents` out of the Build group; rename "Refinement Fleet" | §5.2.5 |

### 5.3 Evaluation & judge trust

#### 5.3.1 A human is asked to grade an opaque id

Covered as the headline finding in §4.3. The fix is small and the blast radius
is the entire governance story: include the trace request/response on
`GET /review-queues/{id}` and render it above the questions.
**Priority: Critical.**

#### 5.3.2 Both loops are open — the data to close them exists and is discarded

| Loop | Where it breaks | The data that already exists |
| --- | --- | --- |
| Failing scorecard row → back into the test set | `EvaluationDetail` has **zero row actions**, and `EvalRunResultRow` carries **no `trace_id`** (`api/types.ts:1220-1238`) while the only capture endpoint is trace-keyed | — (needs one field) |
| κ → which examples disagreed → fix the judge | UI renders aggregate + FP/FN only (`Judges.tsx:665-691`) | `per_example` with `human_label`, `judge_label`, `judge_score`, `agree`, `error` is returned (`judges.py:316-348`) **and typed** (`api/types.ts:1233-1241`) |
| κ → fix the judge's instructions | **There is no `/judges/:judgeId` route.** `updateJudge` is called with `{status}` only (`Judges.tsx:87`) | `PATCH` accepts description/instructions/model/type/tags (`judges.py:66-73`) |

Because judge names are globally unique (`judges.py:131-142`), a judge with a
typo in its instructions can only be archived — and **cannot be recreated under
the same name.**

#### 5.3.3 A blocking run with no progress, nothing persisted, and no timeout

Evaluation is fully synchronous by design (`routes/evaluations.py:14-17`), up to
50 examples (20 for workflows, each executing a whole compiled workflow). What
the user gets:

- the submit button's text changes to `"Running…"` — no spinner, no counter, no
  elapsed time, **no cancel**;
- **no row appears in the list while it runs** — the row is created only after
  scoring completes (`:626-675`), and `status` is only ever `completed` or
  `failed`, making `StatusPill`'s neutral branch dead code;
- the client sets **no timeout on writes** (`caliberApi.ts:351-354`, `:431`), so
  the tab hangs indefinitely;
- **closing the tab destroys the run** — nothing was persisted, and every LLM
  call was already paid for.

The same app already does this correctly for tool calibration:
`refetchInterval: … queued|running ? 2000 : false`
([ToolCalibrationJobs.tsx:25-31](caliber/caliber-ui/src/components/ToolCalibrationJobs.tsx#L25-L31)).

Two smaller cuts in the same flow: the created `EvalRun` is explicitly discarded
— `const run = await caliberApi.createEvaluation(payload); void run;`
([Evaluations.tsx:261-263](caliber/caliber-ui/src/pages/Evaluations.tsx#L261-L263))
— forcing the user to hunt for it in a list that has **no search and no
filters**; and pre-flight errors are backend-voiced, e.g. *"set
`CALIBER_LLM_PROVIDER` (openai/anthropic) and the matching API key, then
retry"* — a server env-var instruction shown to a UI operator with no link to
Settings or Gateway.

#### 5.3.4 The naming problem, catalogued

**One concept, six user-visible names.** The test set is called *Test Sets*
(nav, title, buttons, empty states), *Eval Dataset* / *Existing Dataset*
(pickers), `eval-datasets` (URL), `dataset_id` (API), and *"eval dataset
'ED-…'"* in every error message the user sees. The docs concede it in writing:
*"(UI "Test Sets", backend `eval-datasets`)"*
([docs/11-test-sets/architecture.md:26](docs/11-test-sets/architecture.md#L26)).

**The grader concept has five names**: *Judges* (nav/page), *Graders* (the
column header and field label — the word a user meets first), *Custom LLM
judges* (the sub-heading inside Graders), *scorers* (the wire field and the
`"unknown scorer 'x'"` error), and *LLM judge* (an unrelated per-calibration
boolean on the Dashboard).

**The `Judge.<id>` token is invisible and misdocumented.** It is computed and
used only as a checkbox `value` (`Evaluations.tsx:26-31`, `:387`), then stripped
and replaced by `⚖ <name>` on read-back (`:69-75`). The backend docstring says
the format is `Judge.<name>` ([judges.py:7](caliber/src/caliber/routes/judges.py#L7))
while the code partitions the **id** (`evaluations.py:334`) — so an SDK user
following the docstring gets a 404, and a UI-only user cannot attribute a
`Judge.JDG-7f3a90c2` string in an audit row at all.

#### 5.3.5 `predict_target` is a first-class capability behind one unlabelled dropdown

The server supports scoring an `llm` completion, a registered `prompt` version,
a `skill`, or a compiled `workflow` version, pinning a content digest of what
was actually scored (`evaluations.py:539-604`). The UI exposes this as **one
select that defaults to `llm` and collapses the subject field entirely**, so a
user who never opens it never learns the other three exist. `subject_ref` is
then **free text with no picker** — you must hand-type `support-greeting@3` or
`SK-…`, and there is no in-app way to find a skill id except copying it out of
another tab's URL. There is **no reverse entry point**: no "Evaluate this"
action on Prompts, SkillDetail, or WorkflowVersionDetail.

Switching to `workflow` also silently drops the sample cap from 50 to 20
(`evaluations.py:461-464`); the user discovers this from a badge *after* paying
for the run.

#### 5.3.6 Evaluation: Keep / Improve / Consolidate / Hide / Remove

| Verdict | Item | Justification |
| --- | --- | --- |
| **Keep** | The **Evidence panel** (`EvaluationDetail.tsx:478-576`) | `bounded sample` vs `full dataset`, `graded N of M`, dataset + result digests, per-scorer denominators, tag slices, and an honest caveat that slice weights don't sum. The most trustworthy artifact in the product |
| **Keep** | Incomplete-row honesty (`:233-242`, `:327`, `:402-425`) | Refuses to let a partial run masquerade as clean |
| **Keep** | Controlled baseline comparison (`:111-122`, `:277-293`) | Requires matching dataset version + scorer suite; *discloses* differences rather than hiding them; `DeltaChip` refuses a misleading `+0pp` |
| **Keep** | MLflow sync badge semantics (`EvalDatasets.tsx:349-391`) | "This is registry PARITY, not version liveness" |
| **Keep** | Judge instruction authoring: insert chips + client mirror of the server validator (`Judges.tsx:34-38`, `:801-829`) | |
| **Keep** | Review-queue auto-advance (`ReviewQueues.tsx:398-402`) | Gives a real queue-worker rhythm |
| **Improve** | Show the trace in the review form | **Critical** — §5.3.1 |
| **Improve** | Render `per_example`; make each disagreement actionable | Without it κ is a number with no next step |
| **Improve** | Persist the run before scoring; poll for progress; navigate to the created run | §5.3.3 |
| **Improve** | Make judges editable (`/judges/:judgeId` + existing PATCH fields) | A typo is currently unfixable |
| **Improve** | Add row actions to the scorecard (+ a trace ref on the row) | Closes loop A |
| **Improve** | Replace free-text `subject_ref` with pickers; add "Evaluate this" from artifacts | §5.3.5 |
| **Improve** | Bulk import of examples | 4 interactions per row makes a 50-row golden set a 200-click job — and `CaliberEvalDatasetFile` already models file-backed examples with **zero UI references** |
| **Improve** | Surface `skipped[].reason` on alignment import; stop silently overwriting hand-typed rows | `Judges.tsx:559-569` |
| **Consolidate** | **One scoring engine** — retire the in-browser judge in `Prompts.tsx` in favour of `POST /evaluations` with `predict_target: "prompt"` | The product's most-used scoring surface bypasses its own engine, judges, and evidence contract |
| **Consolidate** | **One Test Sets surface** — make the prompt workspace stage an embedded view of the real dataset | |
| **Consolidate** | Move Review Queues from *Operate* into *Evaluate* | Human labels are evaluation input, not an ops chore |
| **Consolidate** | Adopt `PageHeader` on the four hold-outs; delete four duplicate local `Chevron()` components | |
| **Hide** | The version-view controls (`EvalDatasetDetail.tsx:218-301`) | Every mutation auto-increments the version, so the dropdown enumerates `v1..vN` where N is the mutation count — unusable at scale. Move to a history drawer |
| **Hide** | "Sync to MLflow" | Registry plumbing shown as a column + two buttons + a 3-state badge legend |
| **Hide** | `predict_target: workflow` | Executes a real workflow per example; *"Preview is an execution mode, not a guarantee that every integration is side-effect-free"* — sitting in a flat dropdown with no cost warning |
| **Remove** | The `non_empty` grader from the default chip row | The backend itself calls it *"too lenient to carry a run on its own"* — yet it can be the sole grader and yield a 100% pass rate |
| **Remove** | The unreachable `skipped` review status | Rendered and typed, but no UI can set it |
| **Remove** | The always-empty Tag column/filter on Test Sets | The create form has no `tags` field |

### 5.4 Knowledge base build & retrieval tuning

#### 5.4.1 The bloat exhibit, quantified

| Metric | Count |
| --- | --- |
| Lines in one page component | **9,006** |
| `useState` hooks | **66** |
| `useEffect` hooks | 17 |
| `useApiQuery` calls | 13 |
| `<select>` / `<input>` / `<button>` elements | 19 / 27 / 59 |
| Module-scope helpers before the component | 27 (**11 of them AGE-specific**) |
| Distinct feature areas | **~55** (≈20 core, ≈33 expert-only, 1 dead) |
| Tests in its suite | 43 — **20 of which name AGE** |

Nearly half the page's test surface and 40% of its helpers exist for Apache
AGE, an optional backend that is **off by default**
([knowledge/schemas.py:502](caliber/src/caliber/knowledge/schemas.py#L502)).

**Duplication census — same data, multiple renderers:** entities/relationships
have **four** renderers; graph query presets **two** (identical five cards);
the query playground **two** (a full one and a "Graph Retrieval Probe", whose
~230-line result cards have already drifted apart); the AGE sync button two;
**Top K appears twice on the same screen bound to the same state**; and the
library list/grid views duplicate ~320 lines including two copies of the
delete-confirm flow.

#### 5.4.2 The configuration burden is 2 decisions, presented as 26 control groups

**The defaults are correct and the product never says so.** The submit gate
requires only a bucket, ≥1 source, and a name; chunking (`recursive`), embedding
(`BAAI/bge-m3`, the server's `recommended` spec), and the entire graph config
are pre-seeded from server defaults. `graph_config` is `| None` at the API
boundary — the whole graph subsystem is optional.

Three things defeat that good default:

1. **A "Pipeline blueprint" panel restates the advanced config *above* the
   collapsed disclosure** (`:3486-3644`), including a "Quick graph preset" pill
   row — so the user is asked to choose a graph posture before they know what
   one is.
2. **The one blocking error lives inside the collapsed disclosure.** When local
   embeddings are flagged, submit is permanently disabled and the only
   explanation renders at `:3765-3769` **inside** the collapsed block. The test
   suite itself has to call `openBuildAdvanced(user)` before asserting the
   message. A first-time user on a flagged runtime sees a dead grey button and
   no reason.
3. **Server `tags` are dropped.** Options carry `recommended` / `fast` /
   `default` tags server-side; the UI renders only `name` and `description`, so
   nothing marks the recommended choice.

Twelve distinct graph vocabularies (~45 enumerated values) are exposed across
three surfaces with three parallel state trees. And `"custom"` is reachable by
accident — `graphBuildPresetMatch` does exact equality over every patch key, so
nudging one number silently flips the label to "Custom graph profile" with
**no "Reset to recommended" anywhere.**

#### 5.4.3 Build observability: a stepper that promises granularity the events don't provide

`kbBuildStepStates` jumps `reached` from `1` straight to `4`, so **`Chunk` and
`Embed` can never render as `active`** — they flip pending→done in one tick.
`failed` is computed globally but applied only at `index === reached`, so an
embedding failure at step 3 draws a red X on **Extract**.

**A failed build is invisible on the library landing.** `kbSummaryBuilding`
covers only `queued|processing|running|pending`, so a `failed` summary falls
through to the counts branch and renders **"0 chunks · 0 entities"** — pixel-
identical to a KB that built successfully over an empty folder. And
`last_run_status` **is** fetched: it drives `refetchInterval` and is **never
rendered**.

#### 5.4.4 Two state bugs that silently target the wrong knowledge base

`selectedKnowledgeBaseId` auto-seeds to `knowledgeBases[0]`, and **"New
knowledge base" does not clear it**. Therefore:

- a workspace headed "New knowledge base / draft" renders a **"Build Runs"**
  panel for an unrelated corpus; and, more seriously,
- **the "New version" button stays enabled** (it is gated on
  `!selectedKnowledgeBase`, which is non-null), and clicking it **overwrites the
  user's hand-picked sources** with the other KB's manifest, then submits
  `createKnowledgeBaseVersion(selectedKnowledgeBase.knowledge_base_id, …)` —
  **silently creating a version of a different knowledge base than the header
  names.** **Priority: Critical.**

#### 5.4.5 The playground misrepresents `hybrid` in three places

`hybrid` is a first-class server mode (BM25 + vector RRF, no graph involvement):

1. **The score breakdown doesn't explain the score.** `_retrieve_hybrid` returns
   `score = rrf_score` with a breakdown of `dense`/`lexical`/`rrf`; the UI
   renders **neither `lexical` nor `rrf`**. So a hybrid result shows
   `score 0.031` beside a breakdown reading `dense 0.847` — visibly
   contradictory numbers in the one panel meant to establish grounding trust.
2. **A fake, empty "Graph context" panel renders.** Gated on
   `retrieval_mode !== "dense"`, so every hybrid answer gets a blue box reading
   *"0 boosted"* and *"No direct entity matches for this question."* — implying
   graph retrieval ran and found nothing, when no graph was consulted.
3. **The version summary omits it entirely** — `graphConfigSummary` hardcodes
   `["Dense", "GraphRAG hybrid"]`.

Same root cause elsewhere: `playgroundUsesGraphModes` is
`some(mode => mode !== "dense")`, so selecting Hybrid enables the whole
query-time graph-tuning block, letting the user set knobs the hybrid path
silently ignores.

#### 5.4.6 KB: Keep / Improve / Consolidate / Hide / Remove

| Verdict | Item | Justification |
| --- | --- | --- |
| **Keep** | 4-stage workspace IA (Build/Explore/Calibrate/Use) | Genuinely good spine |
| **Keep** | Sane defaults; only 2 truly-required inputs | The right call — just undiscoverable |
| **Keep** | Conditional 2s polling throughout | Correct pattern; the rest of the app should copy it |
| **Keep** | "Auto" retrieval mode = follow the saved policy; compare-collapsed version pinning | Careful, correct scoping |
| **Keep** | AGE fallback honesty (pills, reasons, strict-mode explanations) | |
| **Keep** | Per-chunk score breakdown + matched-entity chips | Best grounding evidence in the product — once §5.4.5 is fixed |
| **Keep** | Object Store preview modal (8 kinds + server-side Office extraction) | Best-executed piece of either journey |
| **Improve** | **Document the eval-example contract in the UI** (`{"question": …}` / `{"sources": […], "answer": …}`) and make the 400 say *"N examples found, 0 with an `input.question` field"* | The only defect that makes a documented feature unusable while appearing to work |
| **Improve** | Explain `—` metrics and blanket `fail` verdicts (missing `expected.sources`; no judge configured) | Currently four dashes and an all-red table with no cause |
| **Improve** | Move the embedding-blocked reason outside the collapsed disclosure | §5.4.2 |
| **Improve** | Show failed builds on the library landing (render `last_run_status`) | §5.4.3 |
| **Improve** | Clear `selectedKnowledgeBaseId` on create; gate "New version" on `!creatingKnowledgeBase` | §5.4.4 — silent wrong-target writes |
| **Improve** | Gate graph panels on `graph_hybrid`/`age_graph`, not `!== "dense"`; render `lexical`/`rrf`; fix `graphConfigSummary` | §5.4.5 |
| **Improve** | Paginate the KB source picker (`BucketTree` never reads `next_token`/`is_truncated`) | **Silent data loss** — a folder >1000 objects is capped with no warning at the top of the KB journey |
| **Improve** | Add "Reset to recommended" whenever the profile reads "Custom" | |
| **Improve** | Default calibration `retrieval_mode` to the version's saved default, and warn when it differs | Calibration currently measures a different retrieval path than production |
| **Improve** | Link Calibrate → Test Sets; badge the tab when no test set exists | The tab has no `Link`/`href` at all |
| **Consolidate** | Extract `<GraphRetrievalControls>` (one component, three scopes) | ~600 lines of near-identical clusters |
| **Consolidate** | Extract `<GraphPresetPicker>` and `<KnowledgeQueryResultCard>` | Four preset renderers; ~570 duplicated lines of result card |
| **Consolidate** | Extract `<KnowledgeBaseListItem variant>` | ~320 duplicated lines |
| **Consolidate** | One object-store browser (`BucketTree` vs `ObjectStore` table; **three** `humanSize` copies) | |
| **Consolidate** | **Split the file** into `pages/knowledge/` — the directory already exists with one sibling | 9,006 lines in one route |
| **Hide** | **The entire Graph sub-view** — gate on `entityCount > 0` | 1,385 lines of AGE machinery shown as a peer tab for graphs that don't exist |
| **Hide** | The 17-row "Apache AGE Retrieval" diagnostics card; the 12-badge metadata row; the 16-chip entity-type multi-select | Debug telemetry at top level |
| **Remove** | The object-store launch deep-link machinery | ~50 lines with **zero production callers** — `BucketTree`'s own docstring says the launchers were removed — plus a test guarding the unreachable path |
| **Remove** | "Graph Retrieval Probe" + `KnowledgeQueryCompactResultCard` | ~410 lines: a second ask-box and a second result renderer that have already drifted |
| **Remove** | Legacy `KnowledgeTab` alphabet + `setActiveTab` shim | The comment admits it exists to avoid touching 8 call sites |
| **Remove** | The "Pipeline blueprint" panel; the Chunks-tab "GraphRAG Lineage" card; the "AGE ready" library badge; the duplicate Top K | §5.4.1–2 |
| **Remove** | Object Store's "Add immutable copy to active project files" | Names a destination with **no page, no nav entry, and no link** |
| **Remove** | Object Store's fake upload-cancel (a `span` titled "Cancel upload" with no handler) | |

### 5.5 Tools, skills & integration governance

#### 5.5.1 ~80% of mutating controls have no client-side permission gate

Three permission models coexist: `me.is_admin` (8 pages), scope strings (2
pages), and project-level `access_role`/`permissions` (1 page). The backend
enforces **any-of scopes** with `ADMIN ⊃ {APPROVER, OPERATOR, VIEWER}`, so
**OPERATOR cannot satisfy an ADMIN gate** — and `is_admin`, the dominant UI
model, cannot express the OPERATOR tier that gates most writes.

| Population | Controls visible that will 403 |
| --- | --- |
| **VIEWER** | **~33** |
| **OPERATOR** | **8** (Register Tool, tool-wizard submit, skill Author-stage Save, Add Server, Test, discover, Invoke, Save Policy) |

`OpenApiIntegrations.tsx` contains **zero** permission checks across 11
mutations. `CalibrationPanel` has **no permission prop at all** and is
instantiated four times.

Two standout bugs:

- **`McpServers.tsx:1631-1633` states the intent and doesn't implement it:**
  *"MCP server config (register / edit / delete) is an admin-only operation on
  the backend; gate the mutating controls so only admins see them."* `isAdmin`
  is threaded to edit/delete only — **Register and Test are ungated.**
- **OPERATOR can create a skill but cannot edit it** (`create_skill` →
  OPERATOR; `update_skill` → ADMIN). The Author-stage Save is the primary
  authoring action for the primary authoring role, and it 403s. Tools have the
  **reverse** asymmetry (create = ADMIN), so the two surfaces disagree about
  who may create, with no stated rationale.
- **One inverse bug:** skill package import is gated on `isAdmin` but needs only
  OPERATOR — operators who *are* permitted cannot see the control.

#### 5.5.2 "Calibrate" is one word for three genuinely different actions

| Semantics | Surfaces | Consequence |
| --- | --- | --- |
| **Measure** (sync, nothing changes) | tool (×2 mechanisms), MCP tool, knowledge base | "tell me my pass rate" |
| **Propose a mutation** (queues an LLM optimizer, needs approval) | **skill, prompt, workflow** | "let an LLM rewrite this and file it for approval" |
| **Hyperparameter search** | workflow | objective + epsilon + candidate budget |

**Same verb, same button styling, opposite consequence.** Input surfaces range
from **zero inputs** (skills: one button) to a **seven-field form with scorer
weights and regression gates** (prompts). Nothing signals the difference. The
alias trail `PromptOptimizationOptions = PromptCalibrationOptions` shows
"Optimization" was the original — and better — name.

**Within the tool surface alone, the identical `CalibrationPanel` renders three
times**, and the Fixtures-stage instance contradicts its own docstring (*"It is
the storage surface only; the deterministic suite is RUN on the Hardening
tab"*) while shipping a working Calibrate button that persists **less** state
than the Hardening one. Two identically-labelled buttons on adjacent tabs
produce different durable results.

Meanwhile the *durable* async mechanism — whose backend docstring says the
inline route is *"fine for a handful of cases and wrong for two hundred: the
client holds a connection open for minutes, a proxy timeout or a closed lid
loses the result"* — is rendered **only on the orphaned detail page**. The page
users actually reach offers only the mechanism the backend calls wrong.

#### 5.5.3 Governance is weakest at the default door

| Door | Default posture | Verdict |
| --- | --- | --- |
| MCP | **`allowed: false`** — invoke/test/calibrate blocked until an explicit allow + side-effect + approval classification is saved | **Strongest, and clearly communicated** |
| OpenAPI | `requires_approval` **forced on** for any non-read tool, cannot be disabled | Strongest enforcement, **invisible in the UI** |
| Builtin | Curated per-tool map | Fine, but invisible |
| **Native registry** | **`side_effect_level: "read"`, `requires_approval: false`, with no coupling between them** | **Weakest — and it is the default door** |

The native wizard offers *"🔴 External Action — Sends data to external
services"* and an **independent, unchecked-by-default** "Requires Approval"
checkbox, with no coupling, no warning, and no validation. Select red, leave
approval off, submit — accepted, client and server.

**And the workflow binding silently discards the MCP policy you just
configured:** `lib/workflowGraph.ts:2462-2469` hardcodes
`side_effect_level: "read"` and `requires_approval: false` for auto-created MCP
bindings, though `server.tool_policies` is on the same object. This is not a
security hole — the deploy gate catches it — but the model is **enforced late
and expressed as a compiler blocker** rather than prevented at bind time. The
user configures `external_action` + approval in the playground, wires it in, and
meets the contradiction at deploy in language describing a manifest rather than
their decision.

#### 5.5.4 Structurally unreachable UI in both wizards

- **`ToolWizard` step 4 of 5 can never render its content.** `PlaygroundStep`
  requires `registeredToolId`, which is set only in `registerMut.onSuccess` —
  which immediately navigates away, unmounting the wizard. So the step can only
  ever show its amber placeholder: *"Playground available after
  registration."* **20% of the wizard is a dead end telling you to go
  elsewhere.**
- **`SkillWizard` step 4 is write-only.** Trigger phrases are stashed into an
  untyped `meta.test_triggers` blob read by exactly one Python module and **no
  UI page**. The user types should-trigger/should-not-trigger lists, then finds
  an empty form in the stage named after them.
- **Neither wizard enforces sequence** — both step indicators are
  click-to-jump and tool submit validates only steps 0–1. They are tabbed
  forms wearing wizard chrome.
- **Both detail routes are orphaned.** `ToolDetail` (664 lines) and
  `SkillDetail` (1,642) have **zero inbound links** from any page; list rows
  open the embedded workspace instead. **2,306 lines of parallel detail UI that
  primary navigation never reaches** — and they hold the only durable
  calibration surface and the only skill-packaging surface respectively.

#### 5.5.5 Tools/Skills/Integrations: Keep / Improve / Consolidate / Hide / Remove

| Verdict | Item | Justification |
| --- | --- | --- |
| **Keep** | MCP deny-by-default policy gate | Best governance affordance in the product; make it the template for the other three doors |
| **Keep** | OpenAPI staged pipeline (*"Importing a spec never creates a runtime tool by itself"*) + publish preconditions | |
| **Keep** | Approval forcing for non-read OpenAPI drafts; max-severity pack ranking | |
| **Keep** | Honest destructive labels (*"allow live preview (fires a real request)"*) | |
| **Keep** | `ToolCalibrationJobs` — durable, snapshotted, retry/abandon, and **the only correctly scope-gated control in the surface set** | Keep it; move it somewhere reachable |
| **Improve** | Add `execution_backend` + provenance to `ToolDefinition` and badge the source in `/tools` | ~3 lines; unlocks the unified catalog. The data already exists server-side |
| **Improve** | Couple side-effect → approval in the native wizard, client **and** server | Closes the only unguarded path to an approval-free external-action tool |
| **Improve** | Seed MCP workflow bindings from `server.tool_policies`; add `side_effect_level` to registry bindings | ~10 lines; turns a late deploy blocker into a correct default |
| **Improve** | Surface `requires_approval` on the OpenAPI draft card | The strongest governance in the system is silent |
| **Improve** | Add a permission prop to `CalibrationPanel`; standardise on scope gating; adopt the graceful-fallback message pattern | Removes ~8 of ~33 false affordances immediately |
| **Improve** | Reconcile the create-scope asymmetry; let OPERATOR edit what OPERATOR created | |
| **Improve** | Read `test_triggers` back into the Trigger Tests stage | |
| **Improve** | Replace the `mcp:server/tool` string convention and the hand-typed skill `node_id` with pickers | |
| **Consolidate** | **Delete both orphaned detail pages**, migrating `ToolCalibrationJobs` into Hardening and skill packaging into a workspace stage | 2,306 lines out; two broken journeys fixed |
| **Consolidate** | Collapse Fixtures + Hardening into one stage | Same panel, same handlers; keeping both guarantees hitting the non-persisting one half the time |
| **Consolidate** | Unify the three stage vocabularies and align the lifecycle ladders | |
| **Consolidate** | **Split the "Calibrate" verb** — reserve *Calibrate* for measurement, use *Optimize* for mutation-proposing surfaces | §5.5.2 |
| **Consolidate** | Give tools a Bind stage + endpoint mirroring skills | Journey A currently terminates outside the tool surface |
| **Consolidate** | Build one **"Tools I can call"** catalog (registry ∪ MCP discovered tools), filterable by source / side-effect / approval | Largely a query change once provenance exists |
| **Hide** | OpenAPI Dependencies + Graph tabs (~700 lines) | Sophisticated, irrelevant to import→publish |
| **Hide** | Tool `secret_refs` free-text entry; raw-JSON schema toggles | A typo silently produces an unresolvable reference; OpenAPI already validates this |
| **Remove** | `ToolWizard` step 4 | Structurally unreachable §5.5.4 |
| **Remove** | The Fixtures-stage Calibrate button | Contradicts its own docstring |
| **Remove** | The global `updateAssistantConfig` side effect from tool/MCP test generators | A per-tool panel silently rewrites a **global** setting |

### 5.6 Dashboard, operate & administration

#### 5.6.1 Nine of ten backend setting groups are invisible

`GET /settings/runtime` returns ten groups: `assistant`, `model-providers`,
`memory`, `storage`, `knowledge`, `security`, `runtime-advisories`,
`operations`, `tool-sandbox`, `versioning`. The UI renders **exactly one** —
`groups.find(g => g.id === "versioning")`
([Settings.tsx:196](caliber/caliber-ui/src/pages/Settings.tsx#L196)) — and the
endpoint is `GET`-only, so even that one is display-only.

The group whose name most obviously belongs under "Administration" —
`security` — is rendered nowhere. And this produces a **dangling reference in
shipped copy**: Settings tells the user *"Workflow-run retention is configured
under the Operations group (`CALIBER_WORKFLOW_RUN_RETENTION_DAYS`)."* **There is
no Operations group in the UI.**

#### 5.6.2 The Settings / Administration split is not principled

| | Settings (5 tabs) | Administration (3 sections) |
| --- | --- | --- |
| Writes anything? | Aria + Providers only — **4 of 5 tabs are read-only** | all three |
| Contents | Aria model/effort, provider keys, service health table, read-only versioning list, **an Allure URL bookmark + `make` commands** | accounts, project access, secrets |

Five specific breaks: `security` is in neither; a page called "Settings" is
mostly unchangeable; **Aria configuration lives in three places** (Settings tab,
panel gear, and the composer's inline selector — all writing the same global
mutation); provider **credentials** are split from provider **routing**
(`/gateway`); and Settings ships build tooling (`cd caliber && make
test-allure`) in a production admin UI.

`Administration.tsx` is also the only page in the reviewed set **styled for a
dark canvas on a light shell** — every input is `bg-slate-900 border-slate-700`,
with pastel `text-*-400` status copy on `bg-surface-50`, plus two hand-rolled
bare `<table>`s with no overflow wrapper (a correct one exists 150 lines below).

#### 5.6.3 Releases: an audited compliance surface built on self-attested numbers

`_evaluate_candidate` reads `score` **straight out of the JSON the operator
typed**:

```python
weighted = sum(float(item.get("weight", 0)) * float(item.get("score", 0))
               for item in criteria)
```

`evidence_refs` are **never resolved** — their only use in the file is as report
hyperlinks. No evaluation run is fetched, no judge result read, no gate verdict
consulted. `/gate-verdicts` has API-client methods and **zero UI consumers**.

Meanwhile the page promises *"Weighted evidence, blockers, waivers, accountable
decisions."* Three of six required fields are **free-text identifiers with no
picker**; criteria and evidence are **raw JSON textareas**.

Two undisclosed traps:

- `DEFAULT_CRITERIA` ships `score: 0` for both criteria, so the out-of-the-box
  form produces `weighted_score = 0.0`, `status = "blocked"` — and
  **`Sign off GO` is disabled unless status is `ready`**. The default path leads
  to a permanently unsignable candidate.
- **There is no `PATCH` on a candidate.** Criteria are immutable after creation,
  so "Re-evaluate" recomputes the same arithmetic forever. The only escapes are
  an admin waiver or a brand-new candidate. Nothing says so.

**And a correctness bug in the compliance surface itself:** `rationale`,
`waiverKey`, and `waiverReason` are single state values at factory scope, shared
across the whole candidate list. Every row's rationale input is bound to the
same string — typing a rationale for candidate A fills it for candidate B, and
clicking B's **Sign off GO** submits what you wrote for A. **Priority:
Critical.**

Governance reasons are additionally collected through **three
`window.prompt()` calls** — abandoning a release, retry/skip on an indeterminate
effect, and acknowledging a dead letter.

#### 5.6.4 Observability: one-way navigation, no failure drill-down

The trace payload has **no `workflow_run_id`, `workflow_id`, `agent_id`, or
`evaluation_id`** — the concept does not exist in the API. Exactly one link
connects the two worlds (`WorkflowRunTracePanel` → `/observability?trace=…`),
and **nothing on the Observability page links back**. So trace → run → workflow
is not bidirectional; it is run → trace, terminal.

Inside a trace, `TraceSpanTree` initialises `expanded = {}` — every span starts
collapsed, with **no auto-expand to the first error, no "jump to failure", no
error count on the header, and no errors-only filter**. On a deep trace you
hand-expand until you find it. Adding one field to the trace summary creates the
missing "a run failed → the span that failed" path.

Secondary: the free-text search filters **client-side over the 100 fetched rows
only**, with no pagination and no "showing 100 of N" disclosure.

#### 5.6.5 Aria: the execution gap is handled well; two honesty gaps remain

The documented planner gap is largely **fixed in code**: `HeuristicPlanner`
still emits `inputs: {}`, but the executor raises a typed `kind="input"`
interaction carrying `input_schema`/`missing`/`current_inputs`, and the UI
renders a proper JSON-Schema-driven form with enum selects, required markers,
and per-field parse errors, headed **"Aria needs information"**. So the UI does
**not** let a user believe artifacts appeared from nowhere.

Two gaps remain: **no forewarning at plan time** — a draft plan with 5 steps and
12 empty inputs looks identical to a fully-specified one, and "Approve & run"
offers no *"this will stop 5 times to ask for 12 values"* preflight, while the
panel banner oversells it as *"describe a goal and Aria drafts a plan you
approve & run here"*; and **publish is silent about destination** —
`showToast.success("Published!")` names no artifact, no type, and no link.

`/aria/plans` also has **no URL state** — plan detail is `useState`, so a plan
cannot be linked or bookmarked, browser-back exits the feature entirely, and the
sidebar's "N plans awaiting your input" badge navigates to the **list**, not to
the plan that is waiting. The page then hand-rolls fake breadcrumbs to
compensate for having no real hierarchy.

**A live role bug:** `AccessBadge` tests `scopes.includes("admin")` /
`("operator")` while the backend emits `caliber.admin` / `caliber.operator` — so
**the assistant panel's access badge reads "Viewer" for every user, including
admins.**

#### 5.6.6 Operate/Admin: Keep / Improve / Consolidate / Hide / Remove

| Verdict | Item | Justification |
| --- | --- | --- |
| **Keep** | `Cookbooks.tsx` as built | Readiness checks with deep links to the fixing page; an install modal stating exactly what will and won't happen. Best onboarding artifact — it just needs to be reachable |
| **Keep** | Observability's capture loop (thumbs feedback + trace → test set) | Turns a debugging surface into an evaluation feeder |
| **Keep** | `components/aria/planView.tsx` | One presentation layer shared by panel and page; the typed input form is exemplary |
| **Keep** | Releases' error-vs-empty discipline (*"This is a load failure, not an empty release board."*) | Should be house style |
| **Keep** | `useHealthStatus` | An honest indicator with an honest tooltip |
| **Keep** | `AuditLog` 403 handling + filtered-vs-empty distinction | The right way to degrade |
| **Keep** | Administration's write-only credential model | |
| **Improve** | Zero-denominator branches on the Dashboard tiles ("—"/"Not started", not red 0%) | Removes three day-one false alarms |
| **Improve** | Make the actionable numbers clickable (`CompactMetricTile` → `Link`) | Gate-blocked and candidate-ready are the only real signals and neither links |
| **Improve** | Fix `AccessBadge` scope strings | Mislabels every admin as Viewer |
| **Improve** | Key Releases' rationale/waiver state by `candidate_id` | §5.6.3 correctness bug |
| **Improve** | Give `/aria/plans` a `?plan=` URL; point the badge at the waiting plan | |
| **Improve** | Flag unfilled step inputs at plan time; name the artifact on publish | |
| **Improve** | Auto-expand to the first errored span; add an error count + errors-only filter | |
| **Improve** | Replace the three `window.prompt()` governance calls with dialogs that show the object and validate the reason | |
| **Improve** | Adopt `PageHeader` on the 16 hold-outs; populate `crumbs` on the 9 detail routes | |
| **Improve** | Fix Administration's palette; wrap its two bare tables | |
| **Consolidate** | **Merge Settings + Administration into one Admin destination** with tabs: Access · Credentials · Aria · Platform · Services | Closes the `security` gap and removes one top-level destination |
| **Consolidate** | Fold provider keys and `/gateway` together | Same content split across two destinations |
| **Consolidate** | One owner for Aria config (Settings = global default; composer = session override; delete the third) | |
| **Consolidate** | Merge the Dashboard's "Recent Activity" and "Workflow Delivery Lane" | Same `listJobs` response, re-cut |
| **Hide** | Releases' criteria/evidence JSON textareas behind "Edit as JSON" | Default to a structured builder |
| **Hide** | The Recovery console | Break-glass content above the release timeline |
| **Hide** | `assistant_slo` reliability tiles | A platform-team metric as an operator's landing headline — and it reads red 0% on day one |
| **Remove** | Settings → Allure Report | A localStorage bookmark plus `make` commands in a production admin UI |
| **Remove** | The "Completed runs" `MetricBar` | A lifetime counter with failures demoted to a subtitle |
| **Remove** | `MetricBar`'s self-normalizing `max` heuristic | `max(1, verification_pending, jobs_completed)` encodes ratio-to-an-arbitrary-other-metric and misleads at both ends |
| **Remove** | The dead "Awaiting signal" branches; the dangling "Operations group" sentence; `getPlatformCapabilities`; the unused `hideBreadcrumb` prop | Dead code and copy pointing at nothing |

---

## 6. UX Strengths

These are not consolation prizes. Several are better than the equivalent
surfaces in comparable tools, and the recommendations in §9–§13 are designed to
**protect** them.

### 6.1 The evidence and honesty layer is excellent

This is CALIBER's genuine differentiator, and it is implemented with unusual
rigour:

| Strength | Evidence |
| --- | --- |
| The **Evidence panel** discloses `bounded sample` vs `full dataset`, `graded N of M`, dataset + result digests, per-scorer denominators, and tag slices — with an honest caveat that slice weights don't sum to the run total | `EvaluationDetail.tsx:478-576` |
| **Incomplete runs cannot masquerade as clean ones** — amber banner, per-row `Incomplete` pill, per-row error, per-scorer coverage (`valid 7/10 rows`) | `EvaluationDetail.tsx:233-242`, `:327`, `:402-425` |
| **Baseline comparison is controlled**: requires a matching dataset version + scorer suite, and *discloses* target/subject/model differences rather than hiding them; `DeltaChip` refuses to print a misleading `+0pp` | `EvaluationDetail.tsx:111-122`, `:277-293`, `:55-71` |
| The prompt **`Vs. baseline` panel** defines "regression" explicitly ("was passing/partial in baseline, now fails"), diffs per-case output, and falls back to input-text alignment when ids differ | `Prompts.tsx:2342-2472` |
| **AGE fallback is stated, not hidden**: "AGE-backed" vs "Fell back to {mode}" pills, `Served via {mode} fallback`, and "Strict AGE mode prevented fallback…" | `KnowledgeBases.tsx:7286-7305`, `:7451-7473` |
| **Per-chunk score breakdown** with `dense`/`graph_boost`/`age_*` components and matched-entity chips | `KnowledgeBases.tsx:7511-7585` |
| **The MLflow sync badge means parity, and says so** — "not which dataset version is active … labelled accordingly so it is not mistaken for version liveness" | `EvalDatasets.tsx:349-391` |
| **Error-vs-empty discipline**: *"This is a load failure, not an empty release board."*; `AuditLog` distinguishes "no entries recorded" from "no entries match these filters" | `Releases.tsx:386-392`, `AuditLog.tsx:253-258` |
| **The health dot is real** — driven by an actual `/health` poll, with a comment explaining why it refuses to overclaim (a decorative always-green dot taught users to ignore status lights) | `useHealthStatus.ts`, `Sidebar.tsx:418-436` |

### 6.2 Governance done right — where it is done right

- **MCP deny-by-default.** `allowed: false` is the default, and invoke / test /
  calibrate stay **blocked** until an explicit allow + side-effect + approval
  classification is saved. The backend agrees in writing: *"Missing policy is
  deliberately denied."* This is the best governance affordance in the product
  and should be the template for the other three tool doors.
- **OpenAPI's staged pipeline.** *"Importing a spec never creates a runtime tool
  by itself"* — with well-chosen publish preconditions (server URL, auth
  binding, resolved secrets), approval **forced on** for any non-read tool, and
  max-severity ranking across packs.
- **`VersionPanel`** is the only release path that reads the gate verdict and
  requires a typed override reason before overriding a FAIL.
- **`CalibrationApplyReviewDialog`** is the only surface that hard-blocks a
  failed gate, and it shows the score, per-scorer deltas, gate reasons, a
  word-level diff, and a rationale field before applying.
- **Honest destructive labels**: *"allow live preview (fires a real request)"*.
- **Write-only credential handling** in Administration — values are never read
  back and the form clears on success.

### 6.3 Editor craft

- **Per-node validation and execution badges drawn on the canvas itself**, with
  a `data-testid` per node, in both expanded and collapsed variants.
- **`ProblemsPanel` focuses issue → node → field in one click**, by parsing
  `nodes.<id>.<field>` out of the issue path.
- **Debounced autosave with an SPA-unmount flush**, and **optimistic locking**
  with a legible 409 that tells you to reload before editing.
- **Deep links that resolve across workflows** (`?tab=`, `?run=`), plus a
  `/workflow-runs/:id` redirect shim so a bare run id is a valid URL.
- **Situation-specific empty and degraded copy** — distinct messages for empty
  run history, empty logs, and filtered-to-nothing, with graceful fallbacks when
  full-history search or stats fail.

### 6.4 Two exemplary surfaces worth copying wholesale

**`Cookbooks.tsx`** is the best onboarding artifact in the product: per-recipe
readiness checks with **deep links to the settings page that fixes each unmet
prerequisite**, and an install modal that states exactly what will and will not
happen — *"CALIBER will create one paused workflow and one editable draft.
Nothing is published, deployed, or invoked."* Its only defect is that nothing
links to it (§4.6).

**`components/aria/planView.tsx`** is one presentation layer shared by both the
assistant panel and the Plans page, and its typed interaction form builds a real
input UI from a JSON Schema — enum selects, booleans, JSON textareas, required
markers, per-field parse errors — under the heading *"Aria needs information"*.
This is precisely the pattern the raw-JSON textareas elsewhere should adopt.

**Also worth keeping:** Observability's capture loop (thumbs feedback and "Add
to test set" turn a debugging surface into an evaluation feeder); Object Store's
preview modal (8 media kinds plus server-side Office extraction); review-queue
auto-advance, which gives a real queue-worker rhythm; KB's conditional 2s
polling, which is the pattern the rest of the app should adopt; and KB's
genuinely sane build defaults.

---

## 7. UX Gaps, Inconsistencies & Friction

### 7.1 The systemic finding: the platform computes the signal, and the UI drops it

This is the most consequential pattern in the review, and it recurs in every
journey. **CALIBER's backend is markedly more honest and more complete than its
front end.** In **sixteen** verified cases the data a user needs is already
computed, already typed in TypeScript, and often already in the browser — and no
component renders it.

| # | Signal the backend provides | What the UI does | User-visible consequence |
| --- | --- | --- | --- |
| 1 | `ApiErrorBody.errors[{loc,msg,type}]` — typed, carried on `ApiError.body` | **No component reads `.errors`** | Every malformed payload shows the literal string *"request body validation failed"* — no field, no reason, no fix |
| 2 | `capabilities.approval_readiness.blockers` + `settings_path` | Typed in `workflowTypes.ts`, rendered nowhere — the source comment admits it | "Why can't I run this?" is a `title=` tooltip, and the named remedy is unreachable |
| 3 | `approvals_pending`, `jobs_awaiting_approval` — fetched **app-wide on every page load** | Rendered by **zero** production components | Whoever must approve a run has no way to find out (§7.3) |
| 4 | Judge alignment `per_example` (`human_label`, `judge_label`, `agree`, `error`) | Only aggregates + FP/FN rendered | You learn *how many* disagreements exist and can never see *which* |
| 5 | `workspace.last_run` / `baseline_run` — added expressly to avoid a round-trip | Neither field is read | Two redundant fetches, and the header goes stale |
| 6 | KB `last_run_status` — fetched to drive polling | Never displayed | **A failed build renders as "0 chunks · 0 entities"** — identical to an empty success |
| 7 | `next_token` / `is_truncated` on object listings | `BucketTree` reads neither | **Silent truncation above 1000 objects** in the KB source picker |
| 8 | Option `tags` (`recommended`, `fast`, `default`) | Only `name` + `description` rendered | Nothing marks the recommended chunker / embedding / strength |
| 9 | `citation.score` | Only `label` rendered | Citations cannot be ranked |
| 10 | Alignment-import `skipped[].reason` | Count only | *"Imported 0, skipped 7"* with no reason — the likely first-run result |
| 11 | `/gate-verdicts` — full route + client methods | **Zero UI consumers** | Release criteria cannot reference the gate the platform already computed |
| 12 | `execution_backend` on `ToolSchema` | Absent from the UI's `ToolDefinition` | Four tool provenances render as identical rows (§7.5) |
| 13 | `CaliberEvalDatasetFile` — file-backed dataset examples | **Zero UI references** | No bulk import; a 50-row golden set is a 200-click job |
| 14 | Hybrid `lexical` / `rrf` score components | Not rendered | `score 0.031` displayed beside a breakdown reading `dense 0.847` |
| 15 | `server.tool_policies` — on the object at bind time | Not consulted; the binding hardcodes `read` / no-approval | The policy you just configured is discarded, then re-raised as a deploy blocker |
| 16 | Release `evidence_refs` | Never resolved; used only as report hyperlinks | **Weighted scores are arithmetic on numbers the gated party typed** |

Two related gaps are genuine *backend* omissions rather than dropped signals,
and both are one field wide: the trace payload carries **no
`workflow_run_id`/`agent_id`/`evaluation_id`** (so trace navigation is one-way),
and `EvalRunResultRow` carries **no trace reference** (so a failing scorecard row
cannot be captured back).

**Why this matters more than any individual bug:** the fastest path to a
materially better product is *not* new features. Most of §13's high-value work
is rendering data the platform already produces.

### 7.2 Four loops that do not close

CALIBER is architecturally a set of feedback loops. Four of the most important
are open — and in three cases the missing piece is item 1, 4, or 13 above.

```mermaid
flowchart LR
  subgraph L1["Prompt regression"]
    A1["build test set"] --> A2["Save to Test Sets"] --> A3["Runs → Run tests"]
    A3 -.->|"dataset was never pinned;<br/>error tells you to do<br/>what you just did"| A1
  end
  subgraph L2["Evaluation evidence"]
    B1["scorecard"] -.->|"no row actions,<br/>no trace ref"| B2["capture failing row"]
  end
  subgraph L3["Judge trust"]
    C1["κ computed"] -.->|"per_example dropped"| C2["find disagreements"]
    C2 -.->|"judge not editable"| C3["fix instructions"]
  end
  subgraph L4["KB calibration"]
    D1["test set"] -.->|"input.question contract<br/>documented only in a docstring"| D2["calibrate"]
  end

  classDef broken fill:#fee2e2,stroke:#dc2626,color:#991b1b
  class A1,B2,C2,C3,D2 broken
```

### 7.3 Nothing tells a human that something needs them

**The entire product has exactly one attention badge**: paused Aria plans,
polled every 30s. `badgeKey` is typed as the single literal `"plans"`, so no
other badge is even *expressible* without a type change.

| Attention item | Surfaced globally? | Where it actually lives |
| --- | --- | --- |
| **Pending run approvals** | **No — and no UI exists at all** | nowhere |
| Jobs awaiting approval | **No** | nowhere (a `StatusBadge` label only) |
| Review-queue items pending | No | visit `/review-queues` |
| Failed runs | No | a subtitle string on an **unclickable** Dashboard row |
| Gate verdicts | **No UI at all** | nowhere |
| Gate-blocked / candidate-ready calibrations | No | Dashboard numbers with no links |
| Blocked release candidates, incomplete ops, dead letters | No | visit `/releases` (and only with `canOperate`) |
| Unhealthy backing services | No | Settings → Services |
| Simulated providers | **Yes** — global banner | every route |
| API/DB down | **Yes** — health dot ×2 | TopBar + sidebar |

**Both global signals are *system* health. Every *human* attention item requires
you to remember the page and go look.** Approvals are the sharpest case: the
count is computed server-side, on the wire, typed, and in the browser on every
page load — and rendered nowhere.

The one badge that exists points at the wrong thing: it navigates to the Plans
**list**, not to the plan that is waiting, because plan detail has no URL.

### 7.4 Permissions: three models, one of them correct, ~80% ungated

Documentation recognises six audiences; auth recognises four scopes; the UI
gates on `me.is_admin` (8 pages), scope strings (2 pages), and project
`access_role` (1 page). Only the scope model matches the backend's enforcement
primitive — and because `ADMIN ⊃ OPERATOR`, `is_admin` **cannot express the
OPERATOR tier that gates most write endpoints.**

- **VIEWER sees ~33 mutating controls that will 403**; OPERATOR sees 8.
- `OpenApiIntegrations.tsx` has **zero** permission checks across 11 mutations.
- `CalibrationPanel` has **no permission prop** and is instantiated 4×.
- The sidebar has **no scope logic at all**, so a viewer sees Administration and
  Audit Log and learns their permissions by receiving a 403. (`AuditLog` handles
  this gracefully with a dedicated panel; `Administration` degrades to a raw red
  error line.)
- **`AccessBadge` compares the wrong strings** (`"admin"` vs `caliber.admin`), so
  the assistant panel labels **every user "Viewer", including admins.**
- One **inverse** bug: skill package import is admin-gated but needs only
  OPERATOR, hiding a control from users entitled to use it.
- **Two sibling surfaces disagree about who may create**: tools require ADMIN,
  skills require OPERATOR — and OPERATOR can create a skill but **cannot edit
  it**, breaking the primary authoring action for the primary authoring role.

### 7.5 Vocabulary collisions

**One concept, many names:**

| Concept | Names visible in the UI | Count |
| --- | --- | --- |
| Evaluation dataset | Test Sets · Eval Dataset · Existing Dataset · `eval-datasets` (URL) · `dataset_id` (API) · *"eval dataset 'ED-…'"* (errors) | **6** |
| Grader | Judges · Graders · Custom LLM judges · scorers · LLM judge (an unrelated boolean) | **5** |
| Ship it | Save & promote · Make live · Apply candidate live · Promote · Publish | **5** |
| Optimizer | Calibration Strategy · `optimizer_type` | 2 |
| Object store | Object Store · Workspaces · "file manager" | 3 |

**One name, several things:** *Test Sets* is a prompt-workspace stage **and** a
top-level page; *Runs* is prompt test runs **and** calibration jobs, in adjacent
tabs; *Judge* is a hardcoded frontend string **and** a registry entity;
*baseline* is a pinned test run **and** a pre-optimization score; *Publish* means
draft→published, expose-as-HTTP-service, **and** (in leftover copy)
promote-to-prod; *Calibrate* means measure, propose-a-mutation, or
hyperparameter-search depending on the page; and *Agents* is a nav destination
that is **not** where you author agents, while the thing that executes is an
`agent` node inside a workflow.

**Implementation details surfaced as labels:** `agent_id` — documented as *"an
implementation detail, never something an operator manages directly"* — is shown
on every prompt card and populates a picker labelled "Select a prompt". And
`SINGLE_ENVIRONMENT` mode instructs *"never surface [the alias] to users"*, yet
three no-op alias dropdowns remain visible, each offering the single value
`@prod`.

### 7.6 Long-running work loses its status the moment you look away

| Surface | Progress visible? | Survives a tab switch / reload? |
| --- | --- | --- |
| Tool calibration jobs | **Yes** — 2s conditional poll | Yes (durable) |
| KB builds | **Yes** — 2s poll + 7-step stepper | Yes |
| Prompt calibration | Yes, 2s — **only while the tab is mounted** | **No** — the poll dies on unmount, and **no queued-job indicator exists anywhere** |
| Prompt test runs | No — a manual Refresh link | n/a |
| **Evaluation runs** | **No** — the button text changes to "Running…" | **No — closing the tab destroys the run.** Nothing is persisted until scoring completes, and the client sets no write timeout |
| Workflow runs | Yes — SSE + 2s telemetry | Yes |

The product already contains the correct pattern three times; the two surfaces
that most need it don't use it.

### 7.7 Wayfinding

Across **22 nav destinations plus 9 unlinked detail routes**:

- **No command palette, no global search, no cross-object search.** No `cmdk` or
  `fuse` dependency exists. 11 of 22 destinations have no search at all.
- **No global keyboard shortcuts** — six page-local `keydown` handlers form a
  per-page dialect, and the editor's `Cmd+Z/Y/A/D` is undiscoverable (there is
  no `?` overlay).
- **Breadcrumbs are built and unused.** `PageHeader.crumbs` supports a real
  trail; **one** page passes it. All nine detail routes — including the
  three-level-deep editor — claim you came from the Dashboard.
- **`PageHeader` is used by 18 of 34 pages**, against a docstring saying "every
  top-level page". Three pages hand-roll chevron trails instead, and four
  duplicate a local `Chevron()`.
- **`EmptyState` is a local function inside a 4,788-line page** — the
  highest-leverage moment for a new user is the least standardised surface.
- **No recently-viewed.** 34 `caliber.*` localStorage keys exist; none records
  navigation.

**Net: the sidebar is effectively the only way to get anywhere, and once you are
three levels deep the shell gives you nothing to climb back with.**

### 7.8 URL state is missing where work happens

| Surface | Has a URL? | Consequence |
| --- | --- | --- |
| `ToolWizard` / `SkillWizard` (2,391 lines) | **No** | a refresh or tab switch discards in-progress authoring, with no draft persistence |
| Aria plan detail | **No** | plans can't be linked; back exits the feature; the badge can't target the waiting plan |
| Review-queue detail | **No** | a queue has no permalink; back exits the feature |
| Workflow editor view state (mode, selected node, run monitor) | **No** | unshareable, lost on reload |
| Workflow detail (`?tab=`, `?run=`) | **Yes** | the pattern the others should copy |
| Observability (`?trace=`, filters) | **Yes** | |

### 7.9 The product does not yet meet its own design principle #9

ARCHITECTURE.md commits to *"**Developer & User Friendly** — provide a clear,
productive experience for both developers **and business users**"*, discharged
by *"the SPA surfaces and the Cookbook examples"*. Measured against that:

- The **business user** (the `system-user` and `decision-maker` audiences, 23
  docs between them) is not served by the SPA today. Their most likely
  destinations — Review Queues, Releases, Plans — are respectively a form that
  shows no evidence, a raw-JSON compliance surface with self-attested scores,
  and a page with no URLs.
- The **Cookbook examples**, named as half the mechanism, are **linked from
  nowhere but the sidebar**.
- Principle **#4 "Evaluation by Design"** is contradicted by the most-used
  scoring surface in the product bypassing the evaluation engine, the judge
  registry, and the evidence contract in favour of a judge prompt hardcoded in
  the browser bundle.

This is not a rhetorical flourish — it is the clearest available statement of
the product's own intent, and it names exactly which personas the current UI
under-serves.

---

## 8. Feature Creep & UI Bloat

### 8.1 Weight is concentrated in four destinations

Four of 34 pages hold **27,260 of 57,373 lines (47.5%)**, and each is a single
nav destination. The hypothesis that page size indicates a destination hosting
too many jobs holds in every case:

| Page | Lines | Distinct feature areas | Verdict |
| --- | --- | --- | --- |
| `KnowledgeBases.tsx` | 9,006 | **~55** (≈20 core, ≈33 expert-only, 1 dead) · 66 `useState` | Must be split |
| `Prompts.tsx` | 8,192 | **16** | Must be split |
| `WorkflowEditor.tsx` | 5,274 | 12 toolbar controls · 8 overlays · 46 `useState` | Extract run monitoring |
| `WorkflowDetail.tsx` | 4,788 | 25 queries + 22 mutations · **17 always-expanded panels** in the selected-run view | Extract run monitoring |
| *(`Inspector.tsx`, a component)* | *6,611* | *~45 `<Section>` panels in a 300px rail* | Capability-gate sections |

`Prompts.tsx` grew **460 lines during this review**. The largest pages are still
accreting features.

### 8.2 Duplication census

| Duplicated thing | Copies | Where |
| --- | --- | --- |
| Workflow run-action helper functions | **19, verbatim** | `WorkflowDetail.tsx` ↔ `WorkflowEditor.tsx` (~600 lines) |
| Run-monitoring panel stack | **2 full mounts** | Runs tab ↔ editor Run Monitor overlay |
| KB entity/relationship renderers | **4** | Chunks card, focus card, Graph lists, canvas trails |
| Query playground (ask box + result card) | **2** | Explore ▸ Query ↔ "Graph Retrieval Probe" (~410 lines, already drifted) |
| Graph preset pickers | **4 renderers, 2 families** | build pills, build cards, explorer, playground |
| Graph retrieval control clusters | **3** | build default / explorer / query override (~600 lines) |
| `CalibrationPanel` instances | **3 in tools alone** | Fixtures, Hardening, orphaned detail — two labelled identically, persisting differently |
| Prompt run surfaces / run histories | 2 / 2 | Test Sets ↔ Runs, neither aware of the other |
| Prompt diff renderers | **3** | Versions modal, Apply dialog, unused `PromptDiff.tsx` |
| Prompt promote paths | **5** (only 2 gate-aware) | §5.1.5 |
| Object-store browsers | 2 | `BucketTree` ↔ `ObjectStore` table |
| `humanSize` implementations | **3** | KB, ObjectStore, BucketTree |
| Local `Chevron()` components | **4** | the four pages that skip `PageHeader` |
| Versions lists on one tab | 2 | `VersionPanel` + a second table, four disjoint verbs |
| Detail pages duplicating workspaces | **2,306 lines** | `ToolDetail` + `SkillDetail`, zero inbound links |
| Parallel 6-stage workspaces | **3 vocabularies** | Prompts / Skills / Tools |
| "Calibrate" mechanisms | **7 endpoints, 3 semantics** | §5.5.2 |
| Aria config writers | 3 | Settings tab, panel gear, composer |

### 8.3 Dead, unreachable, and lying UI

| Item | Status |
| --- | --- |
| `WorkflowVersionDetail` route | **Unreachable** — and holds the only Compile/Export affordance |
| `ToolDetail` + `SkillDetail` (2,306 lines) | **Unreachable** — hold the only durable calibration and the only skill packaging |
| `ToolWizard` step 4 of 5 | **Structurally unreachable** — `registeredToolId` is always null while mounted |
| `PromptCalibrationTab` (~150 lines) | **Dead** — and contains the numbered stepper the page needs |
| Object-store launch machinery (~50 lines + a test) | **Dead** — `BucketTree`'s docstring says the launchers were removed |
| Deployments + Promotions tab bodies (~180 lines) | **Dead** under `SINGLE_ENVIRONMENT` + `GATED_ALIASES = frozenset()` |
| `runMut` non-queue branch | **Unreachable** behind the disable |
| `StatusPill` neutral branch | **Unreachable** while eval runs are synchronous |
| Dashboard "Awaiting signal" fallbacks | **Unreachable** — `assistant_slo` is non-optional, so day-one tiles read red `0%` |
| `skipped` review status | Rendered + typed, **no UI can set it** |
| `hideBreadcrumb` prop · `getPlatformCapabilities` | **Zero callers** |
| Publish drawer step 2 | Permanently empty (`changeSummary={[]}`) — a mandatory click on nothing |
| "Open Prompt Calibration after save" checkbox | **Does nothing** — the arg is dropped at the call site; it only relabels the button |
| "Run Pipeline" button | Runs nothing (`setTab("runs")`) — styled as the page's primary action |
| "Save as New Version" button | **Promotes to live** without saying so |
| Object Store upload-cancel | A `span` with no handler |
| Copy: *"Create a prompt on the Create Prompt tab"* | **No such tab exists** (3 occurrences) |
| Copy: *"configured under the Operations group"* | **No such group in the UI** |
| Tag column + filter on Test Sets | Permanently empty — the create form has no `tags` field |

### 8.4 Expert controls presented as primary

- **Assistant-Guided Calibration is the *first* thing in the prompt Calibration
  tab**, above the actual run form — a free-text box, three buttons, and result
  cards exposing `plan_id`, `operation_id`, `trace_id`, `correlation_id`, and
  `missing_slots`. A debugging console for CALIBER's own intent pipeline,
  positioned as the primary CTA.
- **The KB Graph sub-view is a peer tab**, so a dense-only knowledge base
  presents 1,385 lines of AGE machinery — including a 17-row diagnostics card and
  a second query playground — for a graph that does not exist.
- **A "Pipeline blueprint" panel restates the collapsed advanced config above
  it**, asking for a graph posture before explaining what one is.
- **The workflow Calibrate panel** (epsilon, candidate budget, deploy-gate
  dataset) is bolted onto the page header.
- **Raw gate numerics as free-text inputs** with no units or validation;
  **per-scorer JSON config textareas**; a **16-chip entity-type multi-select**
  whose default is "all"; a **12-badge graph metadata row**; and a **spinner +
  stepper + 7 preset chips + hint sentence for one integer 1–50**.
- **`assistant_slo` reliability tiles** — a platform-team metric as the
  operator's landing headline.
- **Settings ships `cd caliber && make test-allure`** in a production admin UI.

---

## 9. Simplify / Consolidate / Remove Opportunities

Master list, applying the decision order. Per-journey detail is in §5; this is
the consolidated view with estimated code movement.

### 9.1 Simplify (same capability, fewer decisions)

| # | Action | Effect |
| --- | --- | --- |
| S1 | Say *"defaults are fine — pick files and go"* on the KB build form; move the blocking embedding error out of the collapsed disclosure; propagate the server's `recommended` tags | Turns a 26-control-group form back into the 2-decision form it already is |
| S2 | Show stage order (numbers + completion marks) on the three 6-stage workspaces — **the component already ships, unused** | Makes an implicit pipeline explicit at zero design cost |
| S3 | Replace free-text identifier fields with pickers: eval `subject_ref`, release artifact/version/rollback, MCP `mcp:server/tool`, skill `node_id`, tool `secret_refs` | Removes the "go find an id in another tab" pattern product-wide |
| S4 | Replace raw-JSON textareas (release criteria/evidence, dataset examples) with structured builders, keeping "Edit as JSON" behind a toggle — **`planView.tsx` already demonstrates the pattern** | Removes ~10 hand-authored JSON blobs from the first-evaluation journey |
| S5 | Hide the three no-op `@prod` alias dropdowns under `!SINGLE_ENVIRONMENT` | Honours the codebase's own stated rule |
| S6 | Reduce the test-case-count control to one number input | −1 stepper, −7 chips, −1 hint sentence |

### 9.2 Consolidate

| # | Action | Code movement |
| --- | --- | --- |
| C1 | **Merge Settings + Administration** into one Admin destination (Access · Credentials · Aria · Platform · Services); fold `/gateway` in beside provider keys | −1 top-level destination; closes the missing `security` group |
| C2 | **One "Tools I can call" catalog** (registry ∪ MCP discovered), filterable by source / side-effect / approval; keep `/mcp-servers` and `/openapi-integrations` as *connection* management | Collapses 4 mental models into 1 view; largely a query change once provenance exists |
| C3 | **Extract one run-monitoring component**, mounted twice at different densities; move the 19 duplicated helpers to `lib/workflowRunActions.ts` | ~1,300 lines de-duplicated across the two largest workflow files |
| C4 | **Delete the two orphaned detail pages**, migrating durable calibration into Hardening and skill packaging into a workspace stage | **−2,306 lines**; two broken journeys fixed |
| C5 | **Split `KnowledgeBases.tsx`** into `pages/knowledge/*` (the directory exists with one sibling); extract `<GraphRetrievalControls>`, `<GraphPresetPicker>`, `<KnowledgeQueryResultCard>`, `<KnowledgeBaseListItem variant>` | ~1,900 lines de-duplicated; a 9,006-line route broken up |
| C6 | **One scoring engine** — retire the in-browser prompt judge for `POST /evaluations` with `predict_target: "prompt"`; make the workspace Test Sets stage an embedded view of the real dataset | Removes a fork of the evaluation engine; makes prompt scores comparable and auditable |
| C7 | **Collapse the prompt page's duplicates**: 2 run surfaces → 1, 2 run histories → 1, 3 diff renderers → 1, 5 promote paths → 2, dataset authoring → `/eval-datasets` | ~2,000 lines simplified; governance becomes path-independent |
| C8 | **Split the "Calibrate" verb** — *Calibrate* = measure, *Optimize* = propose a mutation for approval | Removes the most dangerous ambiguity in the product |
| C9 | **Unify the three workspace vocabularies** and align the lifecycle ladders; stop awarding a tool's top rung to an unbound tool | One learnable pattern instead of three |
| C10 | **Merge Cookbooks into the New-Workflow gallery** as "Curated examples", carrying readiness checks over to templates | −1 destination; makes the best onboarding artifact reachable |
| C11 | **Collapse Plan + Copilot** into one "Describe" surface (their docstrings say they differ only by whether the graph is empty) | −1 overlay, −1 concept |
| C12 | **Move Review Queues into Evaluate**; adopt `PageHeader` + `crumbs` everywhere; promote `EmptyState` to a shared component | Coherent grouping; −4 duplicate `Chevron()`s |
| C13 | **Collapse Fixtures + Hardening** into one tool stage | Removes two identically-labelled buttons with different durability |
| C14 | One owner for Aria config; merge the Dashboard's two job feeds | −2 duplicate surfaces |

### 9.3 Improve defaults

| # | Action |
| --- | --- |
| D1 | Couple side-effect → approval in the native tool wizard, **client and server** |
| D2 | Seed MCP workflow bindings from saved `tool_policies`; add `side_effect_level` to registry bindings |
| D3 | Default KB calibration `retrieval_mode` to the version's saved default, and warn on divergence |
| D4 | Persist an evaluation run as `running` **before** scoring; poll to completion; navigate to the created run |
| D5 | Zero-denominator branches on Dashboard tiles ("—", not red `0%`) |
| D6 | Gate KB graph panels on `graph_hybrid`/`age_graph`, not `!== "dense"` |

### 9.4 Progressive disclosure

Hide behind *Advanced* (each already has server defaults or is expert-only):
Assistant-Guided Calibration · per-scorer weights + JSON config · gate
thresholds · the KB Graph sub-view (gate on `entityCount > 0`) · the 17-row AGE
diagnostics card · the 16-chip entity-type selector · the 12-badge metadata row ·
the workflow Calibrate panel · resume-by-event correlation · Files & Artifact
Lineage · OpenAPI Dependencies + Graph tabs · raw-JSON schema toggles ·
Releases' Recovery console and JSON textareas · `assistant_slo` tiles · Test Sets
version controls and MLflow sync · `predict_target: workflow` (with a cost
warning) · Inspector sections gated by capability.

### 9.5 Remove

The full inventory is §8.3. Highest-value removals, **all with zero
user-visible loss**:

`PromptCalibrationTab` · the object-store launch machinery · the second KB query
playground + compact result card · the legacy `KnowledgeTab` shim · the
"Pipeline blueprint" panel · the Chunks-tab lineage card · `ToolWizard` step 4 ·
the Fixtures-stage Calibrate button · the Fixtures/Hardening split · publish
drawer step 2 · the non-functional calibration checkbox · Deployments +
Promotions bodies · `runMut`'s dead branch · legacy palette components ·
`Delete all (N)` in the browse toolbar · the in-modal prompt switcher · Settings
→ Allure · the "Completed runs" `MetricBar` and its self-normalizing `max` · the
dead "Awaiting signal" branches · `hideBreadcrumb` · `getPlatformCapabilities` ·
the empty Tag column · the `non_empty` default chip · duplicate
`/prompts/optimization/*` aliases · the global `updateAssistantConfig` side
effect · Object Store's fake cancel and its orphaned "project files" button ·
the three stale copy strings.

---

## 10. Missing Capabilities That Materially Improve UX

Every item clears a high bar: **either the data already exists and is being
dropped, or its absence is itself a load-bearing friction identified in §7.**
Nothing is proposed for completeness.

| # | Capability | Why it is justified | Cost |
| --- | --- | --- | --- |
| **A1** | **A "Needs your attention" surface** — the top block of Home, backed by generalised sidebar badges | §7.3: the product has no way to tell a human something needs them. `approvals_pending` and `jobs_awaiting_approval` are **already in the browser on every page load** and rendered nowhere | S–M |
| **A2** | **An Approvals destination** | `CaliberApprovalRequest` has **no UI whatsoever**, and workflow `waiting_approval` runs are undiscoverable without a URL (§5.2.4). This is a governance product; the approver persona has no home | M |
| **A3** | **A first-run experience on `/`** — on an empty deployment, replace the metric grid with "Install a Cookbook → Add a provider key → Run it" | §4.6: a fresh install opens on three false alarms, and the one good on-ramp is unlinked | S |
| **A4** | **Command palette (`Cmd+K`)** over destinations plus objects by name | §7.7: 31 addressable routes, no global search, no recent history, no shortcuts. The highest-leverage wayfinding addition | M |
| **A5** | **Real breadcrumbs on the 9 detail routes** | §7.7: the capability already exists and is used once. One line per page | **XS** |
| **A6** | **`workflow_run_id`/`agent_id`/`evaluation_id` on the trace payload**, plus reciprocal links | §5.6.4: one field turns one-way run→trace into bidirectional navigation and creates the missing "a run failed → the span that failed" path | S |
| **A7** | **Row actions on the scorecard** (+ a trace ref on the row): "Add to test set", "Send to review queue", "Show judge rationale" | §7.2: closes evaluation loop A, which cannot close without the reference | S–M |
| **A8** | **Render `per_example` and make judges editable** | §7.2: closes the judge-trust loop. Both the data and the PATCH fields already exist | S |
| **A9** | **Bulk / file import of dataset examples** | §5.3.6: 4 interactions per row; `CaliberEvalDatasetFile` already models this with zero UI references | M |
| **A10** | **Evidence-derived release criterion scores** (resolve `evidence_refs`; consume `/gate-verdicts`) — and until then, state plainly that scores are self-attested | §5.6.3: an audited compliance surface currently computes weighted scores from numbers the gated party typed | M–L |
| **A11** | **A `status` filter on the runs endpoint** | §5.2.4: `waiting_approval` and `failed` are unfindable; the API cannot even express the question | S |
| **A12** | **Draft persistence + URLs for the two wizards** | §7.8: 2,391 lines of authoring that a refresh discards | S–M |
| **A13** | **A `PATCH` on release candidates** (or "clone with edits") | §5.6.3: a candidate created with the shipped defaults is permanently unsignable | S |
| **A14** | **Scope-aware navigation** | §7.4: users currently discover their permissions by receiving 403s | S |

---

## 11. Proposed Target UX & Navigation Model

### 11.1 Principles for the target model

1. **Group by the job, not the object family.** Review Queues is evaluation
   input, not an ops chore; Object Store is infrastructure, not an authored
   asset.
2. **One destination per job.** Two admin pages, two tool-catalog mental models,
   and two galleries each force a guess before work starts.
3. **Connecting a system and using a capability are different jobs.** Keep
   `/mcp-servers` and `/openapi-integrations` for *connecting*; make `/tools` the
   single answer to *"what can I call?"*.
4. **Attention is a first-class surface**, not a property of pages you remember
   to visit.
5. **Expert depth lives behind disclosure**, not adjacent to first-run controls.

### 11.2 Target navigation

```mermaid
flowchart TD
  ROOT["CALIBER"] --> HOME["<b>Home</b><br/>Needs your attention<br/>+ first-run start"]
  ROOT --> BUILD["<b>Build</b>"]
  ROOT --> CONNECT["<b>Connect</b>"]
  ROOT --> MEASURE["<b>Measure</b>"]
  ROOT --> GOVERN["<b>Govern</b>"]
  ROOT --> OPERATE["<b>Operate</b>"]
  ROOT --> ADMIN["<b>Admin</b><br/><i>one destination, 5 tabs</i>"]
  ROOT --> DOCS["Docs ↗"]

  BUILD --> W["Workflows<br/><i>+ Curated examples gallery</i>"]
  BUILD --> PR["Prompts"]
  BUILD --> SK["Skills"]
  BUILD --> TL["Tools<br/><i>unified callable catalog</i>"]
  BUILD --> KB["Knowledge Bases"]
  BUILD --> PL["Plans"]

  CONNECT --> MCP["MCP Servers"]
  CONNECT --> API["OpenAPI Integrations"]
  CONNECT --> GW["LLM Gateway"]
  CONNECT --> OS["Object Store"]

  MEASURE --> TS["Test Sets"]
  MEASURE --> JU["Judges"]
  MEASURE --> EV["Evaluations"]
  MEASURE --> RQ["Review Queues<br/><i>moved from Operate</i>"]

  GOVERN --> AP["Approvals<br/><i>NEW</i>"]
  GOVERN --> RL["Releases"]
  GOVERN --> AU["Audit Log"]

  OPERATE --> OB["Observability"]

  classDef new fill:#dcfce7,stroke:#16a34a,color:#166534
  classDef moved fill:#dbeafe,stroke:#2563eb,color:#1e40af
  class AP,HOME new
  class RQ,OS,TL,W moved
```

### 11.3 What changed and why

| Change | Rationale |
| --- | --- |
| **Home** replaces a metric Dashboard | §7.3 — attention has no surface; §4.6 — first run is hostile |
| **+ Approvals** | §10-A2 — the approver persona has no home, and the data is already on the wire |
| **Cookbooks folded** into the Workflows gallery | §6.4 — the best on-ramp is unlinked; two galleries, one destination |
| **Tools becomes a unified catalog** | §7.5 — four provenances render identically; one field unlocks it |
| **Object Store → Connect**, name unchanged | §5.4.6 — it is infrastructure. Renaming is *not* recommended: the name is defended by a docs series and a distinct `routes/files.py` concept. The right fix is to build or delete the orphaned "project files" button, not to rename the page |
| **Review Queues → Measure** | §5.3.6 — human labels are evaluation input; the two halves of the judge-trust loop sat in different nav groups |
| **Settings + Administration → one Admin** (Access · Credentials · Aria · Platform · Services) | §5.6.2 — the split is unpredictable, 4 of 5 Settings tabs are read-only, and 9 of 10 backend setting groups are invisible |
| **LLM Gateway beside provider credentials** | §5.6.2 — keys and the routing for those keys were split across destinations |
| **`/agents` leaves Build** → Admin, renamed *Refinement Fleet* | §5.2.5 — by its own banner it is an optimizer-fleet record editor, and the current name actively misleads |
| **Plans stays**, and gains a URL | §5.6.5 — a real work surface that simply isn't addressable |
| Group order follows the lifecycle | Build → Connect → Measure → Govern → Operate mirrors the sequence the product implements |

**Net: 21 in-app destinations → 19** (−Cookbooks, −Settings, −Agents from the
main nav, +Approvals). The significant wins are in *consolidation* rather than
count: one admin destination, one tool catalog, one scoring engine, one
run-monitoring component, and ~7,000 lines of duplicated or dead UI removed.

---

## 12. Current vs. Target Journey Diagrams

### 12.1 Prompt regression — the named journey

```mermaid
flowchart TB
  subgraph CUR["CURRENT · 16 interactions · loop dead-ends"]
    direction TB
    c1["New prompt → 4 on-ramps"] --> c2["Create<br/><i>silently promotes live</i>"]
    c2 --> c3["Test Sets: generate + run<br/><i>10 serial LLM calls, no cancel</i>"]
    c3 --> c4["Save to Test Sets"]
    c4 -.->|"dataset NOT pinned"| c5["Runs: 'Build one on the<br/>Test Sets tab first'"]
    c5 -.->|"instruction does not work"| c3
    c3 --> c6["Set baseline"] --> c7["Author: edit"] --> c8["Save &amp; promote<br/><i>bypasses the gate</i>"] --> c9["Runs: re-run"] --> c10["Vs. baseline"]
    c10 -.->|"scored by an unregistered<br/>in-browser judge"| cX{{"not comparable to<br/>/evaluations"}}
  end

  subgraph TGT["TARGET · ~10 interactions · loop closes"]
    direction TB
    t1["New prompt → one form<br/><i>advanced on-ramps disclosed</i>"] --> t2["Create as draft"]
    t2 --> t3["Test Sets: author cases<br/><i>authoring only</i>"]
    t3 --> t4["Runs: pick/pin dataset,<br/>run via POST /evaluations"]
    t4 --> t5["Set baseline"] --> t6["Author: edit"] --> t7["Promote<br/><i>one gate-aware path</i>"] --> t8["Runs: re-run"] --> t9["Vs. baseline<br/><i>registry judge + evidence panel</i>"]
    t9 --> t10["row action →<br/>capture failure back"] --> t3
  end

  classDef bad fill:#fee2e2,stroke:#dc2626,color:#991b1b
  classDef good fill:#dcfce7,stroke:#16a34a,color:#166534
  class c5,cX bad
  class t9,t10 good
```

### 12.2 Debug a failed run

```mermaid
flowchart TB
  subgraph CUR2["CURRENT · 14–18 interactions · 3 pages · data-loss trap"]
    direction TB
    d1["run fails"] --> d2["Runs tab:<br/>17 stacked panels"]
    d2 --> d3["'Open in Editor'<br/><i>the obvious button</i>"]
    d3 --> dX{{"opens the PUBLISHED version:<br/>edits apply, chip says 'Unsaved',<br/>Save disabled with no reason,<br/>work lost on navigate"}}
    d2 --> d4["<i>correct path:</i> Versions →<br/>Restore as draft"] --> d5["re-find the node by hand<br/><i>no ?node= param</i>"]
    d5 --> d6["edit → Validate → Publish<br/><i>3 steps, one always empty</i>"] --> d7["Versions → Promote"] --> d8["Runs → Queue Run"]
  end

  subgraph TGT2["TARGET · 6–8 interactions · 1 page"]
    direction TB
    e1["run fails"] --> e2["Run workspace:<br/>failure + error_summary first,<br/>diagnostics disclosed"]
    e2 --> e3["'Fix this node' →<br/>editor at ?node=&amp;field=<br/><i>auto-restores a draft</i>"]
    e3 --> e4["edit → Validate → Publish"] --> e5["Promote"] --> e6["Retry the failed run"]
  end

  classDef bad fill:#fee2e2,stroke:#dc2626,color:#991b1b
  class dX bad
```

### 12.3 First run

```mermaid
flowchart LR
  subgraph C3["CURRENT"]
    x1(["fresh install"]) --> x2["Dashboard:<br/>0% RED · 0% RED · 0% AMBER"]
    x2 --> x3["4 tiles: Prompts · Tools ·<br/>Skills · Workflows"]
    x3 --> x4{{"no guidance; Cookbooks unlinked;<br/>every empty state is<br/>a terminal statement"}}
  end
  subgraph T3["TARGET"]
    y1(["fresh install"]) --> y2["Home detects an<br/>empty deployment"]
    y2 --> y3["1 · Install a Cookbook<br/>2 · Add a provider key<br/>3 · Run it"]
    y3 --> y4(["a working, traced,<br/>scored example"])
  end
  classDef bad fill:#fee2e2,stroke:#dc2626,color:#991b1b
  classDef good fill:#dcfce7,stroke:#16a34a,color:#166534
  class x4 bad
  class y4 good
```

### 12.4 Judge trust

```mermaid
flowchart LR
  subgraph JC["CURRENT"]
    j1["author judge"] --> j2["enqueue traces"] --> j3["human grades<br/><b>a trace id</b>"]
    j3 --> j4["κ = 0.62"]
    j4 -.->|"per_example dropped"| j5{{"which examples?"}}
    j5 -.->|"judge not editable"| j6{{"fix it?"}}
  end
  subgraph JT["TARGET"]
    k1["author judge"] --> k2["enqueue traces"] --> k3["human grades<br/><b>the request + response</b>"]
    k3 --> k4["κ + disagreement table"]
    k4 --> k5["open a disagreement"] --> k6["edit instructions"] --> k4
  end
  classDef bad fill:#fee2e2,stroke:#dc2626,color:#991b1b
  classDef good fill:#dcfce7,stroke:#16a34a,color:#166534
  class j3,j5,j6 bad
  class k3,k4 good
```

---

## 13. Prioritized Recommendations

Priorities are assigned on **user harm**, not implementation cost. *Critical* =
silent data loss, silently wrong output, or a primary journey that dead-ends.
Effort: **XS** ≲1h · **S** ≲1d · **M** ≲1w · **L** >1w.

Nine issues are documented in the full
*Persona → JTBD → Current Experience → Problem → Evidence → User Impact →
Recommendation → Expected Improvement → Priority* form inline at §5.1.2, §5.1.4,
§5.1.5, §5.2.1, §5.3.1, §5.4.4, §5.5.3, §5.6.3, and §4.3.

### 13.1 Summary table

| Priority | Persona | Journey | Problem | Recommendation | User Impact | Effort |
| --- | --- | --- | --- | --- | --- | --- |
| **Critical** | Evaluator, Approver | Judge trust | Review form shows only a trace **id** — a human grades `tr-abc123` | Return the trace request/response on the queue detail route and render it above the questions | Every human label, κ, and gate decision downstream is founded on evidence the reviewer never saw | S |
| **Critical** | Developer | Prompt regression | "Save to Test Sets" never pins the dataset; Runs dead-ends with an instruction that does not work | Pin on save, or give Runs an explicit dataset picker | The product's core loop fails on first use; one lifecycle state is unreachable | S |
| **Critical** | Developer, Operator | Workflow recovery | The primary header button opens an immutable version that still accepts edits; Save is disabled with no reason | Guard `patchManifest`; read-only banner + one-click Restore as draft | Silent loss of uncommitted work, from the most prominent button on the page | S |
| **Critical** | Developer | KB build | "New version" stays enabled in create mode, overwrites picked sources, and versions **a different KB than the header names** | Clear `selectedKnowledgeBaseId` on create; gate the button on `!creatingKnowledgeBase` | Silently writes to the wrong object | XS |
| **Critical** | Approver | Release signoff | `rationale`/`waiver*` are single state values shared across all candidate rows | Key by `candidate_id`, or move signoff into a per-candidate modal | Signing off candidate B submits the rationale typed for A — in an audited compliance surface | XS |
| **Critical** | Developer | Prompt deploy | A button labelled "Save as New Version" also promotes to live | Rename; split save from promote | Unannounced production changes | XS |
| **Critical** | Approver | Workflow approval | `waiting_approval` runs have no queue, no badge, no filter, and no cross-workflow list | Add a `status` filter + an Approvals destination + generalised badges | An approval-gated platform where approvers cannot find what needs approving | M |
| **High** | All | Every journey | Field-level validation errors are dropped; users see *"request body validation failed"* | One shared error component rendering `ApiErrorBody.errors` | Every 4xx in the product becomes actionable | S |
| **High** | Developer, Admin | Tool governance | The native (default) tool door allows an external-action tool with approval off — uncoupled and unvalidated | Couple side-effect → approval, client **and** server | Closes the only unguarded path to an ungoverned write tool | S |
| **High** | Developer | Tool → workflow | Auto-created MCP bindings hardcode `read` / no-approval, discarding saved policy | Seed bindings from `server.tool_policies` | Turns a late, manifest-worded deploy blocker into a correct default | XS |
| **High** | Evaluator | Prompt / evaluation | The most-used scoring surface uses an unregistered in-browser judge; unparseable output scores 0 as `fail` | Route prompt scoring through `POST /evaluations` with `predict_target: "prompt"` | Comparable, auditable scores; ends manufactured false regressions | M |
| **High** | Evaluator | Evaluation | A blocking run with no progress, nothing persisted, and no write timeout | Persist as `running` first, poll, navigate to the run | A closed tab currently destroys up to 50 paid LLM calls | S |
| **High** | Evaluator | Judge trust | κ's `per_example` is computed then dropped; judges cannot be edited | Render `per_example`; add `/judges/:id` using the existing PATCH fields | Makes the alignment number actionable instead of decorative | S |
| **High** | Developer | KB calibration | The required `input.question` / `expected.sources` contract exists only in Python docstrings; the 400 says "has no examples" | Put the contract in the editor's default templates + field hints; make the error count matching rows | A documented feature is unusable while appearing to work | S |
| **High** | All | First run | Two red 0% tiles and an amber 0% on an empty install; Cookbooks unlinked; no onboarding | Zero-denominator branches + a 3-step empty-state start block | Removes three day-one false alarms and connects the best on-ramp | S |
| **High** | Viewer, Operator | All | ~33 mutating controls visible to users who will 403; three competing permission models | Standardise on scope gating; add a permission prop to `CalibrationPanel`; scope-aware nav | Users stop learning their permissions from error dialogs | M |
| **High** | Approver, Operator | Attention | No global surface for approvals, reviews, failures, or gate blocks | "Needs your attention" on Home + generalised sidebar badges | The data is already in the browser on every page load | M |
| **High** | Admin | Aria | `AccessBadge` compares the wrong scope strings | Compare `caliber.*` | Every admin is currently labelled "Viewer" | XS |
| **High** | Architect, Approver | Release signoff | Weighted scores are arithmetic on operator-typed numbers; `evidence_refs` never resolved; `/gate-verdicts` has no consumer | Resolve evidence server-side; until then state that scores are self-attested | An audited governance surface currently self-attests | M–L |
| **Medium** | Developer | Workflow | Compile/Export exists only on a route nothing links to | Fold into the Versions tab | Recovers a real capability and surfaces compile errors properly | S |
| **Medium** | Operator | Workflow | The run-queue blocker names a setting no page can change | Render `approval_readiness.blockers` + `settings_path`; expose the toggle in Admin | Removes an unresolvable dead end | S |
| **Medium** | Developer | KB sources | `BucketTree` ignores `next_token`/`is_truncated` | Paginate + warn | Silent source truncation above 1000 objects | S |
| **Medium** | Operator | Observability | Trace navigation is one-way; no failure drill-down | Add run/agent/eval ids to the trace payload; auto-expand the first error | Creates the missing "a run failed → the span that failed" path | S |
| **Medium** | Developer | KB retrieval | `hybrid` is misrepresented in three places, including contradictory score numbers | Gate graph panels correctly; render `lexical`/`rrf`; fix the summary | Restores trust in the panel built to establish grounding | S |
| **Medium** | All | Wayfinding | Breadcrumbs built and unused; `PageHeader` on 18/34; no palette | Pass `crumbs` on the 9 detail routes; adopt `PageHeader`; add `Cmd+K` | The sidebar stops being the only way to navigate | XS / S / M |
| **Medium** | Developer | Tools, Skills | 2,306 lines of orphaned detail pages hold the only durable calibration and packaging surfaces | Delete, migrating content into the workspaces | Two broken journeys fixed; 2,306 lines removed | M |
| **Medium** | Developer | Authoring | Wizards have no URL and no draft persistence | Route them; persist drafts | 2,391 lines of work stop being refresh-fragile | S–M |
| **Medium** | All | Vocabulary | 6 names for a test set, 5 for a grader, 5 for promote; "Calibrate" means three things | Pick one term each; split Calibrate/Optimize | Removes the product's steepest learning tax | M |
| **Low** | All | Bloat | ~7,000 lines of dead, duplicated, and lying UI (§8.3) | Execute §9.5 | Smaller surface, less drift risk, faster comprehension | M |

### 13.2 Quick wins (XS–S, no design dependency)

1. Clear `selectedKnowledgeBaseId` on KB create; gate "New version". **(XS, Critical)**
2. Key Releases' rationale/waiver state by candidate id. **(XS, Critical)**
3. Rename "Save as New Version". **(XS, Critical)**
4. Seed MCP bindings from saved policy. **(XS, High)**
5. Fix `AccessBadge` scope strings. **(XS, High)**
6. Pass `crumbs` on the nine detail routes. **(XS, Medium)**
7. Guard `patchManifest` + add a read-only banner. **(S, Critical)**
8. Pin the dataset on save. **(S, Critical)**
9. Return and render the trace in the review form. **(S, Critical)**
10. Shared error component for `ApiErrorBody.errors`. **(S, High)**
11. Zero-denominator Dashboard branches; delete the dead fallbacks. **(S, High)**
12. Couple side-effect → approval. **(S, High)**
13. Persist eval runs before scoring, poll, and navigate. **(S, High)**
14. Render `per_example`. **(S, High)**
15. KB eval-contract hints + a truthful 400. **(S, High)**
16. Hide the three no-op alias dropdowns. **(XS, Medium)**
17. Delete `PromptCalibrationTab`, the launch machinery, publish step 2, the
    non-functional checkbox, and the three stale copy strings. **(S, Low)**

### 13.3 Structural UX improvements

Home + attention surface · Approvals destination · one Admin destination ·
unified tool catalog · one scoring engine · one run-monitoring component ·
splitting `KnowledgeBases.tsx` and `Prompts.tsx` · command palette · scope-aware
navigation · stage-order affordances on the three workspaces.

### 13.4 Simplify / Consolidate

§9.1 and §9.2 in full — the highest-value entries being C1 (Admin), C2 (tool
catalog), C3/C4/C5 (de-duplication), C6/C7 (one scoring engine), and C8
(Calibrate vs Optimize).

### 13.5 Remove / deprioritize

§9.5 in full. Note that **every item there is verifiably dead, duplicated, or
mislabelled** — none of it is a judgement call about whether a feature is
valuable.

### 13.6 New capability — only where justified

§10, items A1–A14. Eight of the fourteen are *rendering data the platform
already computes*; four are one-field backend additions; two (command palette,
bulk import) are genuinely new UI.

---

## 14. Implementation Roadmap

Sequenced so each phase is independently shippable and de-risks the next.
Phase 0 is deliberately all XS/S: it removes silent wrongness before anything
structural moves.

```mermaid
gantt
  title CALIBER UX remediation
  dateFormat X
  axisFormat %s
  section Phase 0 · Stop the bleeding
  6 silent-correctness fixes             :p0a, 0, 1
  Shared error component                 :p0b, 0, 1
  section Phase 1 · Close the loops
  Trace in review form                   :p1a, 1, 2
  Pin dataset · per_example · judge edit  :p1b, 1, 2
  Eval run persistence + polling         :p1c, 1, 2
  KB contract hints                      :p1d, 1, 2
  section Phase 2 · Attention and onboarding
  Home + attention surface               :p2a, 2, 3
  Approvals destination + status filter  :p2b, 2, 3
  First-run start block                  :p2c, 2, 3
  section Phase 3 · Consolidate
  One Admin destination                  :p3a, 3, 4
  Unified tool catalog                   :p3b, 3, 4
  One scoring engine                     :p3c, 3, 4
  section Phase 4 · De-bloat
  Split KB and Prompts pages             :p4a, 4, 5
  Extract run monitoring                 :p4b, 4, 5
  Delete orphans and dead UI             :p4c, 4, 5
  section Phase 5 · Polish
  Palette · breadcrumbs · PageHeader     :p5a, 5, 6
  Vocabulary pass                        :p5b, 5, 6
```

| Phase | Theme | Contents | Exit criterion |
| --- | --- | --- | --- |
| **0** | **Stop the bleeding** | The 6 Critical XS/S fixes (§13.2 items 1–9) + the shared error component | No surface silently writes to the wrong object, loses work, or promotes without saying so |
| **1** | **Close the four loops** | Trace in the review form · dataset pinning · `per_example` + judge editing · eval-run persistence + polling · KB contract hints · scorecard row actions (needs the one-field row ref) | Each of §7.2's four loops returns to its origin without leaving the product |
| **2** | **Attention & onboarding** | Home + "Needs your attention" · Approvals destination · runs `status` filter · generalised badges · first-run block · Cookbooks linked · scope-aware nav | A user is told what needs them; a fresh install has a path to a working example |
| **3** | **Consolidate destinations** | One Admin (5 tabs, all 10 setting groups) · unified tool catalog with provenance · one scoring engine · Cookbooks into the gallery · Review Queues into Measure · `/agents` renamed and moved | 21 destinations → 19, with one answer per job |
| **4** | **De-bloat** | Split `KnowledgeBases.tsx` and `Prompts.tsx` · extract run monitoring + the 19 helpers · shared graph/preset/result components · delete the two orphan pages and §9.5 in full | ≥7,000 lines of duplicated/dead UI removed; no page over 2,500 lines |
| **5** | **Polish & vocabulary** | Command palette · `crumbs` everywhere · `PageHeader` adoption · shared `EmptyState` · stage-order affordances · the naming pass · the progressive-disclosure sweep (§9.4) | One name per concept; consistent chrome; expert depth disclosed |

### 14.1 Sequencing constraints

- **Phase 1 depends on two one-field backend additions** (a trace/prediction ref
  on `EvalRunResultRow`; identity fields on the trace payload). Land those
  first — they are small and unblock four separate UI fixes.
- **Phase 3's tool catalog depends on adding `execution_backend`/provenance to
  `ToolDefinition`** (~3 lines). Do it in Phase 0 as a no-op type change so the
  catalog work is pure UI.
- **Phase 4 should follow Phase 3**, not precede it: splitting the two large
  pages is far cheaper once the scoring engine and tool catalog have removed
  their forked surfaces.
- **The vocabulary pass belongs last** — renaming before consolidating means
  renaming things that are about to be deleted.

### 14.2 What to measure

Because most recommendations either reduce steps or reveal dropped data, the
natural acceptance criteria are mechanical and testable in CI rather than
survey-based:

| Metric | Now | Target |
| --- | --- | --- |
| Interactions: nothing → first scored evaluation | ~46 (10 JSON blobs) | ≤15, 0 JSON blobs |
| Interactions: prompt authored → baseline diff | 16, loop dead-ends | ≤10, loop closes |
| Interactions: failed run → deployed fix | 14–18 across 3 pages | ≤8 across 1 |
| Mutating controls visible to a user who will 403 | ~33 | 0 |
| Human attention items with a global surface | 1 of 12 | ≥8 of 12 |
| Detail routes with a real breadcrumb | 0 of 9 | 9 of 9 |
| Pages over 2,500 lines | 5 | 0 |
| Dead / unreachable UI items (§8.3) | 20 | 0 |
| Backend signals computed and dropped (§7.1) | 16 | ≤2 |
