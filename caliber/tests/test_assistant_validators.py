"""Tests for caliber.assistant.validators."""

from __future__ import annotations

from caliber.assistant.validators import (
    validate_draft,
    validate_mcp_server_draft,
    validate_prompt_draft,
    validate_skill_draft,
    validate_tool_draft,
    validate_workflow_draft,
)

# ---------------------------------------------------------------------------
# Tool validator
# ---------------------------------------------------------------------------


class TestToolValidator:
    def test_valid_tool(self):
        report = validate_tool_draft(
            {
                "name": "greet",
                "source": "def greet(x: str) -> str:\n    return f'Hello, {x}'\n",
                "input_schema": {"type": "object"},
            }
        )
        assert report.valid
        assert not report.errors

    def test_missing_source(self):
        report = validate_tool_draft({"name": "greet"})
        assert not report.valid
        assert any("source" in e.lower() for e in report.errors)

    def test_missing_name(self):
        report = validate_tool_draft({"source": "x = 1"})
        assert not report.valid
        assert any("name" in e.lower() for e in report.errors)

    def test_syntax_error_in_source(self):
        report = validate_tool_draft(
            {
                "name": "bad",
                "source": "def bad(:\n",
            }
        )
        assert not report.valid
        assert any("syntax" in e.lower() for e in report.errors)

    def test_source_too_large(self):
        report = validate_tool_draft(
            {"name": "big", "source": "x = 1\n" * 100_000},
            max_source_bytes=100,
        )
        assert not report.valid
        assert any("byte limit" in e.lower() for e in report.errors)

    def test_inline_secret_detected(self):
        report = validate_tool_draft(
            {
                "name": "leaky",
                "source": 'api_key = "sk-supersecret12345678"\n',
            }
        )
        assert not report.valid
        assert any("secret" in e.lower() for e in report.errors)

    def test_no_input_schema_warning(self):
        report = validate_tool_draft(
            {
                "name": "greet",
                "source": "def greet(): pass\n",
            }
        )
        assert report.valid
        assert len(report.warnings) > 0


# ---------------------------------------------------------------------------
# Skill validator
# ---------------------------------------------------------------------------


class TestSkillValidator:
    def test_valid_skill(self):
        report = validate_skill_draft({"name": "my-skill", "prompt": "You are helpful."})
        assert report.valid

    def test_missing_name(self):
        report = validate_skill_draft({"prompt": "hello"})
        assert not report.valid

    def test_missing_prompt_and_description(self):
        report = validate_skill_draft({"name": "x"})
        assert not report.valid

    def test_bad_metadata_type(self):
        report = validate_skill_draft({"name": "x", "prompt": "hi", "metadata": "string"})
        assert not report.valid


# ---------------------------------------------------------------------------
# Prompt validator
# ---------------------------------------------------------------------------


class TestPromptValidator:
    def test_valid_prompt(self):
        report = validate_prompt_draft(
            {
                "template": "Hello, {{name}}!",
                "variables": ["name"],
            }
        )
        assert report.valid

    def test_missing_template(self):
        report = validate_prompt_draft({"variables": []})
        assert not report.valid

    def test_undeclared_variable(self):
        report = validate_prompt_draft(
            {
                "template": "Hello, {{name}}! Your id is {{id}}.",
                "variables": ["name"],
            }
        )
        assert not report.valid
        assert any("undeclared" in e.lower() for e in report.errors)

    def test_extra_variable_warning(self):
        report = validate_prompt_draft(
            {
                "template": "Hello, {{name}}!",
                "variables": ["name", "unused"],
            }
        )
        assert report.valid
        assert len(report.warnings) > 0

    def test_inline_secret_in_template(self):
        report = validate_prompt_draft(
            {
                "template": 'Use api_key = "sk-mysupersecretkey123"',
                "variables": [],
            }
        )
        assert not report.valid


# ---------------------------------------------------------------------------
# Workflow validator
# ---------------------------------------------------------------------------


class TestWorkflowValidator:
    def test_valid_workflow(self):
        report = validate_workflow_draft(
            {
                "manifest": {"version": "1.0", "steps": [{"id": "s1"}]},
            }
        )
        assert report.valid

    def test_missing_manifest(self):
        report = validate_workflow_draft({})
        assert not report.valid

    def test_manifest_not_dict(self):
        report = validate_workflow_draft({"manifest": "string"})
        assert not report.valid

    def test_manifest_missing_steps(self):
        report = validate_workflow_draft({"manifest": {"version": "1.0"}})
        assert not report.valid

    def test_missing_version_warning(self):
        report = validate_workflow_draft({"manifest": {"steps": []}})
        assert report.valid  # just a warning
        assert len(report.warnings) > 0


# ---------------------------------------------------------------------------
# MCP server validator
# ---------------------------------------------------------------------------


class TestMcpServerValidator:
    def test_valid_stdio(self):
        report = validate_mcp_server_draft(
            {
                "name": "my-server",
                "transport": "stdio",
                "command": "npx my-server",
            }
        )
        assert report.valid

    def test_valid_sse(self):
        report = validate_mcp_server_draft(
            {
                "name": "my-server",
                "transport": "sse",
                "uri": "http://localhost:8080",
            }
        )
        assert report.valid

    def test_missing_name(self):
        report = validate_mcp_server_draft({"transport": "stdio", "command": "x"})
        assert not report.valid

    def test_invalid_transport(self):
        report = validate_mcp_server_draft(
            {
                "name": "x",
                "transport": "websocket",
            }
        )
        assert not report.valid

    def test_stdio_missing_command(self):
        report = validate_mcp_server_draft(
            {
                "name": "x",
                "transport": "stdio",
            }
        )
        assert not report.valid

    def test_sse_missing_uri(self):
        report = validate_mcp_server_draft(
            {
                "name": "x",
                "transport": "sse",
            }
        )
        assert not report.valid

    def test_valid_streamable_http(self):
        report = validate_mcp_server_draft(
            {
                "name": "my-server",
                "transport": "streamable-http",
                "uri": "http://localhost:8080/mcp",
            }
        )
        assert report.valid

    def test_streamable_http_missing_uri(self):
        report = validate_mcp_server_draft(
            {
                "name": "x",
                "transport": "streamable-http",
            }
        )
        assert not report.valid

    def test_empty_name(self):
        report = validate_mcp_server_draft(
            {
                "name": "",
                "transport": "stdio",
                "command": "npx my-server",
            }
        )
        assert not report.valid


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class TestValidateDraftDispatcher:
    def test_unknown_type(self):
        report = validate_draft("unknown_type", {})
        assert not report.valid
        assert any("unknown" in e.lower() for e in report.errors)

    def test_tool_dispatch(self):
        report = validate_draft(
            "tool",
            {
                "name": "x",
                "source": "x = 1\n",
            },
        )
        assert report.valid

    def test_max_source_bytes_kwarg(self):
        report = validate_draft(
            "tool",
            {"name": "x", "source": "x" * 200},
            max_source_bytes=100,
        )
        assert not report.valid
