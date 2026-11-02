"""``docs/start/beta-scope.md`` cannot drift from the capability report it explains (B15-Support.2, #764).

The page says which adapters the Beta work supports and cites the scenario tests behind each claim. A table that
promotes an adapter the code does not, an evidence id that names no test, or a refusal sentence that no longer matches
``GET /capabilities`` would make the page lie in the other direction, so each of those is read back from the code:

* the support matrix lists the runtime adapters in the runtime order, and the best status it gives each is the best status
  any configuration can reach (so a page cannot promote MCP servers or OpenAPI integrations, and cannot demote a supported
  adapter);
* the evidence table is exactly :data:`caliber.features.SUPPORT_EVIDENCE`, and every test id the page cites resolves to a
  test function on ``main``;
* every ``message`` the report can produce, and every ``reason_code``, setting and refusal code it carries, is on the page
  word for word;
* the known-limits table is the matrix's own list of unrouted protected-effect rows, with the owning issue of each;
* the alpha pages stay historical and point at the Beta page, and no page claims a general-availability status.

Nothing here runs a scenario: whether the cited tests pass is the "EGI scenarios (PostgreSQL)" job. A documentation
contract test needs no database and no network.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

import caliber.eval.evidence as evidence_module
import caliber.features as features_module
import caliber.profile as profile_module
from caliber.config import CaliberConfig
from caliber.features import (
    ADAPTERS,
    FEATURES,
    SUPPORT_EVIDENCE,
    SUPPORT_STATUSES,
    SUPPORTED,
    adapter_capabilities,
)
from caliber.profile import reference_profile_violations
from tests.test_profile import SAFE

CALIBER_DIR = Path(__file__).resolve().parents[1]
REPO = CALIBER_DIR.parent
PAGE = REPO / "docs" / "start" / "beta-scope.md"
ALPHA_SCOPE = REPO / "docs" / "start" / "alpha-scope.md"
ALPHA_INSTALL = REPO / "docs" / "start" / "alpha-install.md"
REFERENCE_PAGE = REPO / "docs" / "operate" / "reference-profile.md"
API_REFERENCE = REPO / "docs" / "api" / "reference.md"
BUILD_DOCS = REPO / "docs-site" / "build-docs.mjs"
SITE_MAP = REPO / "docs-site" / "docs-site-map.json"
MATRIX = CALIBER_DIR / "tests" / "fixtures" / "effect_path_matrix.json"
RUNTIME = CALIBER_DIR / "src" / "caliber" / "workflows" / "runtime.py"

pytestmark = pytest.mark.skipif(not PAGE.exists(), reason="repository docs not present")

ADAPTER_IDS = [spec.id for spec in ADAPTERS]


# ---------------------------------------------------------------------------
# Reading the page
# ---------------------------------------------------------------------------


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """The body under ``heading`` (a line such as ``## The support matrix``) up to the next heading of the same or higher level."""
    level = len(heading) - len(heading.lstrip("#"))
    lines = text.splitlines()
    start = lines.index(heading) + 1
    body: list[str] = []
    for line in lines[start:]:
        match = re.match(r"^(#+) ", line)
        if match and len(match.group(1)) <= level:
            break
        body.append(line)
    return "\n".join(body)


def _table(text: str) -> list[list[str]]:
    """Rows of the first pipe table in ``text``, header and separator dropped, each as a list of stripped cells."""
    rows: list[list[str]] = []
    for line in text.splitlines():
        if not line.startswith("|"):
            if rows:
                break
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        rows.append(cells)
    assert len(rows) >= 3, "no table found"
    return rows[2:]


def _code(cell: str) -> list[str]:
    return re.findall(r"`([^`]+)`", cell)


def _cell_status(cell: str) -> tuple[str, str | None]:
    """``(status, reason_code)`` of a matrix cell such as ``preview (`not_reference_profile`)``."""
    match = re.match(r"^`(supported|preview|unavailable)`(?: \(`(\w+)`\))?", cell)
    assert match, f"not a status cell: {cell!r}"
    return match.group(1), match.group(2)


def _configs() -> dict[str, CaliberConfig]:
    """Named configurations that between them reach every status and reason of every adapter."""
    on = {
        "feature_release_candidates_enabled": True,
        "workspace_release_evaluator_enabled": True,
        "eval_provider": "mlflow",
        "feature_integrations_enabled": True,
    }

    def make(base: dict[str, object], **updates: object) -> CaliberConfig:
        return CaliberConfig().model_copy(update={**base, **on, **updates})

    reference = SAFE
    return {
        "reference": make(reference),
        "development": make({}),
        "default": CaliberConfig(),
        "simulated": make(reference, eval_provider="fake"),
        "unknown provider": make(reference, eval_provider="mlflo"),
        "no loops": make(reference, background_tasks_enabled=False),
        "no reconciler": make(reference, effect_reconciler_enabled=False),
        "evaluator off": make(reference, workspace_release_evaluator_enabled=False),
        "integrations off": make(reference, feature_integrations_enabled=False),
        "release off": make(reference, feature_release_candidates_enabled=False),
        "violation": make(reference, csrf_enabled=False),
        "development, release off": make(
            {}, feature_release_candidates_enabled=False, feature_integrations_enabled=False
        ),
    }


def _all_blocks() -> list[tuple[str, str, dict[str, object]]]:
    return [
        (name, adapter_id, block)
        for name, config in _configs().items()
        for adapter_id, block in adapter_capabilities(config).items()
    ]


# ---------------------------------------------------------------------------
# The matrix and its evidence
# ---------------------------------------------------------------------------


def test_the_matrix_lists_the_runtime_adapters_in_the_runtime_order() -> None:
    rows = _table(_section(_page(), "## The support matrix"))

    assert [_code(row[0])[0] for row in rows] == ADAPTER_IDS
    assert list(adapter_capabilities(CaliberConfig())) == ADAPTER_IDS


def test_the_best_status_of_each_adapter_is_the_best_any_configuration_reaches() -> None:
    """The page cannot promote an adapter the code never promotes, nor hide one it does."""
    reached: dict[str, int] = {}
    for _name, adapter_id, block in _all_blocks():
        rank = SUPPORT_STATUSES.index(str(block["status"]))
        reached[adapter_id] = max(reached.get(adapter_id, 0), rank)

    rows = _table(_section(_page(), "## The support matrix"))
    for row in rows:
        adapter_id = _code(row[0])[0]
        best = _code(row[2])
        assert len(best) == 1, f"{adapter_id}: one best status expected, got {best}"
        assert SUPPORT_STATUSES.index(best[0]) == reached[adapter_id], adapter_id
        # The supported adapters are exactly the evidenced ones.
        assert (best[0] == SUPPORTED) is (adapter_id in SUPPORT_EVIDENCE), adapter_id
    assert {
        adapter_id for adapter_id, rank in reached.items() if rank == len(SUPPORT_STATUSES) - 1
    } == set(SUPPORT_EVIDENCE)


def test_the_matrix_cells_match_what_the_report_says_for_both_profiles() -> None:
    configs = _configs()
    reference = adapter_capabilities(configs["reference"])
    development = adapter_capabilities(configs["development"])

    for row in _table(_section(_page(), "## The support matrix")):
        adapter_id = _code(row[0])[0]
        for cell, report in ((row[3], reference), (row[4], development)):
            status, reason = _cell_status(cell)
            assert status == report[adapter_id]["status"], (adapter_id, cell)
            assert reason == report[adapter_id]["reason_code"], (adapter_id, cell)


def test_the_status_vocabulary_is_the_runtime_one() -> None:
    rows = _table(_section(_page(), '## What "supported" means here'))

    assert tuple(sorted(_code(row[0])[0] for row in rows)) == tuple(sorted(SUPPORT_STATUSES))


def test_the_evidence_table_is_exactly_the_pinned_evidence() -> None:
    rows = _table(_section(_page(), "### Evidence for each supported adapter"))

    cited = [(_code(row[0])[0], _code(row[1])[0]) for row in rows]
    pinned = [
        (adapter_id, scenario)
        for adapter_id, evidence in SUPPORT_EVIDENCE.items()
        for scenario in evidence.scenarios
    ]
    assert cited == pinned
    for row in rows:
        assert row[2], f"{row[1]}: say what the scenario demonstrates"


def test_the_pins_the_page_names_are_the_live_ones() -> None:
    text = _page()
    release = SUPPORT_EVIDENCE["release_evaluation"]

    assert release.implementation is not None
    assert release.implementation.split(":")[1] in text
    assert f"evidence schema version `{evidence_module.EVIDENCE_SCHEMA_VERSION}`" in text
    assert release.evidence_schema_version == evidence_module.EVIDENCE_SCHEMA_VERSION
    assert "caliber.profile.REFERENCE_CONNECTORS" in text
    assert "caliber.features.SUPPORT_EVIDENCE" in text


def test_the_also_needs_table_is_the_settings_the_report_checks() -> None:
    """Each supported adapter's conditions are derived from what the report says can change its answer.

    The table names every setting that is ever a supported adapter's ``disabled_by`` (so an omission fails) and nothing
    else (so an invented condition fails). ``CALIBER_PROFILE`` is the profile itself, described above the table.
    """
    rows = _table(_section(_page(), "## The supported shape"))
    by_adapter = {_code(row[0])[0]: row for row in rows}
    assert set(by_adapter) == set(SUPPORT_EVIDENCE)

    for adapter_id in SUPPORT_EVIDENCE:
        checked = {
            str(block["disabled_by"])
            for _name, block_adapter, block in _all_blocks()
            if block_adapter == adapter_id and str(block["disabled_by"]).startswith("CALIBER_")
        } - {"CALIBER_PROFILE"}
        named = set(re.findall(r"CALIBER_[A-Z_]+", by_adapter[adapter_id][2]))
        assert checked, adapter_id
        assert named == checked, (adapter_id, named ^ checked)


def test_the_page_says_supported_is_not_a_binding_and_the_profile_does_not_require_one() -> None:
    """The report cannot see the operator's wiring; if the profile ever starts requiring it, the page must change."""
    text = " ".join(_page().split())
    profile_source = Path(profile_module.__file__).read_text(encoding="utf-8")

    for variable in ("CALIBER_AGENT_STEP_TOOL_BACKENDS", "CALIBER_EFFECT_CALLBACK_CONNECTORS"):
        assert variable in text, variable
        assert variable not in profile_source, f"the profile now checks {variable}: update the page"
    assert "`supported` is not a statement that a binding exists" in text
    assert "connector_unbound" in text


def test_the_release_evaluation_evidence_is_described_as_the_sqlite_scenarios_they_are() -> None:
    """The page says those scenarios do not run on PostgreSQL; if they start to, the page must change."""
    scenarios = (
        CALIBER_DIR / "tests" / "egi_scenarios" / "test_v13_release_evaluation.py"
    ).read_text(encoding="utf-8")
    factory = (CALIBER_DIR / "tests" / "test_workspace_release_evaluation_execution.py").read_text(
        encoding="utf-8"
    )

    assert "from tests.test_workspace_release_evaluation_execution import _factory" in scenarios
    assert 'create_engine(f"sqlite:///' in factory and "Base.metadata.create_all(engine)" in factory
    assert "DIALECTS" not in scenarios
    text = " ".join(_page().split())
    assert "run on SQLite created from the models, not on PostgreSQL" in text
    assert "Only the connector scenarios have a PostgreSQL parameter" in text
    connector = (
        CALIBER_DIR / "tests" / "egi_scenarios" / "test_v07_restart_exactly_once.py"
    ).read_text(encoding="utf-8")
    assert "params=DIALECTS" in connector


def _functions(path: Path) -> set[str]:
    return {
        node.name
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_every_test_the_page_cites_exists() -> None:
    """A path that is not a file, or a ``::name`` that is not a test function, fails: the evidence is not decorative."""
    cited = set(re.findall(r"`(tests/[\w/]+\.py(?:::\w+)?)`", _page()))
    assert cited, "the page cites no test"
    for node_id in sorted(cited):
        relative, _, name = node_id.partition("::")
        source = CALIBER_DIR / relative
        assert source.is_file(), f"{node_id}: {relative} does not exist"
        if name:
            assert name.startswith("test_"), node_id
            assert name in _functions(source), f"{node_id}: no test function {name!r}"
    # Everything pinned is cited, wherever on the page.
    for evidence in SUPPORT_EVIDENCE.values():
        assert set(evidence.scenarios) <= cited


# ---------------------------------------------------------------------------
# Messages, codes and settings
# ---------------------------------------------------------------------------


def test_every_message_the_report_can_give_is_on_the_page_word_for_word() -> None:
    text = _page()
    missing = sorted(
        {
            f"{adapter_id} [{block['reason_code']}]: {block['message']}"
            for _name, adapter_id, block in _all_blocks()
            if str(block["message"]) not in text
        }
    )
    assert missing == []


def test_the_withdrawn_claim_messages_are_on_the_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pin that no longer matches the code withdraws a claim; the page quotes both sentences."""
    text = _page()
    reference = _configs()["reference"]

    changed = tuple(
        replace(record, capability_digest="sha256:" + "0" * 64)
        if record.backend_kind == "document_request"
        else record
        for record in profile_module.REFERENCE_CONNECTORS
    )
    monkeypatch.setattr(profile_module, "REFERENCE_CONNECTORS", changed)
    connector = adapter_capabilities(reference)["document_request_connector"]
    monkeypatch.undo()
    assert connector["reason_code"] == "connector_capability_mismatch"
    assert connector["message"] in text

    monkeypatch.setattr(
        evidence_module, "EVIDENCE_SCHEMA_VERSION", evidence_module.EVIDENCE_SCHEMA_VERSION + 1
    )
    evaluator = adapter_capabilities(reference)["release_evaluation"]
    monkeypatch.undo()
    assert evaluator["reason_code"] == "evaluator_evidence_mismatch"
    assert evaluator["message"] in text

    monkeypatch.setattr(profile_module, "REFERENCE_CONNECTORS", ())
    assert adapter_capabilities(reference)["document_request_connector"]["reason_code"] == (
        "connector_not_admitted"
    )
    assert "connector_not_admitted" in text


def test_every_reason_code_and_setting_the_report_names_is_on_the_page() -> None:
    text = _page()
    codes = {
        value
        for name, value in vars(features_module).items()
        if name.startswith("REASON_") and isinstance(value, str)
    }
    assert "feature_disabled" in codes and "no_support_evidence" in codes
    assert sorted(code for code in codes if f"`{code}`" not in text and code not in text) == []

    settings = {
        value
        for name, value in vars(features_module).items()
        if name.endswith("_ENV_VAR") and isinstance(value, str)
    }
    settings |= {
        flag.env_var for flag in FEATURES if flag.id in {"integrations", "release_candidates"}
    }
    assert {"CALIBER_PROFILE", "CALIBER_EVAL_PROVIDER"} <= settings
    assert sorted(setting for setting in settings if setting not in text) == []


def test_the_startup_refusal_line_the_page_quotes_is_the_profiles() -> None:
    violations = reference_profile_violations(
        CaliberConfig().model_copy(update={**SAFE, "csrf_enabled": False})
    )

    assert [violation.code for violation in violations] == ["csrf_disabled"]
    assert f"- {violations[0].message} [csrf_disabled]" in _page()


def test_the_disabled_area_404_the_page_quotes_is_the_gates() -> None:
    flag = next(flag for flag in FEATURES if flag.id == "release_candidates")
    detail = features_module.disabled_message(flag)
    body = json.dumps(
        {"detail": detail, "status_code": 404, "reason_code": features_module.REASON_CODE}
    )

    assert body in _page()


def test_the_no_authoritative_verifier_refusals_are_the_runtimes() -> None:
    source = RUNTIME.read_text(encoding="utf-8")
    text = _page()

    assert 'raise ToolExecutionError(f"{label} refused: no_authoritative_verifier")' in source
    for label in ("HTTP effect", "registered tool effect", "OpenAPI effect", "MCP effect"):
        assert f'label="{label}"' in source, label
        assert f"{label} refused: no_authoritative_verifier" in text, label
    # Each connector the page names is one the profile pins.
    kinds = {record.backend_kind for record in profile_module.REFERENCE_CONNECTORS}
    assert {"document_request", "http", "registered_tool", "openapi_http", "mcp"} <= kinds


def test_the_profile_lists_no_mcp_command_or_host() -> None:
    assert profile_module.REFERENCE_MCP_ALLOWLIST == ()
    assert "`caliber.profile.REFERENCE_MCP_ALLOWLIST` is empty" in " ".join(_page().split())


# ---------------------------------------------------------------------------
# Known limits
# ---------------------------------------------------------------------------

_COUNT_WORDS = {13: "Thirteen", 14: "Fourteen", 15: "Fifteen", 16: "Sixteen", 12: "Twelve"}


def _unrouted() -> dict[str, str]:
    cases = json.loads(MATRIX.read_text(encoding="utf-8"))["cases"]
    return {
        row_id: case["owner"] for row_id, case in cases.items() if case.get("status") == "unrouted"
    }


def test_the_unrouted_rows_are_the_matrixs_with_their_owners() -> None:
    unrouted = _unrouted()
    text = _page()
    assert unrouted, (
        "the matrix has no unrouted row: delete the known limit instead of keeping it stale"
    )
    assert len(unrouted) in _COUNT_WORDS, "extend _COUNT_WORDS and the page's count"
    assert f"{_COUNT_WORDS[len(unrouted)]} protected-effect rows are not routed" in text

    rows = _table(_section(text, "## Known limits"))
    listed: dict[str, str] = {}
    for row in rows:
        issues = re.findall(r"#\d+", row[2])
        assert len(issues) == 1, row
        for row_id in re.findall(r"EP-\d+", row[0]):
            assert row_id not in listed, f"{row_id} listed twice"
            listed[row_id] = issues[0]

    assert set(listed) == set(unrouted)
    for row_id, owner in unrouted.items():
        assert owner.startswith(f"{listed[row_id]}:"), (row_id, owner, listed[row_id])


def test_the_limits_name_the_open_work_they_depend_on() -> None:
    text = _page()
    for issue in ("#1054", "#1074", "#1086", "#1087", "#1088", "#1063", "#771", "#877", "#613"):
        assert issue in text, issue


# ---------------------------------------------------------------------------
# The page is a Beta page, and the alpha pages stay history
# ---------------------------------------------------------------------------


def test_no_page_claims_a_general_availability_status() -> None:
    text = _page()

    assert re.search(r"^stability: beta$", text, re.MULTILINE)
    for pattern in (
        r"\bGA\b",
        r"general(?:ly)?[- ]availab",
        r"production[- ]ready",
        r"enterprise[- ]grade",
        r"fully supported",
        r"battle[- ]tested",
    ):
        assert not re.search(pattern, text, re.IGNORECASE), pattern


def test_the_page_is_registered_in_the_docs_site() -> None:
    manifest = BUILD_DOCS.read_text(encoding="utf-8")
    site_map = json.loads(SITE_MAP.read_text(encoding="utf-8"))

    assert 'md: "start/beta-scope.md"' in manifest
    assert any(page["href"] == "m-66-beta-scope.html" for page in site_map["pages"])


def test_the_alpha_pages_stay_historical_and_point_at_the_beta_page() -> None:
    for path in (ALPHA_SCOPE, ALPHA_INSTALL):
        text = path.read_text(encoding="utf-8")
        assert "version_applicability: 0.1.0a1 alpha" in text, path.name
        assert "**Historical page" in text, path.name
        assert "](beta-scope.md)" in text, path.name
    # The alpha install no longer says a workspace release evaluation never runs.
    assert "evaluation never runs" not in ALPHA_INSTALL.read_text(encoding="utf-8")


def test_the_operator_and_api_pages_point_at_the_beta_page() -> None:
    reference = " ".join(REFERENCE_PAGE.read_text(encoding="utf-8").split())

    assert "](../start/beta-scope.md)" in reference
    assert "`auto` never reaches this rule" in reference
    api = API_REFERENCE.read_text(encoding="utf-8")
    assert "`adapters`" in api and "](../start/beta-scope.md)" in api
    for adapter_id in ADAPTER_IDS:
        assert f"`{adapter_id}`" in api, adapter_id


def test_grid_reaches_every_adapter_status_the_page_describes() -> None:
    """The grid behind the checks above is not vacuous: it reaches each status for the adapters that can."""
    seen = {(adapter_id, str(block["status"])) for _name, adapter_id, block in _all_blocks()}

    assert ("document_request_connector", "supported") in seen
    assert ("release_evaluation", "supported") in seen
    assert ("mcp_servers", "preview") in seen and ("mcp_servers", "unavailable") in seen
    assert ("openapi_integrations", "preview") in seen
    assert ("openapi_integrations", "supported") not in seen
    assert ("mcp_servers", "supported") not in seen
