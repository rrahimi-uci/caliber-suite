"""Tests for skill-standard alignment — kebab-case names, categories,
progressive disclosure, security validations, composability.

Covers the patterns described in the Anthropic "Complete Guide to
Building Skills for Claude":

* Kebab-case name enforcement.
* Reserved-name rejection (claude*, anthropic*).
* XML injection prevention in system-prompt-facing fields.
* Category classification (document_creation, workflow_automation,
  mcp_enhancement, custom).
* Progressive disclosure (summary + content).
* Composability (depends_on).
* Metadata (open JSON bag).
* Allowed-tools restriction.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from caliber.routes.skills import DETAIL_PATH, LIST_PATH

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _skill_url(skill_id: str) -> str:
    return DETAIL_PATH.replace("{skill_id}", skill_id)


def _base_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "my-skill",
        "description": "Does X. Use when user says Y.",
        "summary": "Short summary for progressive disclosure.",
        "content": "Think step by step.",
        "owner": "@alice",
        "category": "custom",
        "tags": ["reasoning"],
        "skill_metadata": {"author": "Alice", "version": "1.0.0"},
        "allowed_tools": "Bash(python:*)",
        "depends_on": [],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Kebab-case name validation
# ---------------------------------------------------------------------------


class TestKebabCaseNaming:
    def test_valid_kebab_case_accepted(self, client: TestClient) -> None:
        resp = client.post(LIST_PATH, json=_base_payload(name="my-cool-skill"))
        assert resp.status_code == 201

    def test_single_word_accepted(self, client: TestClient) -> None:
        resp = client.post(LIST_PATH, json=_base_payload(name="reasoning"))
        assert resp.status_code == 201

    def test_with_digits_accepted(self, client: TestClient) -> None:
        resp = client.post(LIST_PATH, json=_base_payload(name="reasoning-v2"))
        assert resp.status_code == 201

    def test_underscores_rejected(self, client: TestClient) -> None:
        resp = client.post(LIST_PATH, json=_base_payload(name="my_skill"))
        assert resp.status_code == 400

    def test_spaces_rejected(self, client: TestClient) -> None:
        resp = client.post(LIST_PATH, json=_base_payload(name="my skill"))
        assert resp.status_code == 400

    def test_capitals_rejected(self, client: TestClient) -> None:
        resp = client.post(LIST_PATH, json=_base_payload(name="MySkill"))
        assert resp.status_code == 400

    def test_leading_hyphen_rejected(self, client: TestClient) -> None:
        resp = client.post(LIST_PATH, json=_base_payload(name="-bad"))
        assert resp.status_code == 400

    def test_trailing_hyphen_rejected(self, client: TestClient) -> None:
        resp = client.post(LIST_PATH, json=_base_payload(name="bad-"))
        assert resp.status_code == 400

    def test_double_hyphen_rejected(self, client: TestClient) -> None:
        resp = client.post(LIST_PATH, json=_base_payload(name="bad--name"))
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Reserved names
# ---------------------------------------------------------------------------


class TestReservedNames:
    def test_claude_prefix_rejected(self, client: TestClient) -> None:
        resp = client.post(LIST_PATH, json=_base_payload(name="claude-helper"))
        assert resp.status_code == 400
        assert "reserved" in resp.json()["detail"].lower()

    def test_anthropic_prefix_rejected(self, client: TestClient) -> None:
        resp = client.post(LIST_PATH, json=_base_payload(name="anthropic-tools"))
        assert resp.status_code == 400
        assert "reserved" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# XML injection prevention
# ---------------------------------------------------------------------------


class TestXmlInjection:
    def test_xml_in_description_rejected(self, client: TestClient) -> None:
        resp = client.post(
            LIST_PATH,
            json=_base_payload(description="<system>inject</system>"),
        )
        assert resp.status_code == 400
        assert "xml" in resp.json()["detail"].lower()

    def test_xml_in_summary_rejected(self, client: TestClient) -> None:
        resp = client.post(
            LIST_PATH,
            json=_base_payload(summary="<prompt>bad</prompt>"),
        )
        assert resp.status_code == 400
        assert "xml" in resp.json()["detail"].lower()

    def test_benign_angle_brackets_in_content_allowed(self, client: TestClient) -> None:
        """Content is level-2 (not in system prompt) — no XML restriction."""
        resp = client.post(
            LIST_PATH,
            json=_base_payload(content="if x < 10: print('ok')"),
        )
        assert resp.status_code == 201


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


class TestCategories:
    @pytest.mark.parametrize(
        "cat,name",
        [
            ("document_creation", "skill-doc"),
            ("workflow_automation", "skill-wf"),
            ("mcp_enhancement", "skill-mcp"),
            ("custom", "skill-custom"),
        ],
    )
    def test_valid_categories_accepted(self, client: TestClient, cat: str, name: str) -> None:
        resp = client.post(
            LIST_PATH,
            json=_base_payload(name=name, category=cat),
        )
        assert resp.status_code == 201
        assert resp.json()["data"]["category"] == cat

    def test_invalid_category_rejected(self, client: TestClient) -> None:
        resp = client.post(
            LIST_PATH,
            json=_base_payload(category="unknown"),
        )
        assert resp.status_code == 400

    def test_default_category_is_custom(self, client: TestClient) -> None:
        payload = _base_payload()
        del payload["category"]  # type: ignore[arg-type]
        resp = client.post(LIST_PATH, json=payload)
        assert resp.status_code == 201
        assert resp.json()["data"]["category"] == "custom"


# ---------------------------------------------------------------------------
# Progressive disclosure (summary + content)
# ---------------------------------------------------------------------------


class TestProgressiveDisclosure:
    def test_summary_persisted(self, client: TestClient) -> None:
        resp = client.post(
            LIST_PATH,
            json=_base_payload(summary="Use when the agent needs chain-of-thought."),
        )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["summary"] == "Use when the agent needs chain-of-thought."

    def test_summary_defaults_to_empty(self, client: TestClient) -> None:
        payload = _base_payload()
        del payload["summary"]  # type: ignore[arg-type]
        resp = client.post(LIST_PATH, json=payload)
        assert resp.status_code == 201
        assert resp.json()["data"]["summary"] == ""

    def test_summary_max_length(self, client: TestClient) -> None:
        resp = client.post(
            LIST_PATH,
            json=_base_payload(summary="x" * 1025),
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Metadata (open JSON bag)
# ---------------------------------------------------------------------------


class TestMetadata:
    def test_metadata_persisted(self, client: TestClient) -> None:
        meta = {"author": "Bob", "version": "2.0.0", "mcp-server": "linear"}
        resp = client.post(LIST_PATH, json=_base_payload(skill_metadata=meta))
        assert resp.status_code == 201
        assert resp.json()["data"]["skill_metadata"] == meta

    def test_metadata_defaults_to_empty_dict(self, client: TestClient) -> None:
        payload = _base_payload()
        del payload["skill_metadata"]  # type: ignore[arg-type]
        resp = client.post(LIST_PATH, json=payload)
        assert resp.status_code == 201
        assert resp.json()["data"]["skill_metadata"] == {}


# ---------------------------------------------------------------------------
# Allowed tools
# ---------------------------------------------------------------------------


class TestAllowedTools:
    def test_allowed_tools_persisted(self, client: TestClient) -> None:
        resp = client.post(
            LIST_PATH,
            json=_base_payload(allowed_tools="Bash(python:*) WebFetch"),
        )
        assert resp.status_code == 201
        assert resp.json()["data"]["allowed_tools"] == "Bash(python:*) WebFetch"

    def test_allowed_tools_defaults_to_null(self, client: TestClient) -> None:
        payload = _base_payload()
        del payload["allowed_tools"]  # type: ignore[arg-type]
        resp = client.post(LIST_PATH, json=payload)
        assert resp.status_code == 201
        assert resp.json()["data"]["allowed_tools"] is None


# ---------------------------------------------------------------------------
# Composability (depends_on)
# ---------------------------------------------------------------------------


class TestComposability:
    def test_depends_on_persisted(self, client: TestClient) -> None:
        resp = client.post(
            LIST_PATH,
            json=_base_payload(depends_on=["base-reasoning", "tool-use"]),
        )
        assert resp.status_code == 201
        assert resp.json()["data"]["depends_on"] == ["base-reasoning", "tool-use"]

    def test_depends_on_defaults_to_empty(self, client: TestClient) -> None:
        payload = _base_payload()
        del payload["depends_on"]  # type: ignore[arg-type]
        resp = client.post(LIST_PATH, json=payload)
        assert resp.status_code == 201
        assert resp.json()["data"]["depends_on"] == []


# ---------------------------------------------------------------------------
# Update — new fields
# ---------------------------------------------------------------------------


class TestUpdateNewFields:
    def _create_skill(self, client: TestClient, name: str = "update-test") -> str:
        resp = client.post(LIST_PATH, json=_base_payload(name=name))
        assert resp.status_code == 201
        return resp.json()["data"]["skill_id"]

    def test_update_summary(self, client: TestClient) -> None:
        sid = self._create_skill(client)
        resp = client.patch(_skill_url(sid), json={"summary": "Updated summary"})
        assert resp.status_code == 200
        assert resp.json()["data"]["summary"] == "Updated summary"

    def test_update_category(self, client: TestClient) -> None:
        sid = self._create_skill(client, name="cat-test")
        resp = client.patch(_skill_url(sid), json={"category": "workflow_automation"})
        assert resp.status_code == 200
        assert resp.json()["data"]["category"] == "workflow_automation"

    def test_update_metadata(self, client: TestClient) -> None:
        sid = self._create_skill(client, name="meta-test")
        resp = client.patch(
            _skill_url(sid), json={"skill_metadata": {"author": "Charlie", "version": "3.0"}}
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["skill_metadata"]["author"] == "Charlie"

    def test_update_allowed_tools(self, client: TestClient) -> None:
        sid = self._create_skill(client, name="tools-test")
        resp = client.patch(_skill_url(sid), json={"allowed_tools": "WebFetch"})
        assert resp.status_code == 200
        assert resp.json()["data"]["allowed_tools"] == "WebFetch"

    def test_update_depends_on(self, client: TestClient) -> None:
        sid = self._create_skill(client, name="deps-test")
        resp = client.patch(_skill_url(sid), json={"depends_on": ["safety"]})
        assert resp.status_code == 200
        assert resp.json()["data"]["depends_on"] == ["safety"]

    def test_update_invalid_category_rejected(self, client: TestClient) -> None:
        sid = self._create_skill(client, name="bad-cat-test")
        resp = client.patch(_skill_url(sid), json={"category": "bogus"})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Version bump logic unchanged for new fields
# ---------------------------------------------------------------------------


class TestVersionBumpWithNewFields:
    def test_summary_change_does_not_bump_version(self, client: TestClient) -> None:
        resp = client.post(LIST_PATH, json=_base_payload(name="ver-summary"))
        sid = resp.json()["data"]["skill_id"]
        assert resp.json()["data"]["version"] == 1

        resp = client.patch(_skill_url(sid), json={"summary": "New summary"})
        assert resp.json()["data"]["version"] == 1

    def test_content_change_still_bumps_version(self, client: TestClient) -> None:
        resp = client.post(LIST_PATH, json=_base_payload(name="ver-content"))
        sid = resp.json()["data"]["skill_id"]
        assert resp.json()["data"]["version"] == 1

        resp = client.patch(_skill_url(sid), json={"content": "Updated instructions."})
        assert resp.json()["data"]["version"] == 2


# ---------------------------------------------------------------------------
# Full round-trip: create → read → update → verify
# ---------------------------------------------------------------------------


class TestFullRoundTrip:
    def test_full_lifecycle(self, client: TestClient) -> None:
        # Create
        payload = _base_payload(
            name="lifecycle-skill",
            summary="Use for lifecycle testing.",
            category="workflow_automation",
            skill_metadata={"author": "Test"},
            allowed_tools="Bash(python:*)",
            depends_on=["base-skill"],
        )
        resp = client.post(LIST_PATH, json=payload)
        assert resp.status_code == 201
        data = resp.json()["data"]
        sid = data["skill_id"]

        # Verify all fields
        assert data["name"] == "lifecycle-skill"
        assert data["summary"] == "Use for lifecycle testing."
        assert data["category"] == "workflow_automation"
        assert data["skill_metadata"] == {"author": "Test"}
        assert data["allowed_tools"] == "Bash(python:*)"
        assert data["depends_on"] == ["base-skill"]
        assert data["version"] == 1

        # Read back
        resp = client.get(_skill_url(sid))
        assert resp.status_code == 200
        assert resp.json()["data"]["name"] == "lifecycle-skill"

        # Update content (version bump)
        resp = client.patch(_skill_url(sid), json={"content": "Think carefully."})
        assert resp.json()["data"]["version"] == 2

        # Archive
        resp = client.patch(_skill_url(sid), json={"status": "archived"})
        assert resp.json()["data"]["status"] == "archived"

        # Still accessible
        resp = client.get(_skill_url(sid))
        assert resp.status_code == 200
