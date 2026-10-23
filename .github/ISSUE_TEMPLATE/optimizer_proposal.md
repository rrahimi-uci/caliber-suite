---
name: Optimizer proposal
about: Propose a new optimizer for CALIBER's calibration loop (built in, or as a plugin)
title: "[optimizer] "
labels: optimizer, proposal
assignees: ''
---

Background: the built-in optimizers and how one is selected are described in
[Calibration architecture](../../docs/15-calibration/architecture.md#6-per-asset-calibration-algorithms).
A third party can also ship an optimizer without changing CALIBER; see
[Writing a CALIBER plugin](../../docs/sdk/plugins.md). Say below whether you propose a built-in or a plugin.

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

<!-- Which existing optimizer or code path would back this, or does it need a new one? -->

## Selection rule

<!-- When should CALIBER's optimizer-selection policy choose this optimizer (see Calibration architecture)? -->

## Demo coverage

<!-- Which existing test set, cookbook lab or scenario would exercise it, or what would a new one look like? -->

## Costs and risks

- Compute cost per job (relative to MetaPrompt baseline):
- Token cost per job:
- Latency:
- Failure modes:

## Tier classification

- [ ] Tier 1 — research-backed adapter (wraps a published library)
- [ ] Tier 2 — CALIBER-internal strategy (composition of existing techniques)
