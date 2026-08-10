# caliber-plugin-sdk

Experimental contracts for writing CALIBER extensions.

> **This contract is experimental.** It will change until it has survived a
> release, and CALIBER marks every plugin-provided optimizer `experimental`
> regardless of what the plugin says about itself. Pin an exact version.

## What a plugin is

Three things:

1. A class with an `optimize` method matching the `Optimizer` protocol.
2. A `PluginDeclaration` saying what it is called and what it can target.
3. An entry point in the `caliber.optimizers` group pointing at that declaration.

```python
from caliber_plugin_sdk import (
    OptimizationRequest,
    OptimizationResult,
    OptimizerUnavailable,
    declare,
)


class ShortenPrompt:
    def optimize(self, request: OptimizationRequest) -> OptimizationResult:
        if not request.current_content:
            # Declining is a first-class outcome. CALIBER falls back to a
            # built-in and records a note; returning something useless would
            # instead cost a human review.
            raise OptimizerUnavailable("nothing to shorten on cold start")
        return OptimizationResult(
            content=request.current_content[:2000],
            rationale="Truncated to 2000 characters to fit the model's context.",
        )


declaration = declare(
    "ShortenPrompt",
    summary="Truncates an over-long prompt to fit the context window.",
    artifact_types=("prompt",),
    factory=ShortenPrompt,
)
```

```toml
# your pyproject.toml
[project.entry-points."caliber.optimizers"]
shorten = "your_package:declaration"
```

## Installing your plugin is not enabling it

An optimizer writes the artifact CALIBER promotes to production. A plugin is
therefore discovered automatically and **enabled by nobody automatically** — the
deployment must name your *distribution* in the allowlist:

```bash
export CALIBER_PLUGIN_ALLOWLIST=your-distribution,another-plugin
```

Until then your plugin is reported in `GET /capabilities` as installed and
inert, so an operator can see it and enable it, rather than wondering why the
wheel they installed had no effect.

The allowlist matches distributions rather than optimizer names, because
allowlisting is a statement about who you trust and not about what they happened
to call their entry point.

## Check it before you ship it

```python
from caliber_plugin_sdk.conformance import assert_conformant

from your_package import declaration


def test_the_plugin_conforms() -> None:
    assert_conformant(declaration)
```

The suite runs your optimizer against every artifact kind it claims, including
the **cold-start** case where `current_content` is empty. That is the first
refinement a new agent ever runs and the one a plugin developed against a
populated deployment has never seen.

It reports every problem it finds rather than the first, and it checks the things
that have consequences downstream:

| Check | Why it matters |
| --- | --- |
| Non-empty `content` | An empty candidate that passed the eval gate promotes as a deletion. |
| Non-empty `rationale` | A human approves this diff; approving one with no stated reason makes the gate a formality. |
| Name is not a built-in | Every agent configured for `GEPA` would silently start running your code. The server refuses it too. |
| Factory does not raise | A raising factory kills your plugin's registration, not one run. Defer setup into `optimize`. |
| Telemetry is non-negative | `None` means "not reported"; `0` means free. `-1` means neither. |

## What a plugin does not get

`OptimizationRequest` carries the artifact and the diagnosis. It does not carry
a database session, the agent row, or any credential.

That is the boundary, not an oversight. Selection, evaluation, gating,
promotion, and audit stay on CALIBER's side where they can be governed. A plugin
proposes; CALIBER decides.

## Declining is not failing

Raise `OptimizerUnavailable` when a precondition is missing — an optional
dependency, an empty trainset, a diagnosis too vague to act on. CALIBER falls
back to a built-in and records a note explaining the fallback.

Any *other* exception fails the refinement job, which is correct: falling back
silently past a bug would hide it.

## Development

```bash
pip install -e ".[dev]"
pytest
mypy
ruff check .
python -m build
```

Zero runtime dependencies, and that is the design: a plugin that had to import
the CALIBER server would pull in mlflow, sqlalchemy, and starlette, and this
package's tests would be testing an integration rather than a contract.
