# CALIBER — Video Narration Script

**Total runtime:** ~5 min 30 s (≈324 s narration floor plus inter-scene buffers)
**Voice:** en-US-AndrewMultilingualNeural
**Rate:** +5 % (slightly faster, professional pace)
**Pauses:** 250 ms silence inserted before each scene; ~3 s buffer after each narration

The deck is 12 conceptual scenes — a CSS/diagram-driven product overview with no demo seed and no removed-page screenshots. It tracks the current platform: the asset registries, workflow Studio, the data plane, evaluation and calibration, observability, and Aria the agentic copilot.

---

## Scene 1 — Title · 30 s

> CALIBER — Contextual Adaptive Lifecycle for Intelligent Build, Evaluation, and Refinement. An M L flow-native control plane for trusted agentic workflows: design, verify, calibrate, evaluate, publish, and observe every agent resource from one same-origin platform — then keep learning from real evidence and refining every asset across its lifecycle.

**Visual:** Logo with glow drop shadow, animated wordmark. The acronym expands beneath it — **C**ontextual **A**daptive **L**ifecycle for **I**ntelligent **B**uild, **E**valuation, and **R**efinement. Tagline: Design · Verify · Calibrate · Evaluate · Publish · Observe.

---

## Scene 2 — The Problem · 28 s

> Agentic workflows are easy to build and hard to trust. Prompts, tools, and multi-step agents ship faster than anyone can govern them. They drift, they're tuned by feel, and the lineage — what changed, how it scored, who approved it — lives in chat threads and notebooks instead of one system of record. There's no eval gate, no lineage, and no clean way back.

**Visual:** Three stat cards: no eval gate, no lineage, no rollback.

---

## Scene 3 — Introducing CALIBER · 28 s

> CALIBER is one same-origin control plane for agentic workflows. As an M L flow application plugin, it mounts its interface and A P I on the same server as M L flow, so composing, measuring, and governing every agent resource happens against one identity, one store, and one trace backend. Compose, measure, govern.

**Visual:** Compose / Measure / Govern — three capability cards.

---

## Scene 4 — One Platform, Many Assets · 28 s

> Build composes; the Library supplies. Prompts, tools, skills, M C P servers, knowledge bases, and workflows are each first-class, versioned assets. Every one has its own workspace — pytest for an asset — with a status header, stage tabs, and durable test runs, so you develop and verify it in isolation before a workflow ever uses it.

**Visual:** Six-card grid — Prompts, Tools, Skills, MCP Servers, Knowledge Bases, Workflows.

---

## Scene 5 — Build · Workflows · 28 s

> Workflows are composed in a visual Studio. Wire prompts, tools, skills, and knowledge bases into a graph, preview-run a draft without publishing, then enqueue real runs governed by runtime approvals and checkpointing. Every run carries an M L flow trace with per-tool-call spans.

**Visual:** Four-step flow — Compose, Preview-run, Enqueue, Trace — first and last glowing.

---

## Scene 6 — Data & Knowledge · 26 s

> Behind every workflow is the data plane. Knowledge bases provide hybrid retrieval — B M twenty-five and dense vectors fused with reciprocal rank fusion, optionally tri-hybrid with a knowledge graph — and report their own calibration metrics. The object store is the file interface over S 3-compatible storage, and test sets are the versioned datasets every scored run draws from.

**Visual:** Three diagram boxes — Knowledge Base, Object Store, Test Sets.

---

## Scene 7 — Evaluate & Calibrate · 34 s

> Measurement comes in two layers. Evaluation runs a test set through scorers and compares runs. Calibration goes further: it searches for a better version of an asset — using optimizers like Meta-Prompt, GEPA, or D S Py — and gates promotion on a measured score against the held-out set. Candidates that clear the gate land at candidate-ready and move live only through an explicit operator apply. Never auto-promoted. This is the heart of CALIBER: a closed learning loop that refines each asset against real evidence, so quality compounds over its lifecycle instead of drifting.

**Visual:** Five-step flow — Dataset, Score, Calibrate, Gate, Apply — framed as a closed learning loop.

---

## Scene 8 — Observe · 26 s

> Every run is a trace you can open. Observability is built on M L flow tracing: each workflow run records per-tool-call spans, and the Evaluations surface turns those traces into scorecards, per-example results, and run-to-run comparisons. A readiness endpoint honestly reports which providers are real versus simulated — no fabricated scores, ever.

**Visual:** Three diagram boxes — Tracing, Evaluations, Honesty.

---

## Scene 9 — Aria · The Agentic Copilot · 30 s

> Aria is the embedded copilot. On OpenAI and Claude it runs a real tool-calling loop inside one turn: it reads live CALIBER state, executes capabilities, observes the result — including a workflow run's trace and scored evaluations — and iterates, bounded to eight tool steps. Chat, plan, and build modes pair with manual, auto-safe, and auto-all approvals to bound exactly what it's allowed to do.

**Visual:** Three diagram boxes — Modes, Permissioned tools, Governed.

---

## Scene 10 — Aria · A Single Turn · 26 s

> Here's a single turn. In build mode with auto-safe approvals, ask Aria to build a tool and test it. It checks for a name clash, drafts the tool, validates the schema, and runs it in the sandbox — then reports exactly what it observed and leaves the draft at the tested gate. Every action is recorded in the turn.

**Visual:** Terminal block showing the tool-loop transcript with the executed tool calls.

---

## Scene 11 — Governance · 28 s

> One gate governs every change, whether a person makes it or Aria does. Validate, test, approve, publish — there is no copilot bypass. R B A C controls who can advance each gate, the audit trail records actor, action, and entity, and artifacts live in object storage. Workflow runtime approvals govern live execution separately from offline artifact promotion.

**Visual:** Four-gate flow — Validate, Test, Approve, Publish — first and last glowing.

---

## Scene 12 — Vision · 30 s

> CALIBER is open source and M L flow-native. Native to M L flow. Agentic with Aria's permissioned tool loop. Measured by evaluation and calibration. Refined by a contextual, adaptive lifecycle that learns from evidence and improves every asset over time. Governed by explicit approval. Observable through tracing. Apache 2 licensed, with no vendor lock-in. A contextual, adaptive lifecycle for intelligent build, evaluation, and refinement — agentic workflows you can measure, refine, govern, and trust.

**Visual:** Six-card vision grid — Native, Agentic, Measured, Governed, Observable, Open.

---

## Production Notes

- **Voice:** `en-US-AndrewMultilingualNeural` at `+5%` rate via `edge-tts`
- **Pauses:** 250 ms silence before each scene (`adelay`); ~3 s buffer after each narration (`apad`)
- **Buffer:** scene durations recomputed at runtime from measured T T S length + 3 s
- **Pronunciation hints:** "M L flow" reads as "em-el-flow"; "M C P" as "em-see-pee"; "R B A C" as "are-bee-ay-see"; "D S Py" as "dee-ess-pie"; "A P I" as "ay-pee-eye"; "S 3" as "ess-three"; "B M twenty-five" as "bee-em twenty-five".
- **Resolution:** 1920 × 1080 @ 30 fps
