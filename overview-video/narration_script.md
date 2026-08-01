# CALIBER — Video Narration Script

**Total runtime:** ~9 min (≈517 s narration floor plus inter-scene buffers; final length is recomputed at render time from measured TTS lengths)
**Voice:** en-US-AndrewMultilingualNeural
**Rate:** +5 % (slightly faster, professional pace)
**Pauses:** 250 ms silence inserted before each scene; ~3 s buffer after each narration

The deck is 16 conceptual scenes — a CSS/diagram-driven product overview with no demo seed and no removed-page screenshots. It tracks the current platform: the problem and market context, the patchwork-stack gap, the MLflow-integrated control plane and its two deployment topologies, the asset registries, workflow Studio, the data plane, evaluation and calibration, observability, Aria the agentic copilot, governance, the two "why different" pillars, and the vision.

Narration below is kept readable with the real product terms. The pipeline respells mispronounced terms automatically before synthesis (see Production Notes) — e.g. "MLflow" is spoken as "em-el-flow" — so you don't write the phonetic spelling here.

This script is the source of truth for the narration in [`generate_video.py`](generate_video.py) and the slide content in [`../docs-site/presentation.html`](../docs-site/presentation.html). All three must stay aligned: scene order, scene titles, and narration text must match `generate_video.py`'s `SCENES`, and the slides must match scene-for-scene.

---

## Scene 1 — Title · 30 s

> CALIBER — Contextual Adaptive Lifecycle for Intelligent Build, Evaluation, and Refinement. An MLflow-integrated control plane for trusted agentic workflows: design, verify, calibrate, evaluate, publish, and observe agent resources from one browser platform — then use real evidence to guide each asset's supported lifecycle.

**Visual:** Logo icon with glow drop shadow and an animated, glowing **CALIBER** wordmark. The acronym expands beneath it — **C**ontextual **A**daptive **L**ifecycle for **I**ntelligent **B**uild, **E**valuation, and **R**efinement. Tagline: "An MLflow-integrated control plane for trusted agentic workflows." Footers: "Embedded or standalone · Apache 2.0" and "Design · Verify · Calibrate · Evaluate · Publish · Observe."

---

## Scene 2 — The Problem · 28 s

> Agentic workflows are easy to build and hard to trust. Prompts, tools, and multi-step agents ship faster than anyone can govern them. They drift, they're tuned by feel, and the lineage — what changed, how it scored, who approved it — lives in chat threads and notebooks instead of one system of record. There's no eval gate, no lineage, and no clean way back.

**Visual:** Red "The problem" tag; title "Agentic workflows are easy to build and hard to trust." Three stat cards, each with a "?" value — No eval gate, No lineage, No rollback. Closing line: "Every shipped artifact should answer: how did it score, who approved it, and how do I revert it?" _Note: that title string is also the first sentence of this scene's narration, so the two must always be edited together._

---

## Scene 3 — The Market Gap · 48 s

> This is not a niche worry. Two independent firms put the AI-agents market near fifty billion dollars by 2030, and Gartner expects agentic AI in a third of enterprise software by 2028. But the trust gap is just as large. On the same forecast, more than forty percent of agentic-AI projects will be canceled by 2027. Deloitte finds only one organization in five has mature governance for agentic AI, and the Cloud Security Alliance puts the share that can trace an agent's actions back to a human at twenty-eight percent. The opportunity is enormous. So is the gap underneath it.

**Visual:** "The market" tag; title "The trust gap is real — and it's the size of the market." Five stat cards: ~$50B market by 2030 (~46%/yr), 33% of enterprise software by 2028, 40%+ of agentic-AI projects canceled by 2027, 21% have mature governance (~79% do not), 28% can trace to a human. Closing line: "A large, fast-growing market — held back by a governance and trust gap." followed by an as-of note: "Figures as published on the dates cited; each horizon is its source's own."

---

## Scene 4 — A Patchwork Stack · 45 s

> So why do projects stall? The stack is fragmented. Teams run one tool to orchestrate, another to trace, another to evaluate, another to manage prompts. Most cover only one or two lifecycle stages, so lineage scatters across chat threads and notebooks. Menlo Ventures finds seventy-six percent of enterprise AI use cases are bought rather than built — more vendors to integrate. And here is the deepest gap: these tools measure, then stop. Optimizers exist. Almost none are wired to regression evidence, human authorization, and audited rollback — so almost none close the loop.

**Visual:** Amber "Why projects stall" tag; title "A patchwork stack measures — almost none close the loop." Two diagram boxes. "Point tools" has four bullets — orchestrate in one tool, trace in another, evaluate in a third, manage prompts in a fourth; most cover only 1–2 lifecycle stages; lineage scatters across chat threads and notebooks; leaders are SaaS-first with self-hosting paywalled, open options are observability-led, not full-lifecycle (CALIBER competitive analysis, mid-2026). "The cost" has three bullets — 76% of enterprise AI use cases are bought, not built, so more vendors to integrate (Menlo Ventures, 2025); they **measure** — surface scores and dashboards, then stop; optimizers are widespread, a gated optimize → re-evaluate → authorize → promote path packaged as one product is not. Closing line: "Most tools measure quality. Almost none close the loop on it."

---

## Scene 5 — Introducing CALIBER · 29 s

> CALIBER is one ASGI control-plane codebase for agentic workflows. Mount it inside MLflow as a single process, or run it beside vanilla MLflow over HTTP. The API and the interface are identical — you choose how the two fail and are operated, not what you get. Either way, one interface unifies authoring, evidence, and governance. Compose, measure, govern.

**Visual:** "Introducing" tag; title "One MLflow-integrated control plane for agentic workflows." Subtitle: one **ASGI control-plane codebase** — mount it inside MLflow as one process, or run it beside vanilla MLflow over HTTP; the API and the interface are identical in both, the choice is a failure-domain and operations decision rather than a feature decision, and CALIBER metadata and MLflow evidence retain explicit owners. Three diagram boxes, three bullets each — Compose (prompts, tools, skills, MCP servers; workflows built in a visual Studio; knowledge bases with hybrid retrieval), Measure (evaluation scorecards on test sets; per-asset calibration algorithms; MLflow tracing & per-tool-call spans), Govern (asset-specific validation, testing, apply, and publish; runtime approvals: role, quorum, checkpoints; server-validated identity, RBAC, audit).

---

## Scene 6 — One Platform, Many Assets · 23 s

> Build composes; the Library supplies. Prompts, tools, skills, MCP servers, knowledge bases, and workflows are first-class registered assets. Their workspaces expose the controls and evidence each family actually implements, so you can verify an asset without pretending every family has one uniform lifecycle.

**Visual:** "The platform" tag; title "Build composes. The Library supplies." Six-card grid — Prompts, Tools ("definitions run in a bounded subprocess: separate interpreter, empty environment, private working directory, and hard time, memory, and output limits"), Skills, MCP Servers ("registered Model Context Protocol servers — including a first-party database server — exposed as tools under command and host allowlists"), Knowledge Bases, Workflows.

---

## Scene 7 — Build · Workflows · 30 s

> Workflows are composed in a visual Studio. Wire prompts, tools, skills, and knowledge bases into a graph, preview-run a draft without publishing, then enqueue real runs governed by runtime approvals and checkpointing. Runs arrive from the Studio, an API call, a published service, or a cron trigger — all into one queue, and every run carries an MLflow trace.

**Visual:** "Build" tag; title "Compose agentic workflows in Studio." Four-step flow — Compose (wire nodes in the graph editor) → Preview-run (execute without publishing) → Enqueue (one queue — Studio, API, service, cron) → Trace (per-tool-call spans in MLflow), first and last glowing. Closing line: "Runs land in one queue with atomic claims for multi-replica workers; every run carries an MLflow trace, and checkpointing makes long runs resumable. Webhook and API nodes are checked against the resolved address, with internal ranges — metadata, loopback, RFC1918 — blocked by default, and they claim an effect-ledger row so an outbound call is made at most once across a restart."

---

## Scene 8 — Data & Knowledge · 27 s

> Behind every workflow is the data plane. Knowledge bases provide hybrid retrieval — BM25 and dense vectors fused with reciprocal rank fusion, optionally tri-hybrid with a knowledge graph — and report their own calibration metrics. The object store is CALIBER's own file interface over local or S3-compatible storage, and test sets are the versioned datasets every scored run draws from.

**Visual:** "Data & knowledge" tag; title "The data plane behind every workflow." Three diagram boxes, three bullets each — Knowledge Base (BM25 + dense RRF hybrid search; tri-hybrid with a knowledge graph; Recall@k · nDCG@k · faithfulness), Object Store (local or S3-compatible storage; the file UI for the whole platform; CALIBER's own file store — separate from MLflow's artifact root), Test Sets (versioned {input, expected} examples; grown from real run traces; the substrate for every scorecard).

---

## Scene 9 — Evaluate & Calibrate · 34 s

> Measurement comes in two layers. Evaluation runs a test set through scorers and compares runs. The prompt refinement path can search for a better candidate using provider paths such as Meta-Prompt, GEPA, or DSPy, then apply per-dimension regression checks before candidate-ready. Moving that candidate live still requires an explicit operator action; registry gate verdicts outside the job are advisory. Tools use deterministic, revision-fenced fixture calibration instead. This is CALIBER's evidence loop: measured proposals with explicit human authority.

**Visual:** "Measure" tag; title "Evaluate, then calibrate — with receipts." Five-step flow — Dataset → Score → Calibrate (MetaPrompt · GEPA · DSPy) → Check → Apply (first and last glowing). Closing line: prompt candidates that pass the job's regression checks land at `candidate_ready` and move live only through an explicit operator `apply` — never auto-promoted; registry gate verdicts outside the job are advisory.

---

## Scene 10 — Observe · 34 s

> Every run is a trace you can open. Each run records per-tool-call spans, and the Evaluations surface turns those traces into scorecards, per-example results, and run comparisons — with readiness reporting which providers are real versus simulated, so no score is ever fabricated. Operating it is the other half: the queue reports depth, oldest wait, and worker heartbeats, and each evaluation turns a breached objective into a durable incident.

**Visual:** "Observe & operate" tag; title "Every run is a trace you can open." Three diagram boxes — Tracing (root run trace per workflow run; per-tool-call spans with timing; integrated with MLflow's trace backend), Evaluations, now four bullets (run a dataset through scorers; per-example results & run compare; add a trace to a dataset in one click; real vs simulated providers — no fabricated scores), Runtime health (readiness probes dependencies: ready · not ready · skipped; queue depth, oldest wait, worker heartbeats; declared objectives open durable incidents).

---

## Scene 11 — Aria · The Agentic Copilot · 30 s

> Aria is the embedded copilot. On OpenAI and Claude it runs a real tool-calling loop inside one turn: it reads live CALIBER state, executes capabilities, observes the result — including a workflow run's trace and scored evaluations — and iterates, bounded to eight tool steps. Chat, plan, and build modes pair with manual, auto-safe, and auto-all approvals to bound exactly what it's allowed to do.

**Visual:** "Aria" tag; title "An embedded copilot that runs, observes, and fixes." Three diagram boxes — Modes (`chat` talks · `plan` outlines; `build` materializes drafts; attachments, queue, and steering), Permissioned tools, now four bullets (read in every mode; `manual` is the default — the operator runs each gate; sandboxed/reversible in `auto_safe`; runs & publish only in `auto_all`), Governed (same RBAC and permission checks; tool calls recorded per turn; autonomy bounded & auditable).

---

## Scene 12 — Aria · A Single Turn · 34 s

> Here's a single turn. In build mode with auto-safe approvals, ask Aria to build a tool and test it. It checks for a name clash, drafts the tool, validates the schema, and runs it in a sandbox — a separate interpreter with hard time, memory, and output limits — then reports what it observed and leaves the draft at the tested gate. Every action is recorded in the turn.

**Visual:** "Aria · run, observe, fix" tag; title "Build and test an artifact in one turn." Terminal block, line by line: the muted header `# Aria panel · mode = build · approval = auto_safe`; the `you ›` request — "Build a tool that returns the weekday for an ISO date, then test it."; three tool calls — `list_tools` (checked for a name clash) → `validate_draft` (schema + signature OK) → `run_tool_sandbox` ("2026-06-20" → "Saturday"); and the `aria ›` reply naming the drafted `iso_weekday` at the `tested` gate and offering to approve & publish on the word rather than doing it.

---

## Scene 13 — Governance · 38 s

> Governance follows each asset's real lifecycle rather than one universal gate. Identity is server-validated — password accounts, revocable sessions, four scopes — and Aria reuses those same scopes, permission checks, and audit path as human-driven routes. Tool code runs behind a swappable execution boundary; the shipped one is a bounded subprocess. Validation, tests, an explicit apply or publish, and alias rollback exist where the asset implements them, and credentials stay encrypted behind a reference no route reads back.

**Visual:** "Govern" tag; title "Asset-specific controls, one authorization boundary." Five-control flow — Authorize (accounts, revocable sessions, four scopes) → Contain (bounded tool subprocess · internal-range egress blocks · MCP allowlists) → Verify (asset-specific validation & tests) → Act (explicit apply or publish) → Record (audit actor, action & entity), first and last glowing. Closing line: "Validation, tests, explicit apply or publish actions, and alias rollback exist where the asset implements them. Runtime approvals honour the node's own required role and a quorum of distinct approvers, and by default whoever triggered the run cannot approve it. Credentials live in an encrypted store with versions, rotation, and revocation; an asset holds a `secret://` reference and no route reads a value back."

---

## Scene 14 — Why Different: Unified & Closed-Loop · 27 s

> CALIBER collapses the patchwork into one control-plane interface, deployed either inside MLflow or beside it. CALIBER metadata and MLflow evidence keep explicit owners, while every asset family keeps the lifecycle controls it actually implements. Then the signature difference: an integrated prompt-refinement path that evaluates, searches with provider paths such as Meta-Prompt, GEPA, and DSPy, records per-dimension regression evidence, and requires an explicit human apply before anything goes live.

**Visual:** "Why different" tag; title "Unified, and evidence-to-action." Five-step flow, each step with its description — Evaluate ("held-out test set through scorers — real scores, no fabricated numbers") → Search ("provider paths (Meta-Prompt, GEPA, DSPy) search for a better prompt candidate") → Check ("record per-dimension regression evidence") → Candidate-ready ("clears the gate but stays offline — never auto-promoted") → Apply ("goes live only via an explicit operator step"), first and last glowing. Closing line: "Embedded or standalone. Evidence connected to action. Self-hostable — your data stays home."

---

## Scene 15 — Why Different: Open & Governed · 27 s

> Whether a person or Aria initiates a change, the audit trail records the same actor, action, and entity. That matters: only twenty-eight percent of organizations can trace an agent's actions back to a human, and sixty-one percent of executives surveyed in 2025 required a human in the loop. EU AI Act high-risk obligations phase in from August 2026, and CALIBER is open source under Apache 2.0 — your data and lineage stay in infrastructure you control.

**Visual:** "Why different" tag; title "Open, and governed." Four vision cards — Same authorization boundary (humans and Aria resolve the same server-validated identity — password accounts, revocable sessions, four scopes; each asset follows the lifecycle it actually implements; the audit trail records actor, action, and entity); Traceable to a human (28% can trace, ~72% cannot, CSA 2025); Human in the loop (61% of executives when surveyed, KPMG Q3 2025; runtime approvals honour the node's required role and a quorum of distinct approvers, and by default whoever triggered the run cannot approve it); Open, and audit-ready (EU AI Act high-risk obligations phase in from 2 August 2026; penalties reach €35M or 7% of global annual turnover, Article 99; Apache 2.0, self-hostable). Closing line: "Governance binds humans and the copilot equally. Open source, no lock-in."

---

## Scene 16 — Vision · 33 s

> CALIBER is open source and MLflow-integrated, deployable embedded or standalone. Agentic with Aria's permissioned tool loop. Measured by evaluation and calibration. Refined through asset-specific lifecycles that preserve evidence and human authority. Governed by server-validated identity, a bounded execution boundary, and recorded actions. Observable through tracing and durable incidents. Apache 2 licensed, with no vendor lock-in. A contextual, adaptive lifecycle for intelligent build, evaluation, and refinement — agentic workflows you can measure, refine, govern, and trust.

**Visual:** "The vision" tag; title "Agentic workflows you can measure, refine, govern, and trust." Six-card vision grid — Integrated, Agentic, Measured & refined, Governed, Observable, Open. Footer: "caliber-suite · Apache 2.0 · open source."

---

## Production Notes

- **Voice:** `en-US-AndrewMultilingualNeural` at `+5%` rate via `edge-tts`
- **Pauses:** 250 ms silence before each scene (`adelay`); ~3 s buffer after each narration (`apad`)
- **Buffer:** scene durations recomputed at runtime from measured TTS length + 3 s, then `SCENE_TIMINGS` in `presentation.html` is patched to match
- **Pronunciation hints** (applied automatically by `apply_pronunciations` in `generate_video.py` — keep the narration above in real terms): "MLflow" → "em-el-flow"; "RBAC" → "ar-back"; "DSPy" → "dee-es-pie"; "BM25" → "B M twenty-five"; "MCP" → "M C P"; "API" → "A P I"; "S3" → "S 3".
- **Resolution:** 1920 × 1080 @ 30 fps
