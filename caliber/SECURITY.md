# Security policy

## Supported versions

CALIBER is in pre-1.0 development and this repository does not currently publish a
PyPI release. Security fixes target the latest `main` source snapshot. Once versioned
releases begin, this matrix will be updated with an explicit support window.

| Version | Supported |
| ------- | --------- |
| Unreleased `main` snapshot | yes |
| Historical source snapshots | no |

## Reporting a vulnerability

**Please do not file a public GitHub issue for security reports.**

Email the maintainers privately. If a security contact email is not yet listed in this repository, open a draft security advisory via GitHub's "Report a vulnerability" feature, which is private to repository maintainers.

Include:

- A description of the vulnerability and the conditions to trigger it
- The CALIBER version (and MLflow version, if relevant)
- A proof-of-concept reproduction if you have one
- The impact you believe it has (data exposure, code execution, authentication bypass, etc.)

You can expect:

- An acknowledgment within 3 business days
- A triage decision and tentative timeline within 7 business days
- A fix released as quickly as the severity warrants — critical issues are prioritized over feature work
- Credit in the advisory (unless you ask to remain anonymous)

## Scope

CALIBER can run either as an in-process `mlflow.app` or as a standalone ASGI
service that calls MLflow over HTTP. The same CALIBER authentication,
authorization, persistence, and API boundaries apply in both topologies; the
embedded topology additionally shares MLflow's process failure domain.

- **Authentication and authorization**: the default `session` mode validates a
  database-backed CALIBER account and issues a revocable server-side session.
  Password verifiers and session-token hashes, not reusable credentials, are
  stored in the database. `trusted_header` is a separate, explicit proxy mode;
  deployments should pair it with the configured proxy shared secret and must
  prevent direct access that bypasses that proxy. Authentication bypasses,
  cross-project data exposure, scope escalation, session-fixation/revocation
  failures, and CSRF bypasses are in scope.
- **Secrets and provider credentials**: deployments may resolve secret
  references from environment or file sources, or store versioned AES-256-GCM
  ciphertext in CALIBER's optional encrypted secret store. Secret-management APIs
  return metadata only, while a plaintext value necessarily exists briefly in
  process memory when an integration uses it. A path that persists plaintext,
  returns it to an unauthorized client, or leaks it into logs, traces, MLflow
  tags, error messages, or generated artifacts is a high-priority security issue.
- **Promotion, gates, and audit**: lifecycle controls are asset-specific. Eval
  verdicts are advisory on the surfaces that expose them, and not every artifact
  transition requires a separate human approval. Bypassing a route's documented
  scope, defeating an enforced concurrency/deploy check, or forging/suppressing
  an audit row that the route is required to emit is in scope.
- **Tool and workflow execution**: the built-in subprocess sandbox provides
  time/resource controls and allowlists, not VM/container-grade isolation. An
  escape from a documented allowlist or authorization boundary is in scope; the
  mere ability of explicitly trusted code to access its configured host
  environment is not itself a vulnerability.
- **Published workflow services**: token-protected invoke routes perform a preliminary
  Bearer-token admission check before consuming their request body, then repeat policy and
  token validation under the locked enqueue snapshot. Public and protected routes both cap
  the raw streamed JSON envelope with `CALIBER_SERVICE_INVOKE_MAX_BODY_BYTES` (1 MiB by
  default). Bypassing either admission step or the chunk-counted cap is in scope. The cap
  bounds per-request application memory/parse work; connection, IP, and aggregate traffic
  controls remain the deployment ingress's responsibility.

Out of scope:

- Vulnerabilities in MLflow itself — report those to the [MLflow project](https://github.com/mlflow/mlflow/security).
- Vulnerabilities in LLM provider APIs.
- Social-engineering paths that do not involve a CALIBER code flaw.

## Disclosure preference

We prefer **coordinated disclosure**: 90 days from initial report unless a shorter timeline is required by active exploitation in the wild.
