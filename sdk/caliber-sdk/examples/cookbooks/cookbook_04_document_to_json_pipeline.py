"""Install Cookbook 04, upload the source document, and validate the draft."""

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
    validation = caliber.raw.post(
        f"/workflow-versions/{installed['version']['version_id']}/validate",
        json={"example_input": {"project_file_id": uploaded.file_id}},
    )
    return {
        "installed": recipe.id,
        "project_id": project.project_id,
        "file_id": uploaded.file_id,
        "version_id": installed["version"]["version_id"],
        "validation": validation,
    }


def main() -> None:
    with env_client() as caliber:
        print(json.dumps(run(caliber), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
