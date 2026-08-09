---
name: Optimizer proposal
about: Propose a new optimizer to add to the §6.6 catalog
title: "[optimizer] "
labels: optimizer, proposal
assignees: ''
---

## Optimizer name

<!-- e.g. "TextGrad", "GEPA", "PromptDistill" -->

## What it optimizes

<!-- Single prompt? Prompt program? Conversation policy? Multi-agent bundle? -->

## Algorithm summary

<!-- 3-5 sentences. Cite the paper or reference implementation. -->

## Reference implementation

- Paper: <!-- DOI or arXiv link -->
- Code repository: <!-- GitHub URL -->
- License: <!-- must be compatible with Apache 2.0 -->
- Production-readiness: <!-- production-grade / research-grade / status-unclear -->

## Backing candidate generator

<!-- Which existing agent class would back this, or does it need a new one? -->

## Selection rule

<!-- When should the orchestrator (§6.6.9) auto-select this optimizer? -->

## Demo coverage

<!-- Which existing demo story would this fit, or what would a new scenario look like? -->

## Costs and risks

- Compute cost per job (relative to MetaPrompt baseline):
- Token cost per job:
- Latency:
- Failure modes:

## Tier classification

- [ ] Tier 1 — research-backed adapter (wraps a published library)
- [ ] Tier 2 — CALIBER-internal strategy (composition of existing techniques)
