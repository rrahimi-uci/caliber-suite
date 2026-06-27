# CALIBER — Video Narration Script

**Total runtime:** ~8 min (≈462 s narration floor plus inter-scene buffers; final length is recomputed at render time from measured TTS lengths)
**Voice:** en-US-AndrewMultilingualNeural
**Rate:** +5 % (slightly faster, professional pace)
**Pauses:** 250 ms silence inserted before each scene; ~3 s buffer after each narration

The deck is 16 conceptual scenes — a CSS/diagram-driven product overview with no demo seed and no removed-page screenshots. It tracks the current platform: the problem and market context, the patchwork-stack gap, the same-origin control plane, the asset registries, workflow Studio, the data plane, evaluation and calibration, observability, Aria the agentic copilot, governance, the two "why different" pillars, and the vision.

Narration below is kept readable with the real product terms. The pipeline respells mispronounced terms automatically before synthesis (see Production Notes) — e.g. "MLflow" is spoken as "em-el-flow" — so you don't write the phonetic spelling here.

This script is the source of truth for the narration in [`generate_video.py`](generate_video.py) and the slide content in [`../docs-site/presentation.html`](../docs-site/presentation.html). All three must stay aligned: scene order, scene titles, and narration text must match `generate_video.py`'s `SCENES`, and the slides must match scene-for-scene.

---

## Scene 1 — Title · 30 s

> CALIBER — Contextual Adaptive Lifecycle for Intelligent Build, Evaluation, and Refinement. An MLflow-native control plane for trusted agentic workflows: design, verify, calibrate, evaluate, publish, and observe every agent resource from one same-origin platform — then keep learning from real evidence and refining every asset across its lifecycle.

**Visual:** Logo icon with glow drop shadow and an animated, glowing **CALIBER** wordmark. The acronym expands beneath it — **C**ontextual **A**daptive **L**ifecycle for **I**ntelligent **B**uild, **E**valuation, and **R**efinement. Tagline: "An MLflow-native control plane for trusted agentic workflows." Footers: "An MLflow plugin · Apache 2.0 · Same-origin by design" and "Design · Verify · Calibrate · Evaluate · Publish · Observe."

---

## Scene 2 — The Problem · 28 s

> Agentic workflows are easy to build and hard to trust. Prompts, tools, and multi-step agents ship faster than anyone can govern them. They drift, they're tuned by feel, and the lineage — what changed, how it scored, who approved it — lives in chat threads and notebooks instead of one system of record. There's no eval gate, no lineage, and no clean way back.

**Visual:** Red "The problem" tag. Three stat cards, each with a "?" value — No eval gate, No lineage, No rollback. Closing line: "Every shipped artifact should answer: how did it score, who approved it, and how do I revert it?"

---

## Scene 3 — The Market Gap · 30 s

> This is not a niche worry. Two independent firms put the AI-agents market near fifty billion dollars by 2030, growing roughly forty-six percent a year, and Gartner expects agentic AI in a third of enterprise software by 2028, up from under one percent in 2024. But the trust gap is just as large. Deloitte finds about eighty percent of organizations lack mature governance. The Cloud Security Alliance finds only twenty-eight percent can trace an agent's actions back to a human. The opportunity is enormous. So is the gap underneath it.

**Visual:** "The market" tag; title "The trust gap is real — and it's the size of the market." Five stat cards: ~$50B market by 2030 (~46%/yr), 33% of enterprise software by 2028, 40%+ of agentic-AI projects canceled by 2027, ~80% lack mature governance, 28% can trace to a human. Closing line: "A large, fast-growing market — held back by a governance and trust gap."

---

## Scene 4 — A Patchwork Stack · 30 s

> So why do projects stall? The stack is fragmented. Teams run one tool to orchestrate, another to trace, another to evaluate, another to manage prompts. Most cover only one or two lifecycle stages, so lineage scatters across chat threads and notebooks. With seventy-six percent of enterprise AI use cases bought rather than built, that is even more vendors to integrate. And here is the deepest gap: these tools measure. They surface scores and stop. None of them close the loop.

**Visual:** Amber "Why projects stall" tag; title "A patchwork stack measures — it never closes the loop." Two diagram boxes — "Point tools" (orchestrate/trace/evaluate/prompts split across tools; 1–2 lifecycle stages each; lineage scattered) and "The cost" (76% bought not built; they measure and stop; no gated optimize → re-evaluate → promote loop). Closing line: "Most tools measure quality. None of them close the loop on it."

---

## Scene 5 — Introducing CALIBER · 28 s

> CALIBER is one same-origin control plane for agentic workflows. As an MLflow application plugin, it mounts its interface and API on the same server as MLflow, so composing, measuring, and governing every agent resource happens against one identity, one store, and one trace backend. Compose, measure, govern.

**Visual:** "Introducing" tag; title "One same-origin control plane for agentic workflows." Three diagram boxes — Compose / Measure / Govern.

---

## Scene 6 — One Platform, Many Assets · 28 s

> Build composes; the Library supplies. Prompts, tools, skills, MCP servers, knowledge bases, and workflows are each first-class, versioned assets. Every one has its own workspace — pytest for an asset — with a status header, stage tabs, and durable test runs, so you develop and verify it in isolation before a workflow ever uses it.

**Visual:** "The platform" tag; title "Build composes. The Library supplies." Six-card grid — Prompts, Tools, Skills, MCP Servers, Knowledge Bases, Workflows.

---

## Scene 7 — Build · Workflows · 28 s

> Workflows are composed in a visual Studio. Wire prompts, tools, skills, and knowledge bases into a graph, preview-run a draft without publishing, then enqueue real runs governed by runtime approvals and checkpointing. Every run carries an MLflow trace with per-tool-call spans.

**Visual:** "Build" tag; title "Compose agentic workflows in Studio." Four-step flow — Compose → Preview-run → Enqueue → Trace (first and last glowing). Closing line about queued runs with atomic claims, MLflow traces, and resumable checkpointing.

---

## Scene 8 — Data & Knowledge · 26 s

> Behind every workflow is the data plane. Knowledge bases provide hybrid retrieval — BM25 and dense vectors fused with reciprocal rank fusion, optionally tri-hybrid with a knowledge graph — and report their own calibration metrics. The object store is the file interface over S3-compatible storage, and test sets are the versioned datasets every scored run draws from.

**Visual:** "Data & knowledge" tag; title "The data plane behind every workflow." Three diagram boxes — Knowledge Base (BM25 + dense RRF, tri-hybrid graph, Recall@k · nDCG@k · faithfulness), Object Store (S3-compatible file UI, backs MLflow & CALIBER artifacts), Test Sets (versioned {input, expected} examples grown from traces).

---

## Scene 9 — Evaluate & Calibrate · 34 s

> Measurement comes in two layers. Evaluation runs a test set through scorers and compares runs. Calibration goes further: it searches for a better version of an asset — using optimizers like Meta-Prompt, GEPA, or DSPy — and gates promotion on a measured score against the held-out set. Candidates that clear the gate land at candidate-ready and move live only through an explicit operator apply. Never auto-promoted. This is the heart of CALIBER: a closed learning loop that refines each asset against real evidence, so quality compounds over its lifecycle instead of drifting.

**Visual:** "Measure" tag; title "Evaluate, then calibrate — with receipts." Five-step flow — Dataset → Score → Calibrate (MetaPrompt · GEPA · DSPy) → Gate → Apply (first and last glowing). Closing line: candidates that clear the gate land at `candidate_ready` and go live only via an explicit operator `apply` — never auto-promoted.

---

## Scene 10 — Observe · 26 s

> Every run is a trace you can open. Observability is built on MLflow tracing: each workflow run records per-tool-call spans, and the Evaluations surface turns those traces into scorecards, per-example results, and run-to-run comparisons. A readiness endpoint honestly reports which providers are real versus simulated — no fabricated scores, ever.

**Visual:** "Observe" tag; title "Every run is a trace you can open." Three diagram boxes — Tracing (root run trace, per-tool-call spans, same-origin MLflow trace UI), Evaluations (datasets through scorers, per-example results & compare, add-trace-to-dataset), Honesty (readiness reports real vs simulated; no fabricated scores).

---

## Scene 11 — Aria · The Agentic Copilot · 30 s

> Aria is the embedded copilot. On OpenAI and Claude it runs a real tool-calling loop inside one turn: it reads live CALIBER state, executes capabilities, observes the result — including a workflow run's trace and scored evaluations — and iterates, bounded to eight tool steps. Chat, plan, and build modes pair with manual, auto-safe, and auto-all approvals to bound exactly what it's allowed to do.

**Visual:** "Aria" tag; title "An embedded copilot that runs, observes, and fixes." Three diagram boxes — Modes (chat / plan / build), Permissioned tools (read in every mode; sandboxed/reversible in auto_safe; runs & publish only in auto_all), Governed (drafts ride the same gates; tool calls recorded per turn; autonomy bounded & auditable).

---

## Scene 12 — Aria · A Single Turn · 26 s

> Here's a single turn. In build mode with auto-safe approvals, ask Aria to build a tool and test it. It checks for a name clash, drafts the tool, validates the schema, and runs it in the sandbox — then reports exactly what it observed and leaves the draft at the tested gate. Every action is recorded in the turn.

**Visual:** "Aria · run, observe, fix" tag; title "Build and test an artifact in one turn." Terminal block showing the tool-loop transcript: `list_tools` (name-clash check) → `validate_draft` (schema + signature) → `run_tool_sandbox` ("2026-06-20" → "Saturday"), ending with Aria reporting the draft at the `tested` gate.

---

## Scene 13 — Governance · 28 s

> One gate governs every change, whether a person makes it or Aria does. Validate, test, approve, publish — there is no copilot bypass. RBAC controls who can advance each gate, the audit trail records actor, action, and entity, and artifacts live in object storage. Workflow runtime approvals govern live execution separately from offline artifact promotion.

**Visual:** "Govern" tag; title "One gate for every change, human or Aria." Four-gate flow — Validate → Test → Approve → Publish (first and last glowing). Closing line: RBAC scopes who advances each gate; audit trail records actor, action, entity; runtime approvals govern live execution separately from offline promotion.

---

## Scene 14 — Why Different: Unified & Closed-Loop · 30 s

> CALIBER collapses the patchwork into one same-origin control plane. It mounts its interface and API on the same server as MLflow, as an application plugin: one identity, one store, one trace backend. Prompts, tools, skills, knowledge bases, and workflows are each first-class, versioned assets with their own pytest-style workspace. Then the signature difference, a closed loop. It evaluates, searches for a better version with optimizers like Meta-Prompt, GEPA, and DSPy, and gates promotion on a measured score against a held-out set. It does not just measure. It closes the loop.

**Visual:** "Why different" tag; title "Unified, and closed-loop." Five-step flow — Evaluate → Search (Meta-Prompt, GEPA, DSPy) → Gate → Candidate-ready → Apply (first and last glowing). Closing line: "Same-origin on MLflow. A loop that closes, not measure-only. Self-hostable — your data stays home."

---

## Scene 15 — Why Different: Open & Governed · 30 s

> One gate governs every change, validate, test, approve, publish, whether a person makes it or the Aria copilot does. There is no copilot bypass. RBAC scopes who advances each gate, the audit trail records actor, action, and entity, and artifacts live in object storage. That matters: only twenty-eight percent of organizations can trace an agent's actions back to a human, and sixty-one percent of executives now require a human in the loop. With EU AI Act high-risk obligations landing August 2026, and CALIBER open source under Apache 2.0, your data and lineage stay home.

**Visual:** "Why different" tag; title "Open, and governed." Four vision cards — One gate, no bypass; Traceable to a human (28% can trace, CSA 2025); Human in the loop (61% of executives, KPMG Q3 2025); Open, ahead of the clock (EU AI Act high-risk obligations Aug 2, 2026; Apache 2.0, self-hostable). Closing line: "Governance binds humans and the copilot equally. Open source, no lock-in."

---

## Scene 16 — Vision · 30 s

> CALIBER is open source and MLflow-native. Native to MLflow. Agentic with Aria's permissioned tool loop. Measured by evaluation and calibration. Refined by a contextual, adaptive lifecycle that learns from evidence and improves every asset over time. Governed by explicit approval. Observable through tracing. Apache 2 licensed, with no vendor lock-in. A contextual, adaptive lifecycle for intelligent build, evaluation, and refinement — agentic workflows you can measure, refine, govern, and trust.

**Visual:** "The vision" tag; title "Agentic workflows you can measure, refine, govern, and trust." Six-card vision grid — Native, Agentic, Measured & refined, Governed, Observable, Open. Footer: "caliber-mlflow · Apache 2.0 · open source."

---

## Production Notes

- **Voice:** `en-US-AndrewMultilingualNeural` at `+5%` rate via `edge-tts`
- **Pauses:** 250 ms silence before each scene (`adelay`); ~3 s buffer after each narration (`apad`)
- **Buffer:** scene durations recomputed at runtime from measured TTS length + 3 s, then `SCENE_TIMINGS` in `presentation.html` is patched to match
- **Pronunciation hints** (applied automatically by `apply_pronunciations` in `generate_video.py` — keep the narration above in real terms): "MLflow" → "em-el-flow"; "RBAC" → "ar-back"; "DSPy" → "dee-es-pie"; "BM25" → "B M twenty-five"; "MCP" → "M C P"; "API" → "A P I"; "S3" → "S 3".
- **Resolution:** 1920 × 1080 @ 30 fps
