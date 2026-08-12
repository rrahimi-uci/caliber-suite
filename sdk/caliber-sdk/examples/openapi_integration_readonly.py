"""Import a read-only enterprise OpenAPI spec and publish a governed tool from it.

The scenario: a status-page API exposes a single read-only operation
(``GET /incidents/{incident_id}``). This walks the whole curation pipeline —
import, review, curate, preview, publish — for the case where no approval
gate is needed because nothing here writes anything.
"""

from __future__ import annotations

from typing import Any

from caliber_sdk import CaliberClient

STATUS_PAGE_SPEC = """
openapi: 3.0.3
info:
  title: Status Page API
  version: "1"
servers:
  - url: https://status.example.com
components:
  securitySchemes:
    bearerAuth:
      type: http
      scheme: bearer
security:
  - bearerAuth: []
paths:
  /incidents/{incident_id}:
    get:
      operationId: getIncident
      summary: Get one incident by id
      tags: [incidents]
      parameters:
        - in: path
          name: incident_id
          required: true
          schema:
            type: string
      responses:
        "200":
          description: ok
"""


def import_and_publish_readonly_tool(caliber: CaliberClient) -> dict[str, Any]:
    """Import the spec, generate a tool draft for the read operation, and publish it.

    Returns the published tool's id and the operation it wraps, so a caller can
    bind it into a workflow or hand its id to Aria's ``openapi_tool.invoke``
    capability immediately.
    """
    integration = caliber.openapi_integrations.create(
        "Status Page", description="Read-only incident status lookups"
    )

    # Importing pins a version; nothing is callable yet. That distinction is
    # the whole point of the staged pipeline — a spec import is not a publish.
    version = caliber.openapi_integrations.import_spec(
        integration.integration_id, spec_text=STATUS_PAGE_SPEC
    )

    operations = caliber.openapi_integrations.list_operations(
        integration.integration_id, version_id=version.version_id
    )
    get_incident = next(
        op for op in operations if op.operation_key == "GET /incidents/{incident_id}"
    )
    assert get_incident.side_effect_level == "read"  # classified from the HTTP method, not asserted

    drafts = caliber.openapi_integrations.generate_tool_drafts(
        integration.integration_id,
        operation_ids=[get_incident.operation_id],
        auth_binding={"kind": "bearer", "secret_ref": "env://STATUS_PAGE_TOKEN"},
        # Safe to preview: nothing this operation does has a side effect.
        allow_in_preview=True,
    )
    draft = drafts[0]

    preview = caliber.openapi_integrations.preview_tool_draft(
        integration.integration_id,
        draft.draft_id,
        input={"path_params": {"incident_id": "INC-1"}},
    )

    published = caliber.openapi_integrations.publish_tool_draft(
        integration.integration_id, draft.draft_id, version="1.0"
    )

    return {
        "integration_id": integration.integration_id,
        "tool_id": published["tool"]["tool_id"],
        "tool_name": published["tool"]["name"],
        "operation_key": get_incident.operation_key,
        "preview_status_code": preview["result"]["status_code"],
    }
