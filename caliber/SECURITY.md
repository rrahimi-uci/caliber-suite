# Security policy

## Supported versions

CALIBER is in pre-1.0 development. Only the latest `0.x` release receives security fixes. Once we ship `1.0`, this matrix will be updated to cover the latest two minor versions.

| Version | Supported |
| ------- | --------- |
| `0.x` (latest) | yes |
| `0.x` (older) | no |

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

CALIBER is an MLflow plugin and inherits the security posture of the MLflow server it runs in. In particular:

- **Authentication**: CALIBER does not provide its own auth layer. It composes with MLflow's auth or an upstream authenticated reverse proxy. Misconfigurations that expose MLflow without auth also expose CALIBER.
- **LLM provider API keys**: CALIBER never logs or stores plaintext API keys; secrets are referenced by URI and resolved at runtime via the secrets backend configured in deployment. If you find a path where a key leaks into logs, traces, MLflow tags, or the database, that is a P1 security issue — please report it.
- **Approval gates and audit log**: bypassing the approval gate or forging audit-log entries is in scope. Any path that promotes an artifact without a recorded human approval (outside of explicitly-documented admin override) is in scope.

Out of scope:

- Vulnerabilities in MLflow itself — report those to the [MLflow project](https://github.com/mlflow/mlflow/security).
- Vulnerabilities in LLM provider APIs.
- Social-engineering paths that do not involve a CALIBER code flaw.

## Disclosure preference

We prefer **coordinated disclosure**: 90 days from initial report unless a shorter timeline is required by active exploitation in the wild.
