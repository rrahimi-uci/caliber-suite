#!/usr/bin/env python3
"""Run one independently schedulable slice of the backend-core PR gate.

The workflow and ``scripts/ci-local.sh`` intentionally call this same runner. Keeping
the test inventory here means splitting the gate for throughput cannot make local CI
silently exercise a different set of files from GitHub Actions.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CALIBER_DIR = REPO_ROOT / "caliber"

SLICES: dict[str, tuple[str, ...]] = {
    "api": (
        "tests/test_routes_jobs_apply.py",
        "tests/test_routes_jobs_isolation.py",
        "tests/test_routes_jobs.py",
        "tests/test_job_route_gate_ratchet.py",
        "tests/test_apply_shared_sqlite_store.py",
        "tests/test_routes_prompts.py",
        "tests/test_routes_releases.py",
        "tests/test_routes_releases_scoping.py",
        "tests/test_release_traceability.py",
        "tests/test_orchestrator_eval.py",
        "tests/test_simulated_provider_guard.py",
        "tests/test_schema_readiness.py",
        "tests/test_readiness_probes.py",
        "tests/test_personal_access_tokens.py",
        "tests/test_pat_binding_gates.py",
        "tests/test_settings_routes.py",
        "tests/test_mlflow_unreachable.py",
        "tests/test_aria_honesty.py",
        "tests/test_migrate_command.py",
        "tests/test_alpha_install_contract.py",
        "tests/test_wheel_boot_script.py",
        "tests/test_extended_family_isolation.py",
        "tests/test_alpha_scope_claims.py",
        "tests/test_sdk_against_server.py",
        "tests/test_sdk_prompt_journey.py",
        "tests/test_sdk_skill_journey.py",
        "tests/test_skill_journey_doc.py",
        "tests/test_routes_tools_availability.py",
        "tests/test_tool_docs_contract.py",
        "tests/test_workflow_eval_provenance.py",
    ),
    "sdk": (
        "tests/test_workflow_component_requirements.py",
        "tests/test_workflow_docs_contract.py",
        "tests/test_knowledge_embedding_availability.py",
        "tests/test_knowledge_stack_script.py",
        "tests/test_sdk_api_coverage.py",
        "tests/test_bare_lookup_inventory.py",
        "tests/test_plugin_contract.py",
        "tests/test_feature_flags.py",
        "tests/test_trace_project_scoping.py",
        "tests/test_profile.py",
        "tests/egi_scenarios/test_downstream_fixture.py",
        "tests/test_protected_effect_inventory.py",
        "tests/test_adr_index.py",
        "tests/test_contracts_registry.py",
        "tests/test_agent_step_contract.py",
        "tests/test_agent_step_adapter.py",
        "tests/conformance/test_agent_adapter_anthropic.py",
        "tests/conformance/test_agent_adapter_openai_chat.py",
        "tests/conformance/test_agent_adapter_openai_responses.py",
        "tests/conformance/test_agent_adapter_openai_agents.py",
        "tests/test_approval_ux_contract.py",
        "tests/test_execution_role.py",
        "tests/test_readiness_execution_role.py",
        "tests/test_execution_owner.py",
        "tests/test_rule_bundle.py",
        "tests/test_run_grants.py",
        "tests/test_policy_decisions.py",
        "tests/test_trace_experiment_ownership.py",
        "tests/test_trace_redaction.py",
        "tests/test_memory_reference_scope.py",
    ),
    "governance": (
        "tests/test_routes_memory.py",
        "tests/test_secret_canary.py",
        "tests/test_observability_logging.py",
        "tests/test_run_manifest.py",
        "tests/test_run_admission.py",
        "tests/test_event_outbox.py",
        "tests/test_event_inbox.py",
        "tests/test_effect_proposals.py",
        "tests/test_effect_records.py",
        "tests/test_effect_receipts.py",
        "tests/test_span_attributes.py",
        "tests/test_workflow_run_trace_linkage.py",
        "tests/test_approval_binding.py",
        "tests/test_approval_supersession.py",
        "tests/test_routes_system_effects.py",
        "tests/test_effect_reconcile.py",
        "tests/test_governed_agent_step.py",
        "tests/test_effect_path_matrix.py",
        "tests/test_registered_tool_dispatch.py",
        "tests/test_context_sources.py",
        "tests/test_context_authorize.py",
        "tests/test_context_manifest.py",
        "tests/test_context_assembly.py",
        "tests/test_context_assembly_runtime.py",
        "tests/test_route_scope_inventory.py",
        "tests/test_conftest_parameter_guard.py",
        "tests/test_routes_openapi.py",
        "tests/test_run_pins.py",
        "tests/test_run_identity.py",
        "tests/test_policy_principal.py",
        "tests/test_worker_inventory.py",
        "tests/test_workspace_release_evaluator.py",
        "tests/test_request_limits.py",
        "tests/test_upgrade_from_alpha.py",
        "tests/test_migration_checksums.py",
    ),
}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in SLICES:
        choices = ", ".join(SLICES)
        print(
            f"usage: {Path(argv[0]).name} <slice>; choose one of: {choices}",
            file=sys.stderr,
        )
        return 2

    env = os.environ.copy()
    env.setdefault("CALIBER_REQUIRE_ALPHA_TAG", "1")
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-n",
        "auto",
        "--dist",
        "loadgroup",
        "--no-cov",
        "-q",
        *SLICES[argv[1]],
    ]
    return subprocess.run(command, cwd=CALIBER_DIR, env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
