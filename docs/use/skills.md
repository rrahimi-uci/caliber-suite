---
audience:
  - system-user
  - developer
doc_type: how-to
product_area: skills
stability: ga
prerequisites:
  - A CALIBER deployment with skill access
reviewed_on: 2026-08-10
version_applicability: current main branch docs contract
tags:
  - skills
  - packaging
  - prompts
  - assistant
---

# Skills

Use this page for the practical path around skills: package them, test their
selection behavior, and understand how they shape assistant or workflow output.

## At a glance

| Task | Start here | Deep reference |
| --- | --- | --- |
| package a reusable instruction asset | define the skill surface and tests | [Skills architecture](../04-skills/architecture.md) |
| verify the right skill is selected | run trigger/selection coverage | [Skills architecture](../04-skills/architecture.md) |
| use a skill with Aria | inspect the assistant flow | [Aria assistant](../use/aria-assistant.md) |
| use a skill in examples | follow a cookbook or SDK recipe | [SDK recipes](../sdk/cookbooks.md) |

## 1. What skills are for in CALIBER

Skills are reusable instruction assets. They let you keep repeatable behavioral
guidance separate from the rest of the workflow or assistant logic.

## 2. Common tasks

| You want to... | Read this next |
| --- | --- |
| author or update a skill | [Skills architecture](../04-skills/architecture.md) |
| verify trigger behavior | [Skills architecture](../04-skills/architecture.md) |
| understand how the assistant chooses or applies a skill | [Aria assistant](../use/aria-assistant.md) |
| package skill logic for developer workflows | [SDK recipes](../sdk/cookbooks.md) |

## 3. Common failure modes

| Symptom | First thing to check |
| --- | --- |
| The skill exists but is not used | trigger cases or selection conditions are too weak |
| A skill package is installed but not active | installation and enablement are separate steps |
| Output changed after calibration | inspect the evidence loop and packaged instructions together |

## 4. Related docs

- [Aria assistant](../use/aria-assistant.md)
- [Trust and governance](../use/trust-and-governance.md)
- [Skills architecture](../04-skills/architecture.md)
- [SDK recipes](../sdk/cookbooks.md)
