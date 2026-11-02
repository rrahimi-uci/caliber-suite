---
audience:
  - system-user
  - developer
  - operator
doc_type: how-to
product_area: openapi-integrations
stability: beta
prerequisites:
  - A CALIBER deployment with admin or operator scope and CALIBER_FEATURE_INTEGRATIONS=true
reviewed_on: 2026-10-15
version_applicability: current main branch docs contract
tags:
  - openapi
  - integrations
  - tools
  - governance
---

# OpenAPI Integrations

> **Disabled by default.** OpenAPI integrations are a preview area and are off by default; enable with
> `CALIBER_FEATURE_INTEGRATIONS=true` and restart CALIBER (the same flag turns on MCP servers). While it is off,
> the `/openapi-integrations*` routes answer 404 (`reason_code: feature_disabled`) and the OpenAPI
> Integrations page is hidden. Tools already published from an integration stay in the tool registry. Enabling
> the flag means you accept preview behavior. See [Feature flags](../start/alpha-scope.md#feature-flags).
> The Beta does not change that: `GET /capabilities` reports `adapters.openapi_integrations` as `preview` and never `supported`;
> only the trusted document-request connector and release evaluation can be, in the reference profile. See
> [Capability exposure](../operate/reference-profile.md#capability-exposure-what-is-supported-b15-support1-755).

Use this page when the practical question is how to bring a third-party
OpenAPI spec into CALIBER, decide which operations should become agent-facing
tools, and publish them safely.

## At a glance

| Task | Start here | Deep reference |
| --- | --- | --- |
| import a spec | paste, upload, or fetch from a URL | [Architecture](../16-openapi-integrations/architecture.md#4-api-surface) |
| review what was found | check operations and detected dependencies | [Architecture §3](../16-openapi-integrations/architecture.md#3-domain-model) |
| curate tool drafts | select operations, set auth, generate drafts | [Architecture §1](../16-openapi-integrations/architecture.md#1-scope-and-responsibilities) |
| publish and use in a workflow | publish a ready draft, bind it like any tool | [Workflows](workflows.md), [Tools](tools.md) |
| reach it from Aria | use the stable bridge or the dynamically projected published tool, subject to its side-effect tier and approval gate | [Architecture §6](../16-openapi-integrations/architecture.md#6-agent-and-workflow-projection) |

## 1. The five-step flow

1. **Create the integration shell** — `POST /openapi-integrations` (or
   `caliber.openapi_integrations.create(name)`). This is just an identity; no
   spec exists yet.
2. **Import a version** — paste JSON/YAML, upload a file, or supply a URL.
   A URL fetch goes through the same guarded egress every tool execution
   does, so an internal-only or blocked host is refused before any request is
   sent. Re-importing the identical document is rejected with a pointer to
   the existing version rather than silently duplicating it.
3. **Review operations and dependencies** — every operation is classified
   (read/write/external_action, pagination style, async-job shape) and
   deterministic dependency detection runs automatically. High-confidence
   rows (an explicit OpenAPI `link`) are auto-wired; everything else is a
   suggestion or an advisory hint an operator can confirm or reject.
4. **Generate tool drafts** — select operations by id, or by
   `tags`/`methods`/`path_prefix` filter for a large spec. Binding several
   related operations (list + get + create for one resource, say) into one
   tool pack keeps a curated spec from producing dozens of near-identical
   tools.
5. **Publish** — a ready draft (server URL set, auth bound if the operation
   requires it, every secret reference resolvable) becomes a normal
   `CaliberToolRegistry` row. From that point it behaves exactly like a
   hand-registered tool: it shows up in the tool registry, can be bound into
   a workflow, and is reachable through `client.tools`.

Publishing is project-aware. The caller must have `resource.publish` in the
selected project's effective permissions, which is granted to the project
`owner` and `editor` roles, and the global `caliber.operator` scope. An
organization-wide publish still requires the global admin scope. Creating an
integration, importing a version, reviewing a dependency and generating or
editing drafts need `owner` or `editor` in the project, and a preview needs
`reviewer`, `editor` or `owner`; a member with too weak a role gets 403 and a
person outside the project gets 404. A successful publish returns the published tool alongside
the updated draft, so the UI/SDK can show what happened immediately rather than
leaving the draft in an ambiguous state.

## 2. Common tasks

| You want to... | Read this next |
| --- | --- |
| import from a URL instead of pasting | [Architecture §4](../16-openapi-integrations/architecture.md#4-api-surface) — `POST .../validate-spec-source` checks reachability first |
| see what changed since the last import | `POST .../reimport` (url sources only), or `POST .../versions/{id}/diff` |
| bundle several operations behind one tool | `generate_tool_drafts(..., group_as_pack=True)` |
| test a draft before publishing | `preview_tool_draft()` — refused unless `allow_in_preview` is set; in the reference profile it previews only a read operation (`GET`, `HEAD`, `OPTIONS`), see below |
| understand why a dependency is only advisory | [Architecture §7](../16-openapi-integrations/architecture.md#7-security-and-trust-boundaries) — only deterministic rules auto-wire |
| call a published tool from an Aria session | use `openapi_tool.list` / `openapi_tool.invoke` for the stable bridge (`invoke` only runs read-only tools that do not need approval), or call the dynamically projected published tool directly (only read tools are projected). Only a tool whose operation reads (`GET`, `HEAD`, `OPTIONS`) runs from Aria: a write is refused with `unsupported_effect_path` and nothing is sent (a write goes through a workflow tool node, which records an approvable proposal) |

## 3. Common failure modes

| Symptom | First thing to check |
| --- | --- |
| There is no OpenAPI Integrations page, or a call answers 404 with `reason_code: feature_disabled` | `CALIBER_FEATURE_INTEGRATIONS` is off (the default); set it to `true` and restart |
| Import from a URL fails immediately | the host is blocked by egress policy (loopback, link-local, private range) — allowlist it if intentional |
| A draft won't publish | its server URL is unset, or the operation requires auth and no binding is set, or a bound secret reference does not resolve |
| A draft is ready but publish is refused | check the selected project's `access_role`/`permissions`; `owner` or `editor` is required for project publication |
| `preview` returns 409 | the draft's `allow_in_preview` flag is off — set it explicitly, since preview performs a real upstream call |
| `preview` returns 409 with `reason_code: unsupported_effect_path` | the server runs the reference profile, where preview is read-only and your draft's operation is not a `GET`, `HEAD` or `OPTIONS`. Nothing was sent. Publish the tool and call it from a workflow tool node, which records an approvable proposal; see [Draft preview is read-only](../operate/reference-profile.md#draft-preview-is-read-only-1072) |
| A dependency never shows up as canonical | it was a low-confidence, same-tag-only signal — advisory hints require operator confirmation, they never auto-wire |
| A published tool is refused in a workflow: `OpenAPI effect proposal refused: <code>` | the call is protected (any method but `GET`, `HEAD` or `OPTIONS`, or a tool published as `write` or `external_action`) and failed a check before an approval was requested: `openapi_egress_blocked` (the server or OAuth token URL is not allowed by the egress policy), `openapi_input_invalid` (the input breaks the tool's schema), `openapi_key_override_refused` (do not send an idempotency key: the platform sends its own), `openapi_precondition_conflict` (`expected_resource_version` and the `If-Match` header disagree). Nothing was sent. See [Governed OpenAPI tools](../operate/reference-profile.md#governed-openapi-tools-b044) |
| A published write tool is `unknown` after the downstream timed out | the request left and the answer never came, so CALIBER does not resend it: settle it with the receipt verifier, a receipt callback or an operator, not by running the node again |
| A tool works from a workflow but not from Aria | check whether it is approval-gated or whether the current Aria mode hides its side-effect tier. A `read` tool that does not need approval is projected in every mode. A `write` / `external_action` tool is not projected in any mode (the public executor refuses a protected method, so the call could only fail). A tool that requires approval is never projected as a direct Aria tool. A tool published as `read` whose operation is a `POST` is projected but answers `unsupported_effect_path` when called (the public executor refuses a protected method) |

## 4. Related docs

- [OpenAPI Integrations architecture](../16-openapi-integrations/architecture.md)
- [Tools](tools.md)
- [Workflows](workflows.md)
- [Trust and governance](trust-and-governance.md)
- [SDK beta surfaces](../sdk/beta.md)
