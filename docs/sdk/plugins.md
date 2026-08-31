---
audience:
  - developer
doc_type: how-to
product_area: sdk
stability: experimental
prerequisites:
  - A CALIBER deployment or SDK extension question
reviewed_on: 2026-08-10
version_applicability: current main branch docs contract
tags:
  - sdk
  - plugins
  - extensibility
  - optimizers
---

# Writing a CALIBER plugin

`caliber-plugin-sdk` lets a third party add an optimizer to CALIBER without
editing CALIBER. Install a wheel, allowlist its distribution, and the refinement
loop can select it.

> **The contract is experimental.** It will change until it has survived a
> release, and CALIBER marks every plugin-provided optimizer `experimental`
> regardless of what the plugin says about itself. Pin an exact version.

## Why an allowlist

The obvious design is: any installed distribution advertising into the
`caliber.optimizers` entry-point group gets loaded. Most plugin systems work that
way, and it is the wrong choice here.

An optimizer authors the artifact CALIBER promotes to production. A transitive
dependency — three levels down, in something a developer added for an unrelated
reason — that quietly registered one would acquire authority over production
prompts with no review step, and nothing would look wrong from the outside: the
refinement loop would keep working, jobs would keep completing, candidates would
keep passing the eval gate.

So discovery and enablement are separate. CALIBER reports every installed plugin
and loads only the ones whose **distribution** the deployment names:

```bash
export CALIBER_PLUGIN_ALLOWLIST=acme-caliber-optimizers,another-plugin
```

The allowlist matches distributions rather than optimizer names, because
allowlisting is a statement about who you trust, not about what they happened to
call their entry point.

An unlisted plugin appears in `GET /capabilities` as installed and inert — which
is what lets an operator enable it, rather than being left to wonder why the
wheel they installed had no effect.

## What a plugin is

Three things.

**One.** A class with an `optimize` method:

```python
from caliber_plugin_sdk import (
    OptimizationRequest,
    OptimizationResult,
    OptimizerUnavailable,
)


class ShortenPrompt:
    def optimize(self, request: OptimizationRequest) -> OptimizationResult:
        if not request.current_content:
            raise OptimizerUnavailable("nothing to shorten on cold start")
        return OptimizationResult(
            content=request.current_content[:2000],
            rationale="Truncated to 2000 characters to fit the model's context.",
        )
```

**Two.** A declaration:

```python
from caliber_plugin_sdk import declare

declaration = declare(
    "ShortenPrompt",
    summary="Truncates an over-long prompt to fit the context window.",
    artifact_types=("prompt",),
    factory=ShortenPrompt,
)
```

`name` is stored on every job that runs the optimizer, so it is a durable
contract rather than a label — renaming it orphans the rows that reference it.

`summary` is required because it is what an operator reads when deciding whether
to allowlist your distribution. An empty summary makes that decision blind.

**Three.** An entry point:

```toml
[project.entry-points."caliber.optimizers"]
shorten = "your_package:declaration"
```

Point it at the declaration, or at a zero-argument callable returning one. The
callable form exists so you can probe for an optional dependency and declare
honestly instead of registering a capability you cannot deliver.

## What a plugin does not get

`OptimizationRequest` carries the artifact and the diagnosis. It carries no
database session, no agent row, and no credential.

That boundary is the design. Selection, evaluation, gating, promotion, and audit
stay on CALIBER's side, where they can be governed. **A plugin proposes; CALIBER
decides.** Handing a plugin a session would move the governed parts to the
ungoverned side of the boundary.

## Declining is a first-class outcome

Raise `OptimizerUnavailable` when a precondition is missing — an optional
dependency, an empty trainset, a diagnosis too vague to act on. CALIBER falls
back to a built-in and records a note explaining why.

Any *other* exception fails the refinement job, and that is correct: falling back
silently past a bug would hide it.

The reference implementation declines in three situations, each one a case where
producing a candidate anyway would cost a human review for nothing:

| Situation | Why declining is right |
| --- | --- |
| Diagnosis confidence below 0.5 | A hard requirement derived from a guess can make the artifact worse in a way the eval gate does not catch. |
| Diagnosis names no root cause | There is nothing to act on. |
| The change is already applied | A no-op candidate spends the scarcest resource in the loop — a reviewer's attention. |

## Check it before you ship it

```python
from caliber_plugin_sdk.conformance import assert_conformant

from your_package import declaration


def test_the_plugin_conforms() -> None:
    assert_conformant(declaration)
```

The suite runs your optimizer against every artifact kind it claims, including
the **cold-start** case where `current_content` is empty. That is the first
refinement a new agent ever runs, and the one a plugin developed against a
populated deployment has never seen.

It reports every problem it finds rather than stopping at the first, and it
checks the things with downstream consequences:

| Check | Why it matters |
| --- | --- |
| Non-empty `content` | An empty candidate that passed the eval gate promotes as a deletion. |
| Non-empty `rationale` | A human approves this diff. Approving one with no stated reason makes the gate a formality. |
| Name is not a built-in | Every agent configured for `GEPA` would silently start running your code instead. |
| Factory does not raise | A raising factory kills your plugin's registration, not one run. Defer setup into `optimize`. |
| Telemetry is non-negative | `None` means "not reported"; `0` means free. `-1` means neither. |

## The name you may not take

A plugin cannot register a name CALIBER ships — `MetaPrompt`,
`SkillMetaPrompt`, `GEPA`, `DSPyBootstrapFewShot`, `DSPyMIPRO`.

This is the one refusal worth explaining, because it looks like a naming
convention and is not. If a plugin could claim `GEPA`, then installing a wheel
would change what `GEPA` does for every agent already configured to use it. Same
name in every config, same name in every audit record, different author's code
producing the candidates. Nothing in any diff would show it.

Both sides refuse it: the conformance suite catches it in your test run, and the
server catches it at load time in case conformance never ran.

## What an operator sees

`GET /capabilities` reports both what can run and what is installed:

```python
extensibility = caliber.capabilities_info.get().extensibility

for optimizer in extensibility.optimizers_for("prompt"):
    print(optimizer.name, optimizer.source, optimizer.is_third_party)

for plugin in extensibility.plugins:
    if not plugin.is_active:
        print(f"{plugin.distribution} is installed but not enabled")
```

Reading the plugin list never imports the code it describes — which is what makes
it safe to render *before* deciding whether to trust anything in it.

A plugin that was allowlisted and then failed to load reports its error rather
than disappearing. The deployment asked for it, so silence would read as success.
