# Versioning, stability, and compatibility

A published versioning policy, and a compatibility statement between
`caliber-sdk` and the CALIBER server. It applies to `caliber-sdk`
specifically; `caliber-cli` and `caliber-plugin-sdk` follow the same shape
but are versioned independently.

## Where this stands today

`caliber-sdk` is **`0.1.0.dev0`**, `Development Status :: 3 - Alpha`. Every public
symbol can still change without a deprecation cycle until 1.0.0 ships — the
cycle described below is a policy this project is *building toward*, and the
two aliases in `client.py` (`capabilities_api`, `datasets`) are the first
symbols actually governed by it.

## SemVer, once 1.0.0 ships

`caliber-sdk` follows [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`.

- **MAJOR** — a breaking change to a `ga` symbol: a removed method, a changed
  required parameter, a narrowed return type.
- **MINOR** — new methods, new resources, new optional parameters. Always safe
  to upgrade for a `ga`-only caller.
- **PATCH** — bug fixes that do not change a documented contract.

**`caliber-sdk` graduates from `0.1.0.dev0`/Alpha to `1.0.0` only when two
things are both true**: 100% addressable API coverage (already true) and an
interface-quality bar — typed request models, uniform idempotency, typed SSE
frames, no dead exports — that is not yet met. Shipping 1.0.0 before then
would claim a stability the interface does not yet have.

## What `ga` / `beta` / `internal` promise

These are **server-reported**, not hardcoded in the SDK — `client.stability`
(or `client.capabilities()["sdk_stability"]`) reports the current tiers for
the deployment you are actually talking to, because a tier is a property of
the running server, not of the SDK version. Check it rather than assume it:

```python
if "aria" in caliber.stability.get("beta", []):
    ...  # this deployment's Aria surface may still move
```

| Tier | Promise |
| --- | --- |
| `ga` | Stable contract. A breaking change needs a MAJOR version and the deprecation cycle below. |
| `beta` | Real and supported, but the shape may still move. A breaking change can ship in a MINOR release; it will still go through the deprecation cycle below when the SDK's own methods are what's changing (as opposed to the server adding a genuinely new field). |
| `internal` | Published for route-table completeness (see the [REST API reference](../../docs-site/m-29-rest-api-reference.md)), not part of the supported SDK contract. Reachable through `client.raw`; no compatibility promise at all. |

## Deprecation window

When a public name is corrected, the old name is kept as an alias for **one
minor cycle**:

1. The new name ships. The old name becomes a thin wrapper — same behavior,
   same return value — that raises `DeprecationWarning` naming its
   replacement and the release it will be removed in.
2. The old name is removed in the named release, which is always the next
   MINOR version at the time the alias was introduced (e.g. an alias added
   in `0.1.x` names `0.2.0` as its removal release).

This is why `caliber-sdk`'s own test suite runs with `filterwarnings = ["error"]`
(`pyproject.toml`): every call site the SDK's own tests, examples, and cookbooks
make must already use the canonical name, so introducing a deprecation warning
can never silently go unnoticed inside this repository.

Current aliases and their removal release:

| Deprecated name | Canonical name | Removed in |
| --- | --- | --- |
| `CaliberClient.capabilities_api` | `CaliberClient.capabilities_info` | `0.2.0` |
| `CaliberClient.datasets` | `CaliberClient.eval_datasets` | `0.2.0` |
| `AsyncCaliberClient.capabilities_api` | `AsyncCaliberClient.capabilities_info` | `0.2.0` |

## Server / SDK compatibility

There is no compatibility *matrix* to publish yet in the traditional sense —
`caliber` (the server) and `caliber-sdk` are developed in this one repository,
released in lockstep, and both currently sit at the same pre-1.0 version
(`0.1.0.dev0`). A matrix of past version pairs would have exactly one row and
would go stale the moment either package's version scheme diverges, which is
worse than no matrix at all.

What exists instead, and is real today:

- **`GET /health`** reports the running server's exact version
  (`caliber.__version__`) — `CaliberClient.health()`.
- **`GET /capabilities`** reports the deployment's `sdk_stability` tiers —
  `client.stability` — so a script checks what *this* deployment actually
  supports instead of trusting a static document.
- **Tolerant decoding** (`models/_decode.py`) means an SDK built against a
  slightly newer or older server schema degrades — unknown fields land in
  `.extra`, missing fields fall back to defaults — rather than raising. This
  is deliberately load-bearing: it is what makes "close enough" versions
  actually interoperate without a matrix telling them to.
- **Version lockstep is tested**, not just asserted: `caliber/tests/test_version_lockstep.py`
  checks that every distribution's `pyproject.toml` version and its
  `__version__` constant agree, across `caliber`, `caliber-sdk`, `caliber-cli`,
  and `caliber-plugin-sdk`.

**Once `caliber` starts publishing independently-versioned releases** (tagged,
with a changelog), this section will be replaced with an actual matrix of
tested server-version ↔ SDK-version ranges, published here and cross-checked
by a contract test against real recorded response payloads from each —
`caliber-ui/src/test/handlers.ts`'s MSW mock fixtures are the raw material
already sitting in the repo for that.
