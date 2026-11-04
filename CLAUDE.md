# CALIBER — Claude Code entry point

Before any work, **read [.agents/engineering-policy.md](.agents/engineering-policy.md) in full**.
It is the mandatory shared quality, testing, review, merge and post-merge policy. Follow applicable
directory instructions and the explicit assignment's scope as well.

Read [.agents/README.md](.agents/README.md) and
[project integration](.agents/github/project-integration.md). The GitHub Project and linked issues
are the authoritative planning and execution state.

Project commands under `.claude/commands/` are thin prompt adapters. Each passes `$ARGUMENTS`
to `.agents/commands/<command>.md`, which routes to one canonical workflow. Read live Project
context before repository investigation. Do not execute the whole lifecycle for one command.
The CLI supplies context and validated GitHub operations; it does not launch models or provide
an agent runtime. Use available environment tools/worktrees for delegation, or run sequentially.
