"""Bind a project's GitHub App connection, then land a CI-triggered push import.

This is Phase 4's own "GitHub Action and SDK example for push import using a
project-bound CALIBER PAT" (docs/workspace-plan.md section 16, Phase 4 item 8):
a repository's CI pipeline validates its own source and invokes a CALIBER
import with a workspace-bound short-lived credential (section 6.5.4) — it
never pushes application state to CALIBER directly, and CALIBER never fetches
the repository itself for a ``push``-mode source.

Two things happen here, and they are independent of each other:

* :meth:`~caliber_sdk.resources.projects.ProjectSourceConnectionAPI.configure`
  binds the project's GitHub App connection, which is what lets CALIBER
  verify commit/PR/review evidence for native Change Request review later
  (`P4-E`). A project can import without ever configuring this.
* :meth:`~caliber_sdk.resources.projects.ProjectImportsAPI.create` submits the
  actual push import — a canonical ZIP bundle plus the commit it was built
  from — exactly what the sibling GitHub Actions workflow
  (``.github/workflows/caliber-import.yml`` in the calling repository, shown
  in ``docs/sdk/beta.md``) runs on every push, authenticated by a
  project-bound CALIBER PAT rather than a session.

``ProjectImportsAPI.wait()`` treats ``reconcile_required`` as terminal, the
same "stopped is not finished" property documented for every other beta
waiter on this page: an import CALIBER cannot safely retry on its own needs
an operator to call :meth:`~caliber_sdk.resources.projects.ProjectImportsAPI.reconcile`,
not a longer timeout.
"""

from __future__ import annotations

from typing import Any

from caliber_sdk import CaliberClient


def connect_github_source_and_import_on_push(
    caliber: CaliberClient,
    *,
    project_id: str = "PRJ-1",
    repository: str = "octo-org/mortgage-underwriting",
    commit_sha: str = "a" * 40,
    bundle: bytes = b"PK\x05\x06" + b"\x00" * 18,  # minimal empty-ZIP end-of-central-directory
) -> dict[str, Any]:
    """Configure the project's GitHub App connection, then submit and wait for
    the push import a CI pipeline performs on every push."""
    connection = caliber.workspaces.source_connection.configure(
        project_id,
        app_id="123456",
        installation_id="789012",
        # Write-only: accepted here, stored through the server's encrypted
        # secret store, and never echoed back by any read.
        private_key="-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----\n",
        webhook_secret="whsec_example",
    )

    import_job = caliber.workspaces.imports.create(
        project_id,
        repository=repository,
        commit_sha=commit_sha,
        bundle=bundle,
        # Reused across a retry of the *same* push, not regenerated per call --
        # see ProjectImportsAPI.create's own docstring for why.
        idempotency_key=f"push-{commit_sha}",
    )
    finished = caliber.workspaces.imports.wait(project_id, import_job.import_job_id)

    return {
        "connection_id": connection.connection_id if connection is not None else None,
        "connection_status": connection.status if connection is not None else None,
        "import_job_id": finished.import_job_id,
        "import_status": finished.status,
        "revision_id": finished.revision_id,
    }
