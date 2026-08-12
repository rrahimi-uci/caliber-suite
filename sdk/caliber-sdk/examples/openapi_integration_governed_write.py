"""Import a write-capable OpenAPI spec and publish a tool CALIBER gates behind approval.

The scenario: a ticketing API exposes ``POST /tickets`` (creates a ticket).
Unlike the read-only cookbook, this walks through the guardrails a write
operation actually hits: it cannot be fired through preview until the
operator explicitly opts in, and the published tool carries
``requires_approval=True`` so a workflow binding it stops for a human
decision rather than running unattended.
"""

from __future__ import annotations

from typing import Any

from caliber_sdk import CaliberClient, CaliberConflictError

TICKETING_SPEC = """
openapi: 3.0.3
info:
  title: Ticketing API
  version: "1"
servers:
  - url: https://tickets.example.com
components:
  securitySchemes:
    bearerAuth:
      type: http
      scheme: bearer
security:
  - bearerAuth: []
paths:
  /tickets:
    post:
      operationId: createTicket
      summary: Create a support ticket
      tags: [tickets]
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              properties:
                title:
                  type: string
              required: [title]
      responses:
        "201":
          description: created
"""


def import_and_publish_governed_write_tool(caliber: CaliberClient) -> dict[str, Any]:
    """Curate the write operation, confirm the preview gate holds, then publish.

    Returns the published tool's id and its ``requires_approval`` flag so a
    caller can see the property the whole example exists to demonstrate:
    curation does not weaken the platform's write-approval guarantees.
    """
    integration = caliber.openapi_integrations.create(
        "Ticketing", description="Support ticket creation"
    )
    version = caliber.openapi_integrations.import_spec(
        integration.integration_id, spec_text=TICKETING_SPEC
    )

    operations = caliber.openapi_integrations.list_operations(
        integration.integration_id, version_id=version.version_id
    )
    create_ticket = next(op for op in operations if op.operation_key == "POST /tickets")
    assert create_ticket.side_effect_level == "write"

    drafts = caliber.openapi_integrations.generate_tool_drafts(
        integration.integration_id,
        operation_ids=[create_ticket.operation_id],
        auth_binding={"kind": "bearer", "secret_ref": "env://TICKETING_TOKEN"},
        # Deliberately omitted: allow_in_preview. A write draft defaults to
        # requires_approval=True and stays un-previewable until an operator
        # opts in — see the refusal this provokes below.
    )
    draft = drafts[0]
    assert draft.requires_approval is True

    try:
        caliber.openapi_integrations.preview_tool_draft(
            integration.integration_id, draft.draft_id, input={"body": {"title": "test"}}
        )
        raise AssertionError("preview should have been refused for a non-previewable draft")
    except CaliberConflictError as refused:
        # 409: "set 'allow_in_preview' before running a live upstream call."
        assert refused.status_code == 409

    published = caliber.openapi_integrations.publish_tool_draft(
        integration.integration_id, draft.draft_id, version="1.0"
    )

    return {
        "integration_id": integration.integration_id,
        "tool_id": published["tool"]["tool_id"],
        "tool_name": published["tool"]["name"],
        "side_effect_level": published["tool"].get("side_effect_level", "write"),
        "requires_approval": published["draft"]["requires_approval"],
    }
