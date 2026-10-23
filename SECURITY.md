# Security Policy

## Reporting a Vulnerability

**Please do not put vulnerability details in a public issue, pull request or discussion.**

This repository does not yet publish a security mailbox, and GitHub's private vulnerability reporting
(Security, then *Report a vulnerability*) is not enabled. Until one of those exists, use this
two-step path:

1. Open a public [GitHub issue](https://github.com/rrahimi-uci/caliber-suite/issues/new/choose) titled
   `Security contact request`. Say only that you have a security report: no description, no
   proof of concept, no affected component.
2. A maintainer will reply on the issue to arrange a private channel for the details.

When you have a private channel, include:

- a description of the issue and its impact,
- steps to reproduce (a minimal proof of concept if possible),
- the affected version or commit (for the alpha, the `0.1.0a1` package version or the commit hash),
- the CALIBER and MLflow versions and how CALIBER was deployed (Docker Compose stack or wheel install).

We aim to acknowledge reports within a few business days and to provide a remediation timeline after
triage. CALIBER is alpha software maintained on a best-effort basis, so these are goals, not commitments.

## Supported Versions

CALIBER is pre-1.0 alpha software. Only the latest `main` (and, once it is published, the latest `0.1.0a*`
prerelease built from it) is supported. Pin a commit or tag for any use beyond evaluation.

## Known limitations of the alpha

The alpha's security posture and its known gaps, such as traces being readable by every signed-in user
and CSRF protection being off by default, are listed in [Alpha scope and known limitations](docs/start/alpha-scope.md#known-limitations).
Read them before exposing a deployment beyond a loopback or trusted network.
