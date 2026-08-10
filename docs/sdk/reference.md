# CALIBER Python SDK reference

Every GA surface the client exposes, what it decodes into, and the stability
tier it carries. Tiers come from the deployment itself — `client.stability`
reports what *your* server advertises, so a script can check rather than assume.

## The client

```python
from caliber_sdk import CaliberClient

with CaliberClient("https://caliber.example.com", token="calpat_...") as caliber:
    ...
```

| Argument | Default | Notes |
| --- | --- | --- |
| `base_url` | `$CALIBER_BASE_URL` | required, `http(s)` only |
| `token` | `$CALIBER_TOKEN` | personal access or session token |
| `user` | `$CALIBER_USER` | trusted-header identity; only for `trusted_header` deployments |
| `project` | `$CALIBER_PROJECT` | sent as `X-CALIBER-Project` on every request |
| `timeout` | `30.0` | seconds |
| `max_retries` | `2` | idempotent methods only |
| `verify` | `True` | TLS verification, or a CA bundle path |
| `http_client` | `None` | bring your own `httpx.Client`; the SDK will not close it |

Top-level methods: `capabilities()`, `openapi()`, `whoami()`, `health()`,
`bootstrap_csrf()`, and the `stability` property.

## Resource modules

| Attribute | Surface | Tier |
| --- | --- | --- |
| `auth.session()` | how identity was established | `ga` |
| `auth.tokens` | `list` · `create` · `revoke` · `rotate` | `ga` |
| `auth.accounts` | `list` · `create` · `update` · `revoke_sessions` | `ga` |
| `me` | `get` | `ga` |
| `capabilities_api` | `get` | `ga` |
| `settings` | `runtime` · `llm` · `update_llm` | `ga` |
| `projects` | `list` · `get` · `create` · `update` · `storage` | `ga` |
| `projects.files` | `list` · `upload` · `download` · `create_folder` · `delete` | `ga` |
| `prompts` | `list` · `get` · `create` · `versions` · `register_version` · `promote` | `ga` |
| `skills` | `list` · `get` · `create` · `update` · `render` · `test_selection` · `versions` | `ga` |
| `tools` | `list` · `get` · `register` · `update` · `calibrate` · `calibration_job(s)` · `wait_for_calibration` | `ga` |
| `workflows` | `list` · `get` · `create` · `update` · `delete` | `ga` |
| `workflows.versions` | `list` · `get` · `create` · `validate` · `compile` · `publish` | `ga` |
| `workflows.runs` | `list` · `get` · `submit` · `cancel` · `wait` | `ga` |
| `workflows.services` | `get` · `publish` · `unpublish` · `openapi` · `invoke` | `ga` |
| `datasets` | `list` · `get` · `create` · `examples` · `add_example` · `add_from_trace` | `ga` |
| `judges` | `list` · `get` · `create` · `test` · `alignment` | `ga` |
| `evaluations` | `list` · `get` · `create` · `wait` | `ga` |
| `raw` | any path under the API prefix | — |

### Paths that are not what they look like

Two surfaces are split on the server in ways the names do not suggest, and the
SDK follows the server rather than the naming:

- **Runs.** `POST /workflow-runs` submits; there is no unscoped run listing.
  `workflows.runs.list(workflow_id)` therefore requires a workflow.
- **Services.** Management lives under `/workflows/{id}/service` — publishing is
  a property of the workflow. `/services/{id}` is the *external* invocation
  surface, authenticated by per-service tokens rather than a user credential.

## Models

Dataclasses, not pydantic models — installing a client should not pull a
compiled core into every developer script for shapes dataclasses already
express.

| Module | Types |
| --- | --- |
| `models.core` | `Identity` · `SessionInfo` · `Account` · `PersonalAccessToken` · `IssuedToken` · `Capabilities` · `WorkflowRunCapabilities` · `LlmSetupStatus` · `RuntimeSettings` · `Project` · `ProjectFile` · `ProjectFolder` |
| `models.assets` | `Prompt` · `Skill` · `SkillRender` · `SkillSelection` · `SkillVersion` · `Tool` · `CalibrationJob` |
| `models.workflows` | `Workflow` · `WorkflowVersion` · `WorkflowRun` · `WorkflowService` |
| `models.quality` | `EvalDataset` · `EvalExample` · `Judge` · `Evaluation` · `JudgeAlignment` |

Every model carries an `extra` mapping holding response fields this SDK does not
model, so a newer server stays reachable without waiting for a release.

### Properties worth knowing

| Property | Answers |
| --- | --- |
| `Identity.is_anonymous` | whether the credential resolved to nobody |
| `Capabilities.is_ga(tag)` / `.tier_of(tag)` | may I rely on this surface? |
| `WorkflowRun.is_terminal` / `.succeeded` | stopped, versus stopped *well* |
| `WorkflowVersion.is_draft` | not runnable until published |
| `CalibrationJob.is_terminal` | the job stopped, whatever the score |
| `Evaluation.is_terminal` | scoring finished, whatever the result |
| `EvalDataset.is_synced` | has *ever* been pushed to MLflow — not that it is currently in sync |

That last one is deliberately narrow. `mlflow_synced_version` lags `version` the
moment a row is added, and reporting "in sync" would let a caller treat stale
evidence as current.

`IssuedToken` is a distinct type from `PersonalAccessToken` for the same class
of reason: only the issue and rotate responses carry a secret, and a listed
token has no `token` field at all rather than a null one — a null would announce
a secret in the one payload that must never mention one.

## Waiters

```python
from caliber_sdk import wait_for, wait_for_terminal_state
```

`wait_for(poll, is_done=..., timeout=..., interval=..., max_interval=...)` polls
with geometric backoff. A fixed short interval turns a slow job into thousands
of requests; a fixed long one makes a fast job feel slow.

`sleep` and `now` are injectable, so tests do not spend real seconds proving
polling behaviour.

Raises `WaitTimeout` — carrying the last observation, so a caller can report
*where* it stalled — or `WaitFailed` for a terminal failure state.

## Exceptions

All inherit `CaliberError`. See the guide for the hierarchy and for what each
one means about whether retrying could help.

`CaliberAPIError` carries `status_code`, `detail`, `method`, `url`,
`request_id`, and the decoded `payload` — so a failure in someone else's CI log
is actionable rather than "the SDK raised".
