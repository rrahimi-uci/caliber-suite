# Role adapters

The canonical role definitions are in `../../.agents/roles/` at the repository
root. The packaged prompts in `src/autocd/prompts/` add machine-readable handoff formats.
The coordinator launches one Planner, isolated Developers, and read-only
Reviewer and QA roles. `src/autocd/agents.py` is the sole model adapter.
The merge readiness check is deterministic and never merges.
