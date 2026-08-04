"""Identifier generators for CALIBER entities.

Each entity type carries a short, human-readable prefix matching the IDs used
in the UI mockups (``FB-``, ``RFN-``, ``AP-``). The suffix is the first eight
characters of a UUID4 hex — long enough to make collisions cosmically unlikely
at the scale CALIBER targets (single-org deployments, not multi-tenant SaaS),
and short enough to fit comfortably in a table cell or log line.

The prefix is part of the public API contract: it appears in URLs, audit
trails, and approval emails. Don't rename a prefix without bumping the package
major version.
"""

from __future__ import annotations

from uuid import uuid4

# Public prefixes — keep in sync with the documented ID conventions.
VERIFICATION_ITEM_PREFIX = "FB-"
REFINEMENT_JOB_PREFIX = "RFN-"
APPROVAL_PREFIX = "AP-"
CHECKPOINT_PREFIX = "CK-"
RELEASE_OPERATION_PREFIX = "REL-"
SKILL_PREFIX = "SK-"
EVAL_DATASET_PREFIX = "ED-"
EVAL_EXAMPLE_PREFIX = "EX-"
EVAL_RUN_PREFIX = "EVR-"
JUDGE_PREFIX = "JDG-"
LLM_PRICING_PREFIX = "LPRC-"
REVIEW_QUEUE_PREFIX = "RVQ-"
REVIEW_ITEM_PREFIX = "RVI-"
ARIA_PLAN_PREFIX = "PLAN-"
ARIA_PLAN_STEP_PREFIX = "PSTEP-"
ARIA_INTERACTION_PREFIX = "ASK-"
REGRESSION_RUN_PREFIX = "RR-"

# Workflow Studio prefixes (plan §14).
WORKFLOW_PREFIX = "WF-"
WORKFLOW_VERSION_PREFIX = "WFV-"
WORKFLOW_DEPLOYMENT_PREFIX = "DEP-"
TOOL_PREFIX = "TL-"
WORKFLOW_RUN_PREFIX = "WR-"
WORKFLOW_RUN_CHECKPOINT_PREFIX = "WRCK-"
RUNTIME_APPROVAL_PREFIX = "RAP-"
WORKFLOW_PATCH_PREFIX = "WP-"
WORKFLOW_PROMOTION_PREFIX = "WPR-"
WORKFLOW_BENCHMARK_REPORT_PREFIX = "WFB-"
MCP_SERVER_PREFIX = "MCP-"
WORKFLOW_SERVICE_PREFIX = "wfs"
SERVICE_TOKEN_PREFIX = "svt"  # noqa: S105 — ID prefix, not a credential
KNOWLEDGE_BASE_PREFIX = "KB-"
KNOWLEDGE_BASE_VERSION_PREFIX = "KBV-"
KNOWLEDGE_BASE_SOURCE_PREFIX = "KBS-"
KNOWLEDGE_BASE_CHUNK_PREFIX = "KBC-"
KNOWLEDGE_BASE_RUN_PREFIX = "KBR-"
KNOWLEDGE_BASE_ENTITY_PREFIX = "KBE-"
KNOWLEDGE_BASE_RELATIONSHIP_PREFIX = "KBRL-"

# Workflow file/workspace prefixes
# .
WORKFLOW_FILE_PREFIX = "FILE-"

# Project / workspace prefix.
PROJECT_PREFIX = "PRJ-"

# Ad-hoc prompt-test run prefix.
PROMPT_TEST_RUN_PREFIX = "PTR-"

# Durable tool-test run prefix (sandbox/suite/hardening runs).
TOOL_TEST_RUN_PREFIX = "TTR-"

# Durable skill-test run prefix (selection/render/scenario runs).
SKILL_TEST_RUN_PREFIX = "SKR-"

# Durable knowledge-base calibration run prefix (retrieval-quality scoring).
# Distinct from the build/ingest run prefix ``KBR-`` so the two run tables never
# collide on an ID; mirrors the ``PTR-``/``TTR-``/``SKR-`` test-run convention.
KNOWLEDGE_BASE_TEST_RUN_PREFIX = "KBTR-"

# Assistant prefixes.
ASSISTANT_SESSION_PREFIX = "ASST-"
ASSISTANT_MESSAGE_PREFIX = "AMSG-"
ASSISTANT_DRAFT_PREFIX = "ADRF-"
ASSISTANT_RUN_PREFIX = "ARN-"
ASSISTANT_PUBLISH_PREFIX = "APUB-"
ASSISTANT_ATTACHMENT_PREFIX = "AATT-"
ASSISTANT_QUEUED_MESSAGE_PREFIX = "QMSG-"
GATE_VERDICT_PREFIX = "GV-"
SKILL_VERSION_PREFIX = "SKV-"
WEBHOOK_DEAD_LETTER_PREFIX = "WDL-"

_ID_SUFFIX_LEN = 8


def _suffix() -> str:
    return uuid4().hex[:_ID_SUFFIX_LEN]


def new_item_id() -> str:
    """Return a fresh verification-queue item ID, e.g. ``FB-3a8f2c7e``."""
    return f"{VERIFICATION_ITEM_PREFIX}{_suffix()}"


def new_job_id() -> str:
    """Return a fresh refinement-job ID, e.g. ``RFN-9b1d4e0a``."""
    return f"{REFINEMENT_JOB_PREFIX}{_suffix()}"


def new_approval_id() -> str:
    """Return a fresh approval-request ID, e.g. ``AP-c2f55681``."""
    return f"{APPROVAL_PREFIX}{_suffix()}"


def new_checkpoint_id() -> str:
    """Return a fresh rollback-checkpoint ID, e.g. ``CK-44e8a1b2``."""
    return f"{CHECKPOINT_PREFIX}{_suffix()}"


def new_release_operation_id() -> str:
    """Return a fresh durable release-operation ID, e.g. ``REL-44e8a1b2``."""
    return f"{RELEASE_OPERATION_PREFIX}{_suffix()}"


def new_skill_id() -> str:
    """Return a fresh skill ID, e.g. ``SK-7f3a90c2``."""
    return f"{SKILL_PREFIX}{_suffix()}"


def new_eval_dataset_id() -> str:
    """Return a fresh eval-dataset ID, e.g. ``ED-9c2f81a3``."""
    return f"{EVAL_DATASET_PREFIX}{_suffix()}"


def new_eval_example_id() -> str:
    """Return a fresh eval-example ID, e.g. ``EX-04ba7c2e``."""
    return f"{EVAL_EXAMPLE_PREFIX}{_suffix()}"


def new_eval_run_id() -> str:
    """Return a fresh eval-run ID, e.g. ``EVR-04ba7c2e``."""
    return f"{EVAL_RUN_PREFIX}{_suffix()}"


def new_regression_run_id() -> str:
    """Return a fresh regression-run ID, e.g. ``RR-d185c2a7``."""
    return f"{REGRESSION_RUN_PREFIX}{_suffix()}"


def new_judge_id() -> str:
    """Return a fresh custom-judge ID, e.g. ``JDG-7f3a90c2``."""
    return f"{JUDGE_PREFIX}{_suffix()}"


def new_llm_pricing_id() -> str:
    """Return a fresh LLM model-pricing ID, e.g. ``LPRC-7f3a90c2``."""
    return f"{LLM_PRICING_PREFIX}{_suffix()}"


def new_review_queue_id() -> str:
    """Return a fresh review-queue ID, e.g. ``RVQ-7f3a90c2``."""
    return f"{REVIEW_QUEUE_PREFIX}{_suffix()}"


def new_review_item_id() -> str:
    """Return a fresh review-item ID, e.g. ``RVI-7f3a90c2``."""
    return f"{REVIEW_ITEM_PREFIX}{_suffix()}"


def new_aria_plan_id() -> str:
    """Return a fresh Aria plan ID, e.g. ``PLAN-7f3a90c2``."""
    return f"{ARIA_PLAN_PREFIX}{_suffix()}"


def new_aria_plan_step_id() -> str:
    """Return a fresh Aria plan-step ID, e.g. ``PSTEP-7f3a90c2``."""
    return f"{ARIA_PLAN_STEP_PREFIX}{_suffix()}"


def new_aria_interaction_id() -> str:
    """Return a fresh Aria interaction ID, e.g. ``ASK-7f3a90c2``."""
    return f"{ARIA_INTERACTION_PREFIX}{_suffix()}"


def new_workflow_id() -> str:
    """Return a fresh workflow ID, e.g. ``WF-3a8f2c7e``."""
    return f"{WORKFLOW_PREFIX}{_suffix()}"


def new_workflow_version_id() -> str:
    """Return a fresh workflow-version ID, e.g. ``WFV-9b1d4e0a``."""
    return f"{WORKFLOW_VERSION_PREFIX}{_suffix()}"


def new_workflow_deployment_id() -> str:
    """Return a fresh workflow-deployment ID, e.g. ``DEP-c2f55681``."""
    return f"{WORKFLOW_DEPLOYMENT_PREFIX}{_suffix()}"


def new_tool_id() -> str:
    """Return a fresh tool-registry ID, e.g. ``TL-44e8a1b2``."""
    return f"{TOOL_PREFIX}{_suffix()}"


def new_workflow_run_id() -> str:
    """Return a fresh workflow-run ID, e.g. ``WR-7f3a90c2``."""
    return f"{WORKFLOW_RUN_PREFIX}{_suffix()}"


def new_workflow_run_checkpoint_id() -> str:
    """Return a fresh workflow-run checkpoint ID, e.g. ``WRCK-3a8f2c7e``."""
    return f"{WORKFLOW_RUN_CHECKPOINT_PREFIX}{_suffix()}"


def new_runtime_approval_id() -> str:
    """Return a fresh runtime-approval ID, e.g. ``RAP-9b1d4e0a``."""
    return f"{RUNTIME_APPROVAL_PREFIX}{_suffix()}"


def new_workflow_patch_id() -> str:
    """Return a fresh workflow-patch ID, e.g. ``WP-04ba7c2e``."""
    return f"{WORKFLOW_PATCH_PREFIX}{_suffix()}"


def new_workflow_promotion_id() -> str:
    """Return a fresh workflow-promotion-request ID, e.g. ``WPR-1c9d2f0a``."""
    return f"{WORKFLOW_PROMOTION_PREFIX}{_suffix()}"


def new_workflow_benchmark_report_id() -> str:
    """Return a fresh workflow-benchmark-report ID, e.g. ``WFB-3a8f2c7e``."""
    return f"{WORKFLOW_BENCHMARK_REPORT_PREFIX}{_suffix()}"


def new_mcp_server_id() -> str:
    """Return a fresh MCP server ID, e.g. ``MCP-3a8f2c7e``."""
    return f"{MCP_SERVER_PREFIX}{_suffix()}"


def new_service_id() -> str:
    """Return a fresh workflow-service ID, e.g. ``wfs3a8f2c7e``."""
    return f"{WORKFLOW_SERVICE_PREFIX}{_suffix()}"


def new_service_token_id() -> str:
    """Return a fresh service-token ID, e.g. ``svt9b1d4e0a``."""
    return f"{SERVICE_TOKEN_PREFIX}{_suffix()}"


def new_gate_verdict_id() -> str:
    """Return a fresh gate-verdict ID, e.g. ``GV-9b1d4e0a``."""
    return f"{GATE_VERDICT_PREFIX}{_suffix()}"


def new_skill_version_id() -> str:
    """Return a fresh skill-version ID, e.g. ``SKV-9b1d4e0a``."""
    return f"{SKILL_VERSION_PREFIX}{_suffix()}"


def new_knowledge_base_id() -> str:
    """Return a fresh knowledge-base ID, e.g. ``KB-3a8f2c7e``."""
    return f"{KNOWLEDGE_BASE_PREFIX}{_suffix()}"


def new_knowledge_base_version_id() -> str:
    """Return a fresh knowledge-base version ID, e.g. ``KBV-9b1d4e0a``."""
    return f"{KNOWLEDGE_BASE_VERSION_PREFIX}{_suffix()}"


def new_knowledge_base_source_id() -> str:
    """Return a fresh knowledge-base source ID, e.g. ``KBS-c2f55681``."""
    return f"{KNOWLEDGE_BASE_SOURCE_PREFIX}{_suffix()}"


def new_knowledge_base_chunk_id() -> str:
    """Return a fresh knowledge-base chunk ID, e.g. ``KBC-44e8a1b2``."""
    return f"{KNOWLEDGE_BASE_CHUNK_PREFIX}{_suffix()}"


def new_knowledge_base_run_id() -> str:
    """Return a fresh knowledge-base run ID, e.g. ``KBR-7f3a90c2``."""
    return f"{KNOWLEDGE_BASE_RUN_PREFIX}{_suffix()}"


def new_knowledge_base_entity_id() -> str:
    """Return a fresh knowledge-base entity ID, e.g. ``KBE-3a8f2c7e``."""
    return f"{KNOWLEDGE_BASE_ENTITY_PREFIX}{_suffix()}"


def new_knowledge_base_relationship_id() -> str:
    """Return a fresh knowledge-base relationship ID, e.g. ``KBRL-9b1d4e0a``."""
    return f"{KNOWLEDGE_BASE_RELATIONSHIP_PREFIX}{_suffix()}"


def new_assistant_session_id() -> str:
    """Return a fresh assistant-session ID, e.g. ``ASST-3a8f2c7e``."""
    return f"{ASSISTANT_SESSION_PREFIX}{_suffix()}"


def new_assistant_message_id() -> str:
    """Return a fresh assistant-message ID, e.g. ``AMSG-9b1d4e0a``."""
    return f"{ASSISTANT_MESSAGE_PREFIX}{_suffix()}"


def new_assistant_draft_id() -> str:
    """Return a fresh assistant-draft ID, e.g. ``ADRF-c2f55681``."""
    return f"{ASSISTANT_DRAFT_PREFIX}{_suffix()}"


def new_assistant_run_id() -> str:
    """Return a fresh assistant-run ID, e.g. ``ARN-44e8a1b2``."""
    return f"{ASSISTANT_RUN_PREFIX}{_suffix()}"


def new_assistant_publish_id() -> str:
    """Return a fresh assistant-publish-event ID, e.g. ``APUB-7f3a90c2``."""
    return f"{ASSISTANT_PUBLISH_PREFIX}{_suffix()}"


def new_assistant_review_id() -> str:
    """Return a fresh assistant-review ID, e.g. ``AREV-7f3a90c2``."""
    return f"AREV-{_suffix()}"


def new_assistant_attachment_id() -> str:
    """Return a fresh assistant-attachment ID, e.g. ``AATT-7f3a90c2``."""
    return f"{ASSISTANT_ATTACHMENT_PREFIX}{_suffix()}"


def new_assistant_queued_message_id() -> str:
    """Return a fresh assistant queued-message ID, e.g. ``QMSG-7f3a90c2``."""
    return f"{ASSISTANT_QUEUED_MESSAGE_PREFIX}{_suffix()}"


def new_workflow_file_id() -> str:
    """Return a fresh workflow/dataset file ID, e.g. ``FILE-3a8f2c7e``."""
    return f"{WORKFLOW_FILE_PREFIX}{_suffix()}"


def new_project_id() -> str:
    """Return a fresh project ID, e.g. ``PRJ-3a8f2c7e``."""
    return f"{PROJECT_PREFIX}{_suffix()}"


def new_prompt_test_run_id() -> str:
    """Return a fresh prompt-test-run ID, e.g. ``PTR-3a8f2c7e``."""
    return f"{PROMPT_TEST_RUN_PREFIX}{_suffix()}"


def new_tool_test_run_id() -> str:
    """Return a fresh tool-test-run ID, e.g. ``TTR-3a8f2c7e``."""
    return f"{TOOL_TEST_RUN_PREFIX}{_suffix()}"


def new_skill_test_run_id() -> str:
    """Return a fresh skill-test-run ID, e.g. ``SKR-3a8f2c7e``."""
    return f"{SKILL_TEST_RUN_PREFIX}{_suffix()}"


def new_knowledge_base_test_run_id() -> str:
    """Return a fresh knowledge-base calibration-run ID, e.g. ``KBTR-3a8f2c7e``."""
    return f"{KNOWLEDGE_BASE_TEST_RUN_PREFIX}{_suffix()}"


def new_webhook_dead_letter_id() -> str:
    """Return a fresh webhook dead-letter ID, e.g. ``WDL-3a8f2c7e``."""
    return f"{WEBHOOK_DEAD_LETTER_PREFIX}{_suffix()}"
