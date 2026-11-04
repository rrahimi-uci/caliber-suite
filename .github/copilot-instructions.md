# GitHub Copilot review instructions

Copilot may be requested as a PR reviewer. The supported development environments are
OpenAI Codex and Claude Code; their shared engineering framework is [.agents/](../.agents/README.md).
This file does not define another development workflow or command implementation.

For review, read the [canonical engineering policy](../.agents/engineering-policy.md),
linked issue acceptance criteria, complete diff, and actual validation evidence. Flag
correctness, security, compatibility, concurrency, regression and test-quality problems
with actionable file/line evidence. Do not treat an agent's self-review as independent
approval, and do not recommend bypassing required checks or repository protections.
