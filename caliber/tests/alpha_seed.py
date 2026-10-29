"""A deterministic, offline ``caliber-v0.1.0a1`` database with representative alpha data (B16-Upgrade.2, #708).

Not collected by pytest (no ``test_`` prefix). ``tests/test_upgrade_from_alpha.py`` uses it, and the upgrade test
that follows (B16-Upgrade.3, #715) consumes it: it takes a database an alpha operator could really have, runs the
migrations after the alpha head, and asserts that nothing was lost or rewritten.

What "alpha-shaped" means here
------------------------------
* **Schema.** The database is created by ``alembic upgrade <alpha head>`` through the working-tree migrations, with
  the alpha head read from the checksum manifest (``tests/migration_checksums.py``), never written in by hand. That
  reproduces the alpha schema because every migration up to that head is byte-identical to the tag, which
  ``verify_tree`` re-checks here before any migration runs (the tag itself is never read: it is absent from a CI
  checkout). A database that is not exactly at the alpha head is refused, loudly (:class:`AlphaSeedError`).
* **Data.** The rows are plain SQL ``INSERT`` statements built from literals in this file. They use the alpha
  table and column names declared below, not ``caliber.db.models`` (the current ORM), so a later model change
  cannot silently change what the alpha "contained". Every row lists every column of its alpha table, so the
  database never invents a value (no ``CURRENT_TIMESTAMP`` defaults): reading the rows back gives exactly the
  seed, on SQLite and on PostgreSQL. Timestamps, ids, hashes, nonces and ciphertext are fixed constants.
* **Content.** One operator-run organization: three users (one disabled), three projects (one archived) with their
  dev/qa/staging/prod environments, project members, a session and access tokens, two workflows with published and
  draft versions and deployments, nine workflow runs covering every status an upgrade can meet (including a run
  that was ``running`` and one ``waiting_approval`` when the operator stopped the alpha), their events and
  checkpoints, runtime approvals, effect-ledger rows ``in_progress`` / ``completed`` / ``failed``, a workflow
  service and its token, secrets (ciphertext only) and the tool that references one, a skill, an eval dataset and
  run, a prompt refinement loop (prompt *text* lives in MLflow's registry, not in this database: only the loop
  around it does), prompt test runs, audit rows and a few runtime tables. Nothing here is a real credential:
  every hash and ciphertext is derived from a public constant.

What it deliberately leaves empty: the workspace/GitHub source tables, the knowledge-base, OpenAPI, MCP, Aria and
release-candidate tables. They are populated only when an operator enables those preview features, and the
upgrade assertions do not need them. Add rows here, never new helpers elsewhere, when a later task needs more.

Interface for the upgrade test (#715)
-------------------------------------
``alpha_database(dialect, tmp_path, name)`` yields an :class:`AlphaDatabase` (``url``, ``head``, ``version_table``)
already created and seeded. Upgrade it with ``migrate_database(url)``; then compare ``read_seeded_rows(url)`` with
``expected_rows()`` table by table (a table the upgrade legitimately rewrites needs its own assertion), use
``row_counts()`` for the counts and ``schema_shape(url)`` to compare the upgraded schema with a fresh install.
"""

from __future__ import annotations

import hashlib
import json
import os
import warnings
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from operator import itemgetter
from pathlib import Path
from typing import Any
from unittest import mock

from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SAWarning

from caliber.db.migrate import build_config
from caliber.db_url import normalize_database_url
from tests.dialect_helpers import dialect_database_url
from tests.migration_checksums import head_revision, load_manifest, verify_tree


class AlphaSeedError(RuntimeError):
    """The database is not an unseeded ``caliber-v0.1.0a1`` database, or the frozen migrations changed."""


# --------------------------------------------------------------------------------------------------------------
# Alpha data
# --------------------------------------------------------------------------------------------------------------

#: A row value. ``dict``/``list`` are JSON columns, ``bytes`` are binary columns, ``bool`` are boolean columns.
Value = str | int | float | bool | bytes | dict[str, Any] | list[Any] | None
Row = dict[str, Value]


@dataclass(frozen=True)
class SeedTable:
    """One alpha table and the rows seeded into it.

    ``columns`` is the alpha table's full column list (the alpha shape), including the autoincrement column when
    there is one; ``rows`` never carry that column, the database assigns it (1, 2, 3 ... in insertion order, which
    is what :func:`expected_rows` reports). ``pk`` orders reads.
    """

    name: str
    columns: tuple[str, ...]
    pk: str
    rows: tuple[Row, ...]
    autoincrement: bool = False


def _ts(day: int, hour: int = 9, minute: int = 0, second: int = 0) -> str:
    """A timestamp in October 2026, in the text form SQLAlchemy writes to SQLite (microseconds included)."""
    return f"2026-10-{day:02d} {hour:02d}:{minute:02d}:{second:02d}.000000"


def _digest(label: str) -> str:
    """A 64-hex fingerprint of a public constant: stands in for token and manifest hashes."""
    return hashlib.sha256(f"alpha-seed:{label}".encode()).hexdigest()


def _blob(label: str, size: int) -> bytes:
    return hashlib.sha256(f"alpha-seed:{label}".encode()).digest()[:size]


#: The login phrase of every seeded user (the stored value is a scrypt hash, in the alpha's own format).
ALPHA_SEED_PASSWORD = "alpha seed login phrase"
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _SCRYPT_DKLEN = 2**15, 8, 1, 64


@cache
def _password_hash() -> str:
    """``scrypt$n$r$p$salt$hash`` with the alpha's work factors and a fixed salt (so it is the same every run)."""
    salt = _blob("password-salt", 16)
    derived = hashlib.scrypt(
        ALPHA_SEED_PASSWORD.encode(),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_N * _SCRYPT_R * 200,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${derived.hex()}"


def _table(
    name: str,
    columns: str,
    rows: Sequence[Mapping[str, Value]],
    *,
    defaults: Mapping[str, Value] | None = None,
    pk: str,
    autoincrement: bool = False,
) -> SeedTable:
    """Build an alpha table from its frozen columns and explicit neutral defaults."""
    column_names = tuple(columns.split())
    base: dict[str, Value] = dict.fromkeys(column_names)
    if len(column_names) != len(set(column_names)):
        raise AlphaSeedError(f"{name}: alpha column list contains duplicates")
    defaults = dict(defaults or {})
    unknown_defaults = set(defaults) - set(column_names)
    if unknown_defaults:
        raise AlphaSeedError(
            f"{name}: defaults use columns the alpha table does not have: {sorted(unknown_defaults)}"
        )
    base.update(defaults)
    unknown = {key for row in rows for key in row} - set(column_names)
    if unknown:
        raise AlphaSeedError(
            f"{name}: seed rows use columns the alpha table does not have: {sorted(unknown)}"
        )
    if not autoincrement and pk not in column_names:
        raise AlphaSeedError(f"{name}: primary key {pk!r} is not in the alpha columns")
    table_columns = (pk, *column_names) if autoincrement else column_names
    return SeedTable(
        name=name,
        columns=table_columns,
        pk=pk,
        rows=tuple({**base, **row} for row in rows),
        autoincrement=autoincrement,
    )


def _manifest(workflow: str, *, send_effect: bool = True) -> dict[str, Any]:
    """An alpha workflow manifest (schema 1): start -> agent -> [approval] -> tool -> output."""
    nodes: dict[str, Any] = {
        "start": {"id": "start", "type": "start"},
        "triage": {"id": "triage", "type": "agent", "model": "alpha-scripted"},
        "final": {"id": "final", "type": "output"},
    }
    edges = [{"id": "e1", "from": "start", "to": "triage", "map": {"msg": "input"}}]
    last = "triage"
    if send_effect:
        nodes["approve_refund"] = {
            "id": "approve_refund",
            "type": "approval",
            "policy": {"approval_count": 1},
        }
        nodes["send_refund"] = {
            "id": "send_refund",
            "type": "tool",
            "tool": {"name": "issue_refund", "version": "1.0"},
            "secret_refs": ["support_api_credentials"],
        }
        edges += [
            {"id": "e2", "from": "triage", "to": "approve_refund", "map": {}},
            {"id": "e3", "from": "approve_refund", "to": "send_refund", "map": {}},
        ]
        last = "send_refund"
    edges.append({"id": "e9", "from": last, "to": "final", "map": {"final_output": "response"}})
    return {
        "schema_version": 1,
        "workflow_id": workflow,
        "name": workflow,
        "runtime": {
            "sdk": "openai-agents-python",
            "compiler_version": "caliber-workflow-compiler-v1",
        },
        "nodes": nodes,
        "edges": edges,
    }


def _canonical(value: Any) -> str:
    """The JSON text stored for a JSON column, and the form read-backs are normalised to."""
    return json.dumps(value, sort_keys=True)


_ENVIRONMENTS = (
    ("dev", "development", 10, "active"),
    ("qa", "qa", 20, "disabled"),
    ("staging", "staging", 30, "disabled"),
    ("prod", "production", 40, "disabled"),
)

_ALICE, _BOB, _CAROL = "alice@example.test", "bob@example.test", "carol@example.test"
_WF_REFUND, _WF_INVOICE, _WF_LEGACY = "WF-A0000001", "WF-A0000002", "WF-A0000003"
_V_REFUND_1, _V_REFUND_2, _V_REFUND_3 = "WFV-A0000001", "WFV-A0000002", "WFV-A0000003"
_V_INVOICE_1, _V_LEGACY_1 = "WFV-A0000004", "WFV-A0000005"
_PRJ_SUPPORT, _PRJ_BILLING, _PRJ_PILOT = "PRJ-A0000001", "PRJ-A0000002", "PRJ-A0000003"

#: (run id, workflow, version, status, project) in the order they were launched.
_RUNS = (
    ("WR-A0000001", _WF_REFUND, _V_REFUND_1, "completed", _PRJ_SUPPORT),
    ("WR-A0000002", _WF_REFUND, _V_REFUND_2, "completed", _PRJ_SUPPORT),
    ("WR-A0000003", _WF_REFUND, _V_REFUND_2, "failed", _PRJ_SUPPORT),
    ("WR-A0000004", _WF_REFUND, _V_REFUND_2, "running", _PRJ_SUPPORT),
    ("WR-A0000005", _WF_REFUND, _V_REFUND_2, "waiting_approval", _PRJ_SUPPORT),
    ("WR-A0000006", _WF_INVOICE, _V_INVOICE_1, "completed", _PRJ_BILLING),
    ("WR-A0000007", _WF_INVOICE, _V_INVOICE_1, "queued", _PRJ_BILLING),
    ("WR-A0000008", _WF_REFUND, _V_REFUND_2, "cancelled", _PRJ_SUPPORT),
    ("WR-A0000009", _WF_REFUND, _V_REFUND_2, "completed", _PRJ_SUPPORT),
)


def _effect_key(run: str, node: str) -> str:
    """Same shape as the alpha's keys (``eff-`` plus 56 hex digits), derived from a public constant."""
    return f"eff-{hashlib.sha256(f'alpha-seed:{run}:{node}'.encode()).hexdigest()[:56]}"


def _run_events() -> list[Row]:
    """The events of each run, sequence numbered from 1; the autoincrement ``event_id`` is the database's."""
    rows: list[Row] = []
    for index, (run_id, _wf, _version, status, project) in enumerate(_RUNS):
        plan: list[tuple[str, str, dict[str, Any]]] = [
            ("run_started", "start", {"status": "queued"}),
            ("node_completed", "triage", {"status": "completed"}),
        ]
        if status == "waiting_approval":
            plan.append(("approval_requested", "approve_refund", {"status": "pending"}))
        elif status == "completed":
            plan.extend(
                [
                    ("approval_granted", "approve_refund", {"status": "approved"}),
                    ("effect_completed", "send_refund", {"status": "completed"}),
                ]
            )
        elif status == "failed":
            plan.append(("effect_failed", "send_refund", {"error_code": "tool_error"}))
        elif status in {"running", "cancelled"}:
            plan.append(("effect_in_progress", "send_refund", {"status": "in_progress"}))
        plan.append(
            (
                "run_finished" if status in {"completed", "failed", "cancelled"} else "run_open",
                "final",
                {"status": status},
            )
        )
        for sequence, (event_type, node, payload) in enumerate(plan, start=1):
            rows.append(
                {
                    "workflow_run_id": run_id,
                    "project_id": project,
                    "sequence": sequence,
                    "event_type": event_type,
                    "node_id": node,
                    "payload": payload,
                    "created_at": _ts(14 + index, 10, sequence),
                }
            )
    return rows


def _build_seed() -> tuple[SeedTable, ...]:  # noqa: PLR0915 - one ordered, reviewable fixture definition
    """The whole seed, in foreign-key order (parents first)."""
    refund_manifest = _manifest("refund-triage")
    invoice_manifest = _manifest("invoice-dispute", send_effect=False)
    legacy_manifest = _manifest("legacy-experiment", send_effect=False)
    tables: list[SeedTable] = []

    tables.append(
        _table(
            "caliber_user_accounts",
            "user_id password_hash disabled created_by created_at password_updated_at last_login_at",
            [
                {"user_id": _ALICE, "created_by": "bootstrap", "last_login_at": _ts(20, 8, 30)},
                {
                    "user_id": _BOB,
                    "created_by": _ALICE,
                    "last_login_at": _ts(19, 15, 5),
                    "created_at": _ts(13),
                },
                {
                    "user_id": _CAROL,
                    "created_by": _ALICE,
                    "disabled": True,
                    "created_at": _ts(13),
                    "password_updated_at": _ts(15),
                },
            ],
            defaults={
                "password_hash": _password_hash(),
                "disabled": False,
                "created_by": "system",
                "created_at": _ts(12),
            },
            pk="user_id",
        )
    )

    tables.append(
        _table(
            "caliber_projects",
            "project_id tenant_id name description owner status created_at updated_at storage_backend slug source_mode archived_at archived_by accepted_revision_id next_revision_number",
            [
                {
                    "project_id": _PRJ_SUPPORT,
                    "name": "Customer Support",
                    "slug": "customer-support",
                    "description": "Refund and escalation automation.",
                },
                {
                    "project_id": _PRJ_BILLING,
                    "name": "Billing Operations",
                    "slug": "billing-operations",
                    "owner": _BOB,
                    "created_at": _ts(13),
                    "updated_at": _ts(13),
                },
                {
                    "project_id": _PRJ_PILOT,
                    "name": "Archived Pilot",
                    "slug": "archived-pilot",
                    "status": "archived",
                    "archived_at": _ts(18),
                    "archived_by": _ALICE,
                    "updated_at": _ts(18),
                },
            ],
            defaults={
                "tenant_id": "local",
                "description": "",
                "owner": _ALICE,
                "status": "active",
                "created_at": _ts(12),
                "updated_at": _ts(12),
                "storage_backend": "local",
                "slug": "",
                "source_mode": "caliber_managed",
                "next_revision_number": 1,
            },
            pk="project_id",
        )
    )

    environment_rows: list[Row] = []
    for number, project in enumerate((_PRJ_SUPPORT, _PRJ_BILLING, _PRJ_PILOT), start=1):
        for position, (name, env_class, order, status) in enumerate(_ENVIRONMENTS, start=1):
            environment_rows.append(
                {
                    "environment_id": f"WSE-A{number:03d}000{position}",
                    "project_id": project,
                    "name": name,
                    "environment_class": env_class,
                    "promotion_order": order,
                    "status": status,
                }
            )
    tables.append(
        _table(
            "caliber_workspace_environments",
            "environment_id project_id name environment_class promotion_order status created_by created_at updated_at recovery_policy_enabled current_release_id pending_operation_id operation_state policy policy_sha256 lock_version",
            environment_rows,
            defaults={
                "status": "disabled",
                "created_by": "",
                "created_at": _ts(12),
                "updated_at": _ts(12),
                "recovery_policy_enabled": False,
                "operation_state": "idle",
                "policy": {},
                "policy_sha256": "",
                "lock_version": 1,
            },
            pk="environment_id",
        )
    )

    tables.append(
        _table(
            "caliber_project_members",
            "member_id project_id user_id role status created_by created_at updated_at deactivated_at deactivated_by",
            [
                {
                    "member_id": "PRJM-A0000001",
                    "project_id": _PRJ_SUPPORT,
                    "user_id": _ALICE,
                    "role": "admin",
                },
                {
                    "member_id": "PRJM-A0000002",
                    "project_id": _PRJ_SUPPORT,
                    "user_id": _BOB,
                    "role": "editor",
                },
                {
                    "member_id": "PRJM-A0000003",
                    "project_id": _PRJ_SUPPORT,
                    "user_id": _CAROL,
                    "role": "viewer",
                    "status": "inactive",
                    "deactivated_at": _ts(17),
                    "deactivated_by": _ALICE,
                },
                {
                    "member_id": "PRJM-A0000004",
                    "project_id": _PRJ_BILLING,
                    "user_id": _BOB,
                    "role": "admin",
                },
            ],
            defaults={
                "role": "viewer",
                "status": "active",
                "created_by": _ALICE,
                "created_at": _ts(12),
                "updated_at": _ts(12),
            },
            pk="member_id",
        )
    )

    tables.append(
        _table(
            "caliber_sessions",
            "session_id user_id token_hash created_at expires_at last_seen_at revoked_at revoked_reason user_agent",
            [
                {
                    "session_id": "SES-A0000001",
                    "user_id": _ALICE,
                    "token_hash": _digest("session-1"),
                },
                {
                    "session_id": "SES-A0000002",
                    "user_id": _CAROL,
                    "token_hash": _digest("session-2"),
                    "created_at": _ts(14, 9),
                    "expires_at": _ts(15, 9),
                    "last_seen_at": _ts(14, 9, 5),
                    "revoked_at": _ts(14, 9, 6),
                    "revoked_reason": "logout",
                },
            ],
            defaults={
                "created_at": _ts(20, 8, 30),
                "expires_at": _ts(21, 8, 30),
                "last_seen_at": _ts(20, 9, 15),
                "user_agent": "alpha-seed-browser",
            },
            pk="session_id",
        )
    )

    tables.append(
        _table(
            "caliber_personal_access_tokens",
            "token_id user_id name token_hash scopes created_at created_by expires_at last_used_at revoked_at revoked_reason rotated_from project_id",
            [
                {
                    "token_id": "PAT-A0000001",
                    "user_id": _BOB,
                    "name": "ci-runner",
                    "token_hash": _digest("pat-1"),
                    "scopes": "workflow:run,prompt:read",
                    "created_by": _BOB,
                    "expires_at": _ts(30),
                    "last_used_at": _ts(20, 7),
                    "project_id": _PRJ_SUPPORT,
                },
                {
                    "token_id": "PAT-A0000002",
                    "user_id": _BOB,
                    "name": "old-ci-runner",
                    "token_hash": _digest("pat-2"),
                    "scopes": "workflow:run",
                    "created_by": _BOB,
                    "revoked_at": _ts(16),
                    "revoked_reason": "rotated",
                    "project_id": _PRJ_SUPPORT,
                },
            ],
            defaults={"scopes": "", "created_at": _ts(13)},
            pk="token_id",
        )
    )

    tables.append(
        _table(
            "caliber_workflows",
            "workflow_id name description owner status default_experiment_id created_at updated_at project_id visibility",
            [
                {
                    "workflow_id": _WF_REFUND,
                    "name": "refund-triage",
                    "description": "Triage refund requests and issue approved refunds.",
                    "default_experiment_id": "exp-refund-triage",
                    "project_id": _PRJ_SUPPORT,
                },
                {
                    "workflow_id": _WF_INVOICE,
                    "name": "invoice-dispute",
                    "owner": _BOB,
                    "project_id": _PRJ_BILLING,
                },
                {
                    "workflow_id": _WF_LEGACY,
                    "name": "legacy-experiment",
                    "status": "archived",
                    "project_id": _PRJ_PILOT,
                    "updated_at": _ts(18),
                },
            ],
            defaults={
                "description": "",
                "owner": _ALICE,
                "status": "active",
                "created_at": _ts(13),
                "updated_at": _ts(13),
                "visibility": "project",
            },
            pk="workflow_id",
        )
    )

    def version(
        version_id: str, workflow: str, number: int, manifest: dict[str, Any], *, published: bool
    ) -> Row:
        row: Row = {
            "version_id": version_id,
            "workflow_id": workflow,
            "version_number": number,
            "manifest": manifest,
            "manifest_hash": hashlib.sha256(_canonical(manifest).encode()).hexdigest(),
            "validation_report": {"valid": True, "errors": [], "warnings": []},
            "created_at": _ts(13 + number, 11),
        }
        if published:
            row.update(
                {
                    "status": "published",
                    "compiler_version": "caliber-workflow-compiler-v1",
                    "compiled_bundle": {"entrypoint": "start", "nodes": sorted(manifest["nodes"])},
                    "published_by": _ALICE,
                    "published_at": _ts(13 + number, 12),
                }
            )
        return row

    tables.append(
        _table(
            "caliber_workflow_versions",
            "version_id workflow_id version_number status manifest manifest_hash compiler_version compiled_artifact_uri validation_report created_by created_at published_by published_at compiled_bundle",
            [
                version(_V_REFUND_1, _WF_REFUND, 1, refund_manifest, published=True),
                version(_V_REFUND_2, _WF_REFUND, 2, refund_manifest, published=True),
                version(_V_REFUND_3, _WF_REFUND, 3, refund_manifest, published=False),
                version(_V_INVOICE_1, _WF_INVOICE, 1, invoice_manifest, published=True),
                version(_V_LEGACY_1, _WF_LEGACY, 1, legacy_manifest, published=False),
            ],
            defaults={
                "status": "draft",
                "manifest_hash": "",
                "created_by": _ALICE,
                "created_at": _ts(13, 11),
            },
            pk="version_id",
        )
    )

    tables.append(
        _table(
            "caliber_workflow_deployments",
            "deployment_id workflow_id alias version_id environment status deployed_by deployed_at rollback_checkpoint",
            [
                {
                    "deployment_id": "DEP-A0000001",
                    "workflow_id": _WF_REFUND,
                    "alias": "prod",
                    "version_id": _V_REFUND_2,
                    "environment": "prod",
                    "rollback_checkpoint": [{"alias": "prod", "version_id": _V_REFUND_1}],
                },
                {
                    "deployment_id": "DEP-A0000002",
                    "workflow_id": _WF_REFUND,
                    "alias": "dev",
                    "version_id": _V_REFUND_2,
                    "environment": "dev",
                },
                {
                    "deployment_id": "DEP-A0000003",
                    "workflow_id": _WF_INVOICE,
                    "alias": "prod",
                    "version_id": _V_INVOICE_1,
                    "environment": "prod",
                    "deployed_by": _BOB,
                },
            ],
            defaults={
                "status": "active",
                "deployed_by": _ALICE,
                "deployed_at": _ts(15, 13),
                "rollback_checkpoint": [],
            },
            pk="deployment_id",
        )
    )

    run_rows: list[Row] = []
    for index, (run_id, workflow, version_id, status, project) in enumerate(_RUNS):
        queued = _ts(14 + index, 10, 0)
        row: Row = {
            "workflow_run_id": run_id,
            "workflow_id": workflow,
            "workflow_version_id": version_id,
            "status": status,
            "project_id": project,
            "queued_at": queued,
        }
        if status != "queued":
            row.update({"started_at": _ts(14 + index, 10, 0, 5), "claimed_by": "worker-alpha-1"})
        if status in ("completed", "failed", "cancelled"):
            row["completed_at"] = _ts(14 + index, 10, 5)
            row["lease_expires_at"] = None
        run_rows.append(row)
    by_id = {str(row["workflow_run_id"]): row for row in run_rows}
    by_id["WR-A0000001"].update(
        {
            "summary": {"final_output": "refund issued", "nodes": 4},
            "created_by": _ALICE,
            "source": "manual",
        }
    )
    by_id["WR-A0000002"].update(
        {"summary": {"final_output": "refund issued", "nodes": 4}, "source": "api", "priority": 5}
    )
    by_id["WR-A0000003"].update(
        {
            "source": "api",
            "error_code": "tool_error",
            "error_summary": "issue_refund returned HTTP 502",
            "current_node_id": "send_refund",
            "manifest_snapshot": refund_manifest,
        }
    )
    by_id["WR-A0000004"].update(
        {
            "source": "api",
            "current_node_id": "send_refund",
            "claimed_at": _ts(17, 10, 0, 5),
            "lease_expires_at": _ts(17, 10, 5),
            "last_heartbeat_at": _ts(17, 10, 1),
            "manifest_snapshot": refund_manifest,
        }
    )
    by_id["WR-A0000005"].update(
        {
            "current_node_id": "approve_refund",
            "claimed_by": None,
            "manifest_snapshot": refund_manifest,
            "session_id": "sess-alpha-1",
        }
    )
    by_id["WR-A0000006"].update(
        {
            "summary": {"final_output": "dispute recorded", "nodes": 3},
            "source": "schedule",
            "created_by": _BOB,
        }
    )
    by_id["WR-A0000007"].update(
        {"source": "api", "idempotency_key": "alpha-seed-idempotency-1", "created_by": _BOB}
    )
    by_id["WR-A0000008"].update(
        {
            "current_node_id": "send_refund",
            "cancel_requested_at": _ts(21, 10, 3),
            "cancel_requested_by": _ALICE,
            "cancel_reason": "customer withdrew the request",
            "claimed_at": _ts(21, 10, 0, 5),
        }
    )
    by_id["WR-A0000009"].update(
        {
            "source": "retry",
            "attempt_number": 2,
            "parent_run_id": "WR-A0000003",
            "summary": {"final_output": "refund issued", "nodes": 4},
            "created_by": _ALICE,
            "manifest_snapshot": refund_manifest,
        }
    )
    tables.append(
        _table(
            "caliber_workflow_runs",
            "workflow_run_id workflow_id workflow_version_id deployment_alias mlflow_run_id trace_id session_id status started_at completed_at summary project_id tenant_id source priority queued_at claimed_by claimed_at lease_expires_at last_heartbeat_at attempt_number parent_run_id cancel_requested_at cancel_requested_by cancel_reason current_node_id idempotency_key input_file_ref error_code error_summary input_payload manifest_snapshot created_by runtime_lineage_id",
            run_rows,
            defaults={
                "deployment_alias": "prod",
                "status": "completed",
                "tenant_id": "local",
                "source": "manual",
                "priority": 0,
                "attempt_number": 1,
                "created_by": _BOB,
            },
            pk="workflow_run_id",
        )
    )

    tables.append(
        _table(
            "caliber_workflow_run_events",
            "workflow_run_id project_id sequence event_type node_id payload created_at",
            _run_events(),
            defaults={"created_at": _ts(14)},
            pk="event_id",
            autoincrement=True,
        )
    )

    tables.append(
        _table(
            "caliber_workflow_run_checkpoints",
            "checkpoint_id workflow_run_id project_id sequence node_id state_blob created_at",
            [
                {
                    "checkpoint_id": "WRCK-A0000001",
                    "workflow_run_id": "WR-A0000004",
                    "node_id": "triage",
                    "state_blob": {"triage": {"final_output": "refund_eligible"}},
                },
                {
                    "checkpoint_id": "WRCK-A0000002",
                    "workflow_run_id": "WR-A0000005",
                    "node_id": "approve_refund",
                    "state_blob": {
                        "triage": {"final_output": "refund_eligible"},
                        "waiting_on": "approve_refund",
                    },
                    "created_at": _ts(18, 10, 2),
                },
            ],
            defaults={"project_id": _PRJ_SUPPORT, "sequence": 1, "created_at": _ts(17, 10, 2)},
            pk="checkpoint_id",
        )
    )

    policy = {
        "required_role": "editor",
        "approval_count": 1,
        "timeout_behavior": "block",
        "allow_self_approval": False,
    }
    tables.append(
        _table(
            "caliber_runtime_approval_requests",
            "runtime_approval_id workflow_run_id project_id node_id status requested_at decided_at decided_by decision_reason policy_snapshot",
            [
                {
                    "runtime_approval_id": "RAP-A0000001",
                    "workflow_run_id": "WR-A0000001",
                    "status": "approved",
                    "requested_at": _ts(14, 10, 2),
                    "decided_at": _ts(14, 10, 3),
                    "decided_by": _ALICE,
                    "decision_reason": "amount within the refund limit",
                },
                {
                    "runtime_approval_id": "RAP-A0000002",
                    "workflow_run_id": "WR-A0000002",
                    "status": "approved",
                    "requested_at": _ts(15, 10, 2),
                    "decided_at": _ts(15, 10, 3),
                    "decided_by": _BOB,
                    "decision_reason": "verified with the customer",
                },
                {
                    "runtime_approval_id": "RAP-A0000003",
                    "workflow_run_id": "WR-A0000005",
                    "policy_snapshot": {"timeout_behavior": "block"},
                },
                {
                    "runtime_approval_id": "RAP-A0000004",
                    "workflow_run_id": "WR-A0000003",
                    "status": "rejected",
                    "requested_at": _ts(16, 10, 2),
                    "decided_at": _ts(16, 10, 4),
                    "decided_by": _ALICE,
                    "decision_reason": "duplicate request",
                },
            ],
            defaults={
                "project_id": _PRJ_SUPPORT,
                "node_id": "approve_refund",
                "status": "pending",
                "requested_at": _ts(18, 10, 1),
                "policy_snapshot": policy,
            },
            pk="runtime_approval_id",
        )
    )

    tables.append(
        _table(
            "caliber_effect_ledger",
            "effect_key workflow_run_id node_id status result error claimed_at completed_at",
            [
                {
                    "effect_key": _effect_key("WR-A0000001", "send_refund"),
                    "workflow_run_id": "WR-A0000001",
                    "status": "completed",
                    "result": {"refund_id": "rf-a0000001", "amount": 42.5, "currency": "USD"},
                    "claimed_at": _ts(14, 10, 3, 1),
                    "completed_at": _ts(14, 10, 3, 2),
                },
                {
                    "effect_key": _effect_key("WR-A0000002", "send_refund"),
                    "workflow_run_id": "WR-A0000002",
                    "status": "completed",
                    "result": {"refund_id": "rf-a0000002", "amount": 18.0, "currency": "USD"},
                    "claimed_at": _ts(15, 10, 3, 1),
                    "completed_at": _ts(15, 10, 3, 2),
                },
                {
                    "effect_key": _effect_key("WR-A0000003", "send_refund"),
                    "workflow_run_id": "WR-A0000003",
                    "status": "failed",
                    "error": "issue_refund returned HTTP 502",
                    "claimed_at": _ts(16, 10, 0, 6),
                    "completed_at": _ts(16, 10, 0, 7),
                },
                {
                    # The worker died after claiming this effect: the outcome is genuinely unknown.
                    "effect_key": _effect_key("WR-A0000004", "send_refund"),
                    "workflow_run_id": "WR-A0000004",
                    "claimed_at": _ts(17, 10, 0, 6),
                },
                {
                    # Claimed, then the run was cancelled before the call returned.
                    "effect_key": _effect_key("WR-A0000008", "send_refund"),
                    "workflow_run_id": "WR-A0000008",
                    "claimed_at": _ts(21, 10, 0, 6),
                },
                {
                    "effect_key": _effect_key("WR-A0000009", "send_refund"),
                    "workflow_run_id": "WR-A0000009",
                    "status": "completed",
                    "result": {"refund_id": "rf-a0000009", "amount": 7.25, "currency": "USD"},
                    "claimed_at": _ts(22, 10, 0, 6),
                    "completed_at": _ts(22, 10, 0, 7),
                },
            ],
            defaults={"node_id": "send_refund", "status": "in_progress"},
            pk="effect_key",
        )
    )

    tables.append(
        _table(
            "caliber_workflow_services",
            "service_id workflow_id alias input_schema output_schema enabled auth_required created_by created_at updated_at rate_limit_per_minute cors_allowed_origins",
            [
                {
                    "service_id": "wfs-a0000001",
                    "workflow_id": _WF_REFUND,
                    "input_schema": {"type": "object", "properties": {"msg": {"type": "string"}}},
                    "output_schema": {
                        "type": "object",
                        "properties": {"response": {"type": "string"}},
                    },
                }
            ],
            defaults={
                "alias": "prod",
                "enabled": True,
                "auth_required": True,
                "created_by": _ALICE,
                "created_at": _ts(15, 14),
                "updated_at": _ts(15, 14),
                "rate_limit_per_minute": 60,
                "cors_allowed_origins": "https://support.example.test",
            },
            pk="service_id",
        )
    )
    tables.append(
        _table(
            "caliber_service_tokens",
            "token_id workflow_id name token_hash prefix scopes created_by created_at expires_at revoked_at",
            [
                {
                    "token_id": "svt-a0000001",
                    "workflow_id": _WF_REFUND,
                    "name": "support-portal",
                    "token_hash": _digest("service-token-1"),
                    "prefix": "svt_a000",
                    "scopes": ["run"],
                    "expires_at": _ts(31),
                }
            ],
            defaults={"name": "", "prefix": "", "created_by": _ALICE, "created_at": _ts(15, 14, 5)},
            pk="token_id",
        )
    )

    tables.append(
        _table(
            "caliber_secrets",
            "name description current_version created_by created_at updated_by updated_at revoked_at revoked_by",
            [
                {
                    "name": "support_api_credentials",
                    "description": "Refund API credentials for the support workflows",
                    "current_version": 2,
                    "updated_by": _ALICE,
                    "updated_at": _ts(16, 8),
                },
                {
                    "name": "billing_webhook_signing",
                    "description": "Signs billing webhook calls",
                    "current_version": 1,
                    "created_by": _BOB,
                },
                {
                    "name": "retired_crm_access",
                    "description": "CRM access retired with the pilot",
                    "current_version": 1,
                    "revoked_at": _ts(18, 8),
                    "revoked_by": _ALICE,
                },
            ],
            defaults={
                "description": "",
                "current_version": 0,
                "created_by": _ALICE,
                "created_at": _ts(13, 8),
            },
            pk="name",
        )
    )
    tables.append(
        _table(
            "caliber_secret_versions",
            "name version nonce ciphertext key_id created_by created_at",
            [
                {
                    "name": "support_api_credentials",
                    "version": 1,
                    "nonce": _blob("nonce-support-1", 12),
                    "ciphertext": _blob("cipher-support-1", 32),
                },
                {
                    "name": "support_api_credentials",
                    "version": 2,
                    "nonce": _blob("nonce-support-2", 12),
                    "ciphertext": _blob("cipher-support-2", 32),
                    "created_at": _ts(16, 8),
                },
                {
                    "name": "billing_webhook_signing",
                    "version": 1,
                    "nonce": _blob("nonce-billing-1", 12),
                    "ciphertext": _blob("cipher-billing-1", 32),
                    "created_by": _BOB,
                },
                {
                    "name": "retired_crm_access",
                    "version": 1,
                    "nonce": _blob("nonce-crm-1", 12),
                    "ciphertext": _blob("cipher-crm-1", 32),
                },
            ],
            defaults={"key_id": "k1", "created_by": _ALICE, "created_at": _ts(13, 8)},
            pk="secret_version_id",
            autoincrement=True,
        )
    )

    tables.append(
        _table(
            "caliber_tool_registry",
            "tool_id name version description module_path callable_name input_schema output_schema side_effect_level requires_approval allow_in_preview secret_refs owner status deprecated_at successor_tool_id created_at updated_at project_id visibility test_cases last_calibration baseline_run_id calibration_revision execution_backend backend_config",
            [
                {
                    "tool_id": "TL-A0000001",
                    "name": "lookup_policy",
                    "version": "1.0",
                    "description": "Look up the refund policy text",
                    "module_path": "support_tools.policy",
                    "callable_name": "lookup_policy",
                    "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}},
                    "output_schema": {
                        "type": "object",
                        "properties": {"policy": {"type": "string"}},
                    },
                    "allow_in_preview": True,
                    "test_cases": [
                        {"input": {"topic": "refund"}, "expected": {"policy": "30-day refund"}}
                    ],
                    "last_calibration": {"pass_rate": 1.0, "cases": 1},
                },
                {
                    "tool_id": "TL-A0000002",
                    "name": "issue_refund",
                    "version": "1.0",
                    "description": "Issue a refund through the payments API",
                    "module_path": "support_tools.payments",
                    "callable_name": "issue_refund",
                    "input_schema": {
                        "type": "object",
                        "properties": {"amount": {"type": "number"}},
                    },
                    "output_schema": {
                        "type": "object",
                        "properties": {"refund_id": {"type": "string"}},
                    },
                    "side_effect_level": "external_action",
                    "requires_approval": True,
                    "secret_refs": ["support_api_credentials"],
                    "updated_at": _ts(16, 9),
                },
                {
                    "tool_id": "TL-A0000003",
                    "name": "issue_refund",
                    "version": "0.9",
                    "description": "Superseded refund tool",
                    "module_path": "support_tools.payments_old",
                    "callable_name": "issue_refund",
                    "side_effect_level": "external_action",
                    "requires_approval": True,
                    "secret_refs": ["retired_crm_access"],
                    "status": "deprecated",
                    "deprecated_at": _ts(16, 9),
                    "successor_tool_id": "TL-A0000002",
                },
            ],
            defaults={
                "description": "",
                "side_effect_level": "read",
                "requires_approval": False,
                "allow_in_preview": False,
                "secret_refs": [],
                "owner": _ALICE,
                "status": "active",
                "created_at": _ts(13, 9),
                "updated_at": _ts(13, 9),
                "project_id": _PRJ_SUPPORT,
                "visibility": "project",
                "test_cases": [],
                "calibration_revision": 1,
                "execution_backend": "python_callable",
            },
            pk="tool_id",
        )
    )

    tables.append(
        _table(
            "caliber_skills",
            "skill_id name description content owner tags status version created_at updated_at summary category skill_metadata allowed_tools depends_on project_id visibility",
            [
                {
                    "skill_id": "SK-A0000001",
                    "name": "refund-policy-brief",
                    "description": "Summarise the refund policy for an agent",
                    "content": "Quote the policy, then state the refund window.",
                    "tags": ["support", "policy"],
                    "version": 2,
                    "summary": "Refund policy brief",
                    "allowed_tools": "lookup_policy",
                    "updated_at": _ts(16, 10),
                }
            ],
            defaults={
                "description": "",
                "owner": _ALICE,
                "tags": [],
                "status": "active",
                "version": 1,
                "created_at": _ts(13, 10),
                "updated_at": _ts(13, 10),
                "summary": "",
                "category": "custom",
                "skill_metadata": {},
                "depends_on": [],
                "project_id": _PRJ_SUPPORT,
                "visibility": "project",
            },
            pk="skill_id",
        )
    )
    tables.append(
        _table(
            "caliber_skill_versions",
            "skill_version_id skill_id version_number content summary created_by created_at",
            [
                {
                    "skill_version_id": "SKV-A0000001",
                    "version_number": 1,
                    "content": "Quote the policy.",
                    "summary": "Refund policy brief",
                },
                {
                    "skill_version_id": "SKV-A0000002",
                    "version_number": 2,
                    "content": "Quote the policy, then state the refund window.",
                    "summary": "Refund policy brief",
                    "created_at": _ts(16, 10),
                },
            ],
            defaults={
                "skill_id": "SK-A0000001",
                "summary": "",
                "created_by": _ALICE,
                "created_at": _ts(13, 10),
            },
            pk="skill_version_id",
        )
    )

    tables.append(
        _table(
            "caliber_eval_datasets",
            "dataset_id name description owner tags status version created_at updated_at project_id visibility mlflow_dataset_id mlflow_synced_at mlflow_synced_version mlflow_record_count mlflow_digest",
            [
                {
                    "dataset_id": "ED-A0000001",
                    "name": "refund-triage-golden",
                    "description": "Golden refund requests with the expected decision",
                    "tags": ["golden"],
                    "version": 2,
                    "updated_at": _ts(16, 12),
                    "mlflow_dataset_id": "d-a0000001",
                    "mlflow_synced_at": _ts(16, 12, 5),
                    "mlflow_synced_version": 2,
                    "mlflow_record_count": 2,
                    "mlflow_digest": _digest("dataset-1"),
                }
            ],
            defaults={
                "description": "",
                "owner": _ALICE,
                "tags": [],
                "status": "active",
                "version": 1,
                "created_at": _ts(13, 12),
                "updated_at": _ts(13, 12),
                "project_id": _PRJ_SUPPORT,
                "visibility": "project",
            },
            pk="dataset_id",
        )
    )
    tables.append(
        _table(
            "caliber_eval_dataset_examples",
            "example_id dataset_id dataset_version input expected weight tags created_at superseded_at superseded_version",
            [
                {
                    "example_id": "EX-A0000001",
                    "dataset_version": 1,
                    "input": {"msg": "Refund order 1001, it arrived broken"},
                    "expected": {"decision": "refund"},
                },
                {
                    "example_id": "EX-A0000002",
                    "dataset_version": 1,
                    "input": {"msg": "I want my money back after 90 days"},
                    "expected": {"decision": "refund"},
                    "superseded_at": _ts(16, 12),
                    "superseded_version": 2,
                },
                {
                    "example_id": "EX-A0000003",
                    "dataset_version": 2,
                    "input": {"msg": "I want my money back after 90 days"},
                    "expected": {"decision": "escalate"},
                    "weight": 2.0,
                    "tags": ["edge"],
                    "created_at": _ts(16, 12),
                },
            ],
            defaults={
                "dataset_id": "ED-A0000001",
                "weight": 1.0,
                "tags": [],
                "created_at": _ts(13, 12),
            },
            pk="example_id",
        )
    )
    tables.append(
        _table(
            "caliber_eval_runs",
            "run_id dataset_id dataset_version label predict_target model scorers pass_threshold n_examples passed_count failed_count overall_score pass_rate aggregate results status error_message created_by project_id visibility created_at completed_at subject_ref evidence runtime_lineage_id",
            [
                {
                    "run_id": "EVR-A0000001",
                    "label": "refund-triage v2 baseline",
                    "model": "gpt-4o-mini",
                    "scorers": ["exact_match"],
                    "n_examples": 2,
                    "passed_count": 2,
                    "overall_score": 1.0,
                    "pass_rate": 1.0,
                    "aggregate": {"exact_match": 1.0},
                    "results": [
                        {"example_id": "EX-A0000001", "passed": True, "score": 1.0},
                        {"example_id": "EX-A0000003", "passed": True, "score": 1.0},
                    ],
                    "subject_ref": f"workflow:{_WF_REFUND}@2",
                }
            ],
            defaults={
                "dataset_id": "ED-A0000001",
                "dataset_version": 2,
                "label": "",
                "predict_target": "llm",
                "scorers": [],
                "pass_threshold": 0.5,
                "n_examples": 0,
                "passed_count": 0,
                "failed_count": 0,
                "aggregate": {},
                "results": [],
                "status": "completed",
                "created_by": _ALICE,
                "project_id": _PRJ_SUPPORT,
                "visibility": "project",
                "created_at": _ts(16, 13),
                "completed_at": _ts(16, 13, 4),
            },
            pk="run_id",
        )
    )

    tables.append(
        _table(
            "caliber_agent_config",
            "agent_id experiment_id name owner artifact_types eval_thresholds optimizer_config approval_policy optimize_for collaboration_mode enabled required_approvals created_at updated_at project_id visibility",
            [
                {
                    "agent_id": "refund-triage-agent",
                    "experiment_id": "exp-refund-triage",
                    "name": "Refund triage",
                }
            ],
            defaults={
                "owner": _ALICE,
                "artifact_types": ["prompt"],
                "eval_thresholds": {"min_aggregate_score": 0.8},
                "optimizer_config": {"optimizer": "auto"},
                "approval_policy": {"required_approvals": 1},
                "optimize_for": "quality",
                "enabled": True,
                "required_approvals": 1,
                "created_at": _ts(13, 14),
                "updated_at": _ts(13, 14),
                "project_id": _PRJ_SUPPORT,
                "visibility": "project",
            },
            pk="agent_id",
        )
    )
    tables.append(
        _table(
            "caliber_verification_queue",
            "item_id agent_id assessment_id trace_id experiment_id session_id workflow_id category free_text severity artifact_type_hint artifact_ref submitted_context status priority assigned_to verified_by verified_at verification_notes refinement_target duplicate_of_id created_at project_id",
            [
                {
                    "item_id": "FB-A0000001",
                    "assessment_id": "as-a0000001",
                    "trace_id": "tr-a0000002",
                    "category": "classification_accuracy",
                    "free_text": "A refund after 90 days was approved; policy says escalate.",
                    "artifact_ref": "prompts:/refund-triage-system/2",
                    "submitted_context": {"workflow_run_id": "WR-A0000002"},
                    "status": "verified",
                    "priority": 2,
                    "verified_by": _ALICE,
                    "verified_at": _ts(17, 9, 30),
                    "verification_notes": "Reproduced on the golden set.",
                    "refinement_target": "prompt",
                },
                {
                    "item_id": "FB-A0000002",
                    "category": "tone",
                    "free_text": "Reply was curt.",
                    "severity": "low",
                    "artifact_type_hint": None,
                },
            ],
            defaults={
                "agent_id": "refund-triage-agent",
                "experiment_id": "exp-refund-triage",
                "severity": "standard",
                "artifact_type_hint": "prompt",
                "status": "pending",
                "priority": 0,
                "created_at": _ts(17, 9),
                "project_id": _PRJ_SUPPORT,
            },
            pk="item_id",
        )
    )
    tables.append(
        _table(
            "caliber_refinement_jobs",
            "job_id agent_id workflow_id primary_item_id mlflow_run_id artifact_type optimizer_type status current_stage attempt_count error_message total_tokens cost_usd bundle_targets bundle_expansion_count created_at updated_at diagnosis candidate eval_results review_notes last_heartbeat_at skill_name calibration_spec refine_iteration",
            [
                {
                    "job_id": "RFN-A0000001",
                    "mlflow_run_id": "ml-a0000100",
                    "optimizer_type": "DSPyBootstrapFewShot",
                    "status": "completed",
                    "current_stage": "approval",
                    "attempt_count": 1,
                    "total_tokens": 4210,
                    "cost_usd": 0.0125,
                    "bundle_targets": ["prompt"],
                    "updated_at": _ts(17, 11),
                    "diagnosis": {"root_cause": "missing escalation rule for old orders"},
                    "candidate": {"prompt": "refund-triage-system", "version": 3},
                    "eval_results": {"overall_score": 0.92},
                    "review_notes": "Approved after the golden set passed.",
                    "refine_iteration": 1,
                }
            ],
            defaults={
                "agent_id": "refund-triage-agent",
                "primary_item_id": "FB-A0000001",
                "artifact_type": "prompt",
                "status": "queued",
                "current_stage": "triage",
                "attempt_count": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "bundle_targets": [],
                "bundle_expansion_count": 0,
                "created_at": _ts(17, 10),
                "updated_at": _ts(17, 10),
                "refine_iteration": 0,
            },
            pk="job_id",
        )
    )
    tables.append(
        _table(
            "caliber_approval_requests",
            "approval_id job_id agent_id status eval_results candidate_snapshot diagnosis_snapshot approved_by approved_at rejection_reason created_at updated_at attempt_number",
            [
                {
                    "approval_id": "AP-A0000001",
                    "job_id": "RFN-A0000001",
                    "status": "approved",
                    "eval_results": {"overall_score": 0.92},
                    "candidate_snapshot": {"prompt": "refund-triage-system", "version": 3},
                    "diagnosis_snapshot": {"root_cause": "missing escalation rule for old orders"},
                    "approved_by": _ALICE,
                    "approved_at": _ts(17, 12),
                    "updated_at": _ts(17, 12),
                }
            ],
            defaults={
                "agent_id": "refund-triage-agent",
                "status": "pending",
                "created_at": _ts(17, 11),
                "updated_at": _ts(17, 11),
                "attempt_number": 1,
            },
            pk="approval_id",
        )
    )
    tables.append(
        _table(
            "caliber_rollback_checkpoints",
            "checkpoint_id approval_id agent_id artifact_type artifact_name artifact_ref_before artifact_ref_after version_before version_after snapshot_payload rolled_back_at rolled_back_by created_at",
            [
                {
                    "checkpoint_id": "CK-A0000001",
                    "artifact_name": "refund-triage-system",
                    "artifact_ref_before": "prompts:/refund-triage-system/2",
                    "artifact_ref_after": "prompts:/refund-triage-system/3",
                    "version_before": 2,
                    "version_after": 3,
                    "snapshot_payload": {"alias": "production", "previous_version": 2},
                }
            ],
            defaults={
                "approval_id": "AP-A0000001",
                "agent_id": "refund-triage-agent",
                "artifact_type": "prompt",
                "created_at": _ts(17, 12, 1),
            },
            pk="checkpoint_id",
        )
    )
    tables.append(
        _table(
            "caliber_prompt_test_runs",
            "test_run_id agent_id prompt_name prompt_alias prompt_version model eval_dataset_id test_set_size passed_count failed_count partial_count overall_score results trace_id mlflow_run_id created_by status created_at completed_at runtime_lineage_id",
            [
                {
                    "test_run_id": "PTR-A0000001",
                    "prompt_alias": "production",
                    "prompt_version": 2,
                    "passed_count": 1,
                    "failed_count": 1,
                    "overall_score": 0.5,
                    "results": [
                        {"example_id": "EX-A0000001", "passed": True},
                        {"example_id": "EX-A0000002", "passed": False},
                    ],
                },
                {
                    "test_run_id": "PTR-A0000002",
                    "prompt_alias": "candidate",
                    "prompt_version": 3,
                    "passed_count": 2,
                    "overall_score": 1.0,
                    "results": [
                        {"example_id": "EX-A0000001", "passed": True},
                        {"example_id": "EX-A0000003", "passed": True},
                    ],
                    "created_at": _ts(17, 11, 30),
                    "completed_at": _ts(17, 11, 32),
                },
            ],
            defaults={
                "agent_id": "refund-triage-agent",
                "prompt_name": "refund-triage-system",
                "model": "gpt-4o-mini",
                "eval_dataset_id": "ED-A0000001",
                "test_set_size": 2,
                "passed_count": 0,
                "failed_count": 0,
                "partial_count": 0,
                "results": [],
                "created_by": _ALICE,
                "status": "completed",
                "created_at": _ts(17, 8),
                "completed_at": _ts(17, 8, 2),
            },
            pk="test_run_id",
        )
    )

    audit_events: list[tuple[str, str, str, str, dict[str, Any], str]] = [
        (_ALICE, "auth.login", "user", _ALICE, {"method": "password"}, "standard"),
        (
            _ALICE,
            "project.create",
            "project",
            _PRJ_SUPPORT,
            {"name": "Customer Support"},
            "standard",
        ),
        (
            _ALICE,
            "project.member_add",
            "project",
            _PRJ_SUPPORT,
            {"user_id": _BOB, "role": "editor"},
            "standard",
        ),
        (_ALICE, "secret.create", "secret", "support_api_credentials", {"version": 1}, "high"),
        (_ALICE, "workflow.publish", "workflow", _WF_REFUND, {"version_number": 2}, "standard"),
        (_BOB, "workflow.run", "workflow_run", "WR-A0000002", {"source": "api"}, "standard"),
        (
            _ALICE,
            "workflow.approval.approve",
            "workflow_run",
            "WR-A0000001",
            {"node_id": "approve_refund"},
            "high",
        ),
        (_ALICE, "secret.rotate", "secret", "support_api_credentials", {"version": 2}, "critical"),
        (
            _ALICE,
            "refinement.approve",
            "refinement_job",
            "RFN-A0000001",
            {"candidate_version": 3},
            "high",
        ),
        (_ALICE, "project.archive", "project", _PRJ_PILOT, {"reason": "pilot ended"}, "high"),
    ]
    tables.append(
        _table(
            "caliber_audit_log",
            "timestamp actor action entity_type entity_id details environment_id severity",
            [
                {
                    "timestamp": _ts(12 + index, 8, index),
                    "actor": actor,
                    "action": action,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "details": details,
                    "severity": severity,
                }
                for index, (actor, action, entity_type, entity_id, details, severity) in enumerate(
                    audit_events
                )
            ],
            defaults={"severity": "standard"},
            pk="log_id",
            autoincrement=True,
        )
    )

    tables.append(
        _table(
            "caliber_live_events",
            "origin event_type payload created_at",
            [
                {
                    "origin": "workflow",
                    "event_type": "run.completed",
                    "payload": {"workflow_run_id": "WR-A0000009"},
                },
                {
                    "origin": "workflow",
                    "event_type": "run.waiting_approval",
                    "payload": {"workflow_run_id": "WR-A0000005"},
                    "created_at": _ts(20, 9, 1),
                },
            ],
            defaults={"created_at": _ts(20, 9)},
            pk="event_id",
            autoincrement=True,
        )
    )
    tables.append(
        _table(
            "caliber_worker_heartbeats",
            "worker_id kind hostname started_at last_heartbeat_at ticks",
            [{"worker_id": "worker-alpha-1", "ticks": 612}],
            defaults={
                "kind": "workflow_run",
                "hostname": "alpha-host-1",
                "started_at": _ts(17, 9),
                "last_heartbeat_at": _ts(17, 10, 1),
                "ticks": 0,
            },
            pk="worker_id",
        )
    )
    tables.append(
        _table(
            "caliber_runtime_locks",
            "lock_name owner expires_at checkpoint updated_at",
            [
                {
                    "lock_name": "workflow-run-scheduler",
                    "owner": "worker-alpha-1",
                    "expires_at": _ts(17, 10, 6),
                    "checkpoint": {"last_scan": _ts(17, 10, 1)},
                }
            ],
            defaults={"owner": "", "updated_at": _ts(17, 10, 1)},
            pk="lock_name",
        )
    )
    tables.append(
        _table(
            "caliber_incidents",
            "incident_id objective signal severity status detail observed target opened_at resolved_at acknowledged_at acknowledged_by silenced_until notified_at resolved_notified_at",
            [
                {
                    "incident_id": "INC-A0000001",
                    "objective": "workflow-run-success",
                    "signal": "success_rate",
                    "detail": "Success rate fell below target after the 502s from the payments API.",
                    "observed": 0.82,
                    "target": 0.95,
                    "opened_at": _ts(16, 10, 10),
                    "acknowledged_at": _ts(16, 10, 30),
                    "acknowledged_by": _ALICE,
                }
            ],
            defaults={"severity": "warning", "status": "open", "detail": ""},
            pk="incident_id",
        )
    )
    return tuple(tables)


@cache
def alpha_seed() -> tuple[SeedTable, ...]:
    """The seed, built once per process, parents before children."""
    return _build_seed()


def _normalise(value: Any) -> Any:
    """One spelling for a value read back from either dialect (psycopg parses JSON and returns datetimes)."""
    if isinstance(value, (dict, list)):
        return _canonical(value)
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S.%f")
    if isinstance(value, memoryview):
        return bytes(value)
    return value


def _stored(value: Value) -> Any:
    """What a seed value looks like once stored and read back (JSON columns come back as their canonical text)."""
    return _canonical(value) if isinstance(value, (dict, list)) else value


def expected_rows() -> dict[str, list[dict[str, Any]]]:
    """Every seeded table's rows as :func:`read_seeded_rows` returns them straight after seeding.

    JSON columns are canonical JSON text; autoincrement ids are 1, 2, 3 ... in insertion order.
    """
    result: dict[str, list[dict[str, Any]]] = {}
    for table in alpha_seed():
        rows: list[dict[str, Any]] = []
        for number, row in enumerate(table.rows, start=1):
            stored = {column: _stored(value) for column, value in row.items()}
            rows.append({table.pk: number, **stored} if table.autoincrement else stored)
        result[table.name] = sorted(rows, key=itemgetter(table.pk))
    return result


def row_counts() -> dict[str, int]:
    """Rows seeded per table."""
    return {table.name: len(table.rows) for table in alpha_seed()}


def seed_sha256() -> str:
    """A fingerprint of the seed data: the same on every run and every dialect, different if any value changes."""
    payload = json.dumps(
        [[table.name, table.columns, table.pk, table.rows] for table in alpha_seed()],
        sort_keys=True,
        default=lambda value: value.hex(),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# --------------------------------------------------------------------------------------------------------------
# Creating and seeding the database
# --------------------------------------------------------------------------------------------------------------


def alpha_head() -> str:
    """The revision a ``caliber-v0.1.0a1`` database is at, derived from the manifest's migration files."""
    return head_revision(load_manifest())


def alpha_version_table() -> str:
    """The alembic version table an alpha database is versioned in (recorded in the manifest)."""
    return str(load_manifest()["version_table"])


def _script_versions_dir() -> Path:
    """The ``versions/`` directory alembic will read when ``caliber.db.migrate`` builds its configuration."""
    return Path(ScriptDirectory.from_config(build_config()).versions)


@contextmanager
def _database_url_env(url: str) -> Iterator[None]:
    with mock.patch.dict(os.environ, {"CALIBER_DATABASE_URL": url}):
        yield


def migrate_database(url: str, revision: str = "head") -> None:
    """``alembic upgrade <revision>`` against ``url``, the way ``caliber-migrate`` does it."""
    with _database_url_env(url):
        command.upgrade(build_config(), revision)


def create_alpha_database(url: str) -> None:
    """Create the alpha schema in the (empty) database at ``url`` by migrating to the alpha head.

    Refuses, before touching the database, if any migration file the alembic configuration reads differs from the
    ones the alpha shipped (an edited, renamed, removed or inserted migration would make this a different schema).
    """
    problems = verify_tree(load_manifest(), _script_versions_dir())
    if problems:
        raise AlphaSeedError(
            "the migrations alembic reads are not the ones caliber-v0.1.0a1 shipped, so the schema built from "
            "them would not be the alpha's:\n" + "\n".join(problems)
        )
    migrate_database(url, alpha_head())
    assert_at_alpha_head(url)


def _recorded_versions(connection: Connection) -> list[str]:
    inspector = inspect(connection)
    table = alpha_version_table()
    if not inspector.has_table(table):
        return []
    quoted = connection.dialect.identifier_preparer.quote(table)
    return sorted(
        str(v) for v in connection.execute(text(f"SELECT version_num FROM {quoted}")).scalars()
    )


def assert_at_alpha_head(url: str) -> None:
    """Raise :class:`AlphaSeedError` unless the database's recorded revision is exactly the alpha head."""
    engine = create_engine(normalize_database_url(url))
    try:
        with engine.connect() as connection:
            recorded = _recorded_versions(connection)
    finally:
        engine.dispose()
    expected = alpha_head()
    if recorded != [expected]:
        shown = (
            ", ".join(recorded)
            if recorded
            else "no revision (the version table is missing or empty)"
        )
        raise AlphaSeedError(
            f"not a caliber-v0.1.0a1 database: it records {shown} in {alpha_version_table()}, "
            f"the alpha head is {expected}"
        )


def _insert_statement(
    connection: Connection, table: SeedTable, row: Row
) -> tuple[Any, dict[str, Any]]:
    quote = connection.dialect.identifier_preparer.quote
    columns = list(row)
    names = ", ".join(quote(column) for column in columns)
    placeholders = ", ".join(f":p{index}" for index in range(len(columns)))
    params = {
        f"p{index}": _canonical(row[column])
        if isinstance(row[column], (dict, list))
        else row[column]
        for index, column in enumerate(columns)
    }
    return text(f"INSERT INTO {quote(table.name)} ({names}) VALUES ({placeholders})"), params


def seed_alpha_database(url: str) -> None:
    """Insert the alpha data into a database that is at the alpha head and holds no application rows yet.

    All rows go in one transaction: a failure leaves the database as it was. Raises :class:`AlphaSeedError` if the
    database is not exactly at the alpha head or already holds rows.
    """
    assert_at_alpha_head(url)
    engine = create_engine(normalize_database_url(url))
    try:
        with engine.begin() as connection:
            quote = connection.dialect.identifier_preparer.quote
            for table in alpha_seed():
                present = connection.execute(
                    text(f"SELECT COUNT(*) FROM {quote(table.name)}")
                ).scalar_one()
                if present:
                    raise AlphaSeedError(
                        f"{table.name} already holds {present} row(s); seed an unseeded alpha database"
                    )
            for table in alpha_seed():
                for row in table.rows:
                    statement, params = _insert_statement(connection, table, row)
                    connection.execute(statement, params)
    finally:
        engine.dispose()


@dataclass(frozen=True)
class AlphaDatabase:
    """A created, seeded alpha database."""

    url: str
    dialect: str
    head: str
    version_table: str


@contextmanager
def alpha_database(
    dialect: str, tmp_path: Path, name: str, *, seed: bool = True
) -> Iterator[AlphaDatabase]:
    """An isolated database (SQLite file or throwaway PostgreSQL database) created at the alpha head and seeded.

    ``seed=False`` yields the bare alpha schema. The PostgreSQL database is dropped on exit.
    """
    with dialect_database_url(dialect, tmp_path, name) as url:
        create_alpha_database(url)
        if seed:
            seed_alpha_database(url)
        yield AlphaDatabase(
            url=url, dialect=dialect, head=alpha_head(), version_table=alpha_version_table()
        )


# --------------------------------------------------------------------------------------------------------------
# Reading the database back
# --------------------------------------------------------------------------------------------------------------


def read_seeded_rows(url: str) -> dict[str, list[dict[str, Any]]]:
    """The rows of every seeded table, restricted to the alpha columns, ordered by primary key.

    Restricting to the alpha columns lets the same call work after an upgrade added columns. Values are normalised
    to one spelling across dialects (JSON as canonical text, timestamps as ``YYYY-MM-DD HH:MM:SS.ffffff``).
    """
    engine = create_engine(normalize_database_url(url))
    try:
        with engine.connect() as connection:
            quote = connection.dialect.identifier_preparer.quote
            result: dict[str, list[dict[str, Any]]] = {}
            for table in alpha_seed():
                names = ", ".join(quote(column) for column in table.columns)
                rows = connection.execute(
                    text(f"SELECT {names} FROM {quote(table.name)}")
                ).fetchall()
                # Sorted here, not by the database: text collation differs between SQLite and PostgreSQL.
                result[table.name] = sorted(
                    (
                        {
                            column: _normalise(value)
                            for column, value in zip(table.columns, row, strict=True)
                        }
                        for row in rows
                    ),
                    key=itemgetter(table.pk),
                )
            return result
    finally:
        engine.dispose()


def schema_shape(url: str) -> dict[str, tuple[str, ...]]:
    """``{table: sorted column names}`` for every table in the database except the alembic version table."""
    engine = create_engine(normalize_database_url(url))
    try:
        inspector = inspect(engine)
        shape: dict[str, tuple[str, ...]] = {}
        for table in inspector.get_table_names():
            if table == alpha_version_table():
                continue
            with warnings.catch_warnings():
                # PostgreSQL's pgvector column (migration 0060) has no SQLAlchemy type; reflecting it warns.
                warnings.simplefilter("ignore", category=SAWarning)
                columns = inspector.get_columns(table)
            shape[table] = tuple(sorted(column["name"] for column in columns))
        return shape
    finally:
        engine.dispose()
