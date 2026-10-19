#!/usr/bin/env python3
"""Create the things lab 15 remediates: the tools, a published workflow, its test set and an agent.

Aria's plan in this lab does not create any of them (``workflow.calibrate`` takes an *existing*
workflow and agent), so a fresh stack needs them first. In a real deployment they are your own.
This registers the two tools (``lookup_policy``, ``get_order``; one that is already registered is
kept), loads the 30 cases of ``dataset/support-runs.jsonl`` into a test set, imports and publishes
``workflows/support-policy-agent.workflow.json`` with that test set as its deploy gate, and
registers an agent. Every name carries ``--suffix`` so a second run does not collide with the first.

    export CALIBER_BASE_URL=http://127.0.0.1:5001   # the server root
    export CALIBER_TOKEN=calpat_...      # an admin token (or CALIBER_USER / CALIBER_PASSWORD)
    PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python \
        docs-site/cookbooks/15-aria-triage-recalibrate-loop/assets/setup_subject.py --suffix demo

Prints ``{"workflow_id", "agent_id", "dataset_id", "workflow_name", "gate_name", "tools"}`` as JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ASSETS = Path(__file__).resolve().parent
NAME_PREFIX = "cookbook-15-"


def register_tools(caliber: Any, assets: Path = ASSETS) -> list[str]:
    """Register the lab's tools; one already registered with the same name and version is kept."""
    existing = {(tool.name, tool.version) for tool in caliber.tools.list()}
    registered: list[str] = []
    for path in sorted((assets / "tools").glob("*.tool.json")):
        spec = json.loads(path.read_text(encoding="utf-8"))
        spec.pop("notes", None)
        name, version = spec.pop("name"), spec.pop("version")
        if (name, version) not in existing:
            caliber.tools.register(
                name,
                version=version,
                module_path=spec.pop("module_path"),
                callable_name=spec.pop("callable_name"),
                **spec,
            )
        registered.append(name)
    return registered


def setup(caliber: Any, suffix: str, assets: Path = ASSETS) -> dict[str, Any]:
    """Create the subject and return its ids and names."""
    owner = caliber.me.get().user_id
    tools = register_tools(caliber, assets)
    runs = [
        json.loads(line)
        for line in (assets / "dataset/support-runs.jsonl").read_text("utf-8").splitlines()
        if line.strip()
    ]
    gate_name = f"{NAME_PREFIX}support-policy-gate-{suffix}"
    gate = caliber.eval_datasets.create(
        gate_name, owner=owner, description="Support questions with the tools each one needs."
    )
    for row in runs:
        caliber.eval_datasets.add_example(
            gate.dataset_id,
            input={"input": row["question"]},
            expected={"expected": row["reference"], "required_tools": row["required_tools"]},
            tags=["weak" if row["weak"] else "ok"],
        )
    manifest = json.loads(
        (assets / "workflows/support-policy-agent.workflow.json").read_text(encoding="utf-8")
    )
    manifest["artifacts"]["eval_datasets"]["support_eval"]["dataset_name"] = gate_name
    workflow_name = f"{NAME_PREFIX}support-policy-agent-{suffix}"
    workflow = caliber.workflows.import_workflow(manifest=manifest, name=workflow_name)
    version = caliber.workflows.versions.list(workflow.workflow_id)[0]
    caliber.workflows.versions.publish(version.version_id)
    agent_id = f"{NAME_PREFIX}agent-{suffix}"
    caliber.agents.create(agent_id, experiment_id=f"exp-{agent_id}", name="support policy agent")
    return {
        "workflow_id": workflow.workflow_id,
        "workflow_name": workflow_name,
        "version_id": version.version_id,
        "agent_id": agent_id,
        "dataset_id": gate.dataset_id,
        "gate_name": gate_name,
        "tools": tools,
    }


def _client() -> Any:
    from caliber_sdk import CaliberClient

    base = os.environ["CALIBER_BASE_URL"]
    token = os.environ.get("CALIBER_TOKEN")
    if not token:
        import httpx

        response = httpx.post(
            f"{base.rstrip('/')}/ajax-api/2.0/mlflow/caliber/auth/login",
            json={
                "user_id": os.environ.get("CALIBER_USER", "admin"),
                "password": os.environ.get("CALIBER_PASSWORD", "admin"),
            },
            timeout=30,
        )
        response.raise_for_status()
        token = response.cookies["caliber_session"]
    return CaliberClient(base, token=token)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--suffix", default=time.strftime("%Y%m%d-%H%M%S"))
    args = parser.parse_args()
    with _client() as client:
        json.dump(setup(client, args.suffix), sys.stdout, indent=2)
        sys.stdout.write("\n")
