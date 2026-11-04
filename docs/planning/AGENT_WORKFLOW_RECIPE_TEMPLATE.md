# Reusable contributor workflow recipe: {name}

Use this template only for a recurring, proven development workflow. This is a repository recipe, not a runtime CALIBER skill or an automatic grant of tool permissions. Review changes through a PR. Link the recipe from [the agent contribution playbook](AGENT_CONTRIBUTION_PLAYBOOK.md) when ready.

## Trigger and outcome

- When to use: {specific task type and repository paths}
- Expected output: {files, tests and evidence produced}
- When not to use: {adjacent task types requiring a different contract}

## Inputs and authority

- Issue/acceptance contract: {link to task or required fields}
- Producing contracts and required merged commits: {links}
- Human decisions or protected infrastructure: {what is required, and at which phase}
- Files owned and shared integration owner: {paths/role}

## Steps

1. {Exact read/inspection command and what to verify}
2. {Smallest safe change, with interface and compatibility boundary}
3. {Focused positive, denial, failure and regression test commands}
4. {Applicable CI jobs, build/docs checks and `git diff --check`}
5. {Evidence path, exact SHA, results and PR handoff}

## Failure handling and stop conditions

- {What a failed test means and where to investigate}
- {What cannot be skipped or mocked}
- {What requires owner input or coordinator intervention}

## Worked example

{One prior PR/issue with its exact commands, results and limitations. Do not present an unrun example as release evidence.}

This recipe inherits [AGENTS.md](../../AGENTS.md). It cannot override repository merge gates, issue acceptance, security controls or human decisions.
