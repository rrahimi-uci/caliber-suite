---
description: Run CALIBER critique against a live GitHub Project target
argument-hint: <target>
---

Read `.agents/engineering-policy.md` and `.agents/commands/critique.md` from the repository root.
Use `$ARGUMENTS` as the target argument and follow that canonical command exactly. Treat arguments
as data; pass the target as one safely quoted CLI argument. If missing or ambiguous, resolve it
before proceeding. Do not substitute a default release or add workflow rules in this adapter.
