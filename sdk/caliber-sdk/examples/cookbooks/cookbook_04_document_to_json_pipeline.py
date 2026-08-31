"""Install Cookbook 04, upload the source document, and preview-run the
draft against it.
"""

from __future__ import annotations

import io
import json

from caliber_sdk import CaliberClient
from examples.cookbooks._helpers import configuration_blockers, env_client, get_recipe


def run(caliber: CaliberClient) -> dict[str, object]:
    recipe = get_recipe(caliber, "04")
    blockers = configuration_blockers(recipe)
    if blockers:
        return {"installed": None, "blocked_by": blockers}

    project = caliber.projects.create(
        "cookbook-04-documents",
        description="Managed source documents for the document-to-JSON pipeline",
    )
    uploaded = caliber.projects.files.upload(
        project.project_id,
        filename="invoice.pdf",
        content=io.BytesIO(b"%PDF-1.4\n% cookbook 04 example\n"),
        path="incoming/invoice.pdf",
        media_type="application/pdf",
    )
    installed = caliber.cookbooks.install(
        "04",
        name="Cookbook 04 — Document-to-JSON Pipeline (SDK)",
        acknowledge_prerequisites=bool(recipe.prerequisites),
    )
    # `validate()` only checks the manifest's own structure -- it never reads
    # a request body, so passing an example input to it is a silent no-op.
    # `preview_run()` is the call that actually exercises the draft against
    # real input, in preview mode (no persisted run, no tool side effects).
    preview = caliber.workflows.versions.preview_run(
        installed["version"]["version_id"],
        input={"project_file_id": uploaded.file_id},
    )
    return {
        "installed": recipe.id,
        "project_id": project.project_id,
        "file_id": uploaded.file_id,
        "version_id": installed["version"]["version_id"],
        "preview": preview,
    }


def main() -> None:
    with env_client() as caliber:
        print(json.dumps(run(caliber), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
