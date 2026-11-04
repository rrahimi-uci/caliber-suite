# CALIBER — Codex entry point

Before any work, **read [.agents/engineering-policy.md](.agents/engineering-policy.md) in full**.
It is the mandatory shared quality, testing, review, merge and post-merge policy. Follow applicable
directory instructions and the explicit assignment's scope as well.

Read [.agents/README.md](.agents/README.md) for routing and
[project integration](.agents/github/project-integration.md) for live GitHub context.
The GitHub Project and linked issues are the authoritative planning and execution state.

Treat `/architect`, `/plan`, `/develop`, `/review`, `/critique`, `/bug-bash` and `/release-check`
followed by a target as repository instruction conventions. They are **not registered native
Codex slash commands**. A plain request such as “Run develop for <target> using .agents/commands/develop.md”
has the same meaning. Read the matching `.agents/commands/<command>.md` and follow its one canonical
workflow; read live Project context before repository investigation. Do not execute the entire
lifecycle merely because a single command was requested.

The CLI supplies context and validated GitHub operations; it does not launch models. Use available
environment tools and isolated worktrees for delegated work, or execute sequentially.

## Code Review Rules

Read the shared policy and [.agents/roles/reviewer.md](.agents/roles/reviewer.md). Flag unmet
acceptance criteria, unsafe behavior, missing meaningful tests and unsupported evidence claims.
Do not count self-review as independent approval or skipped required checks as passing.
