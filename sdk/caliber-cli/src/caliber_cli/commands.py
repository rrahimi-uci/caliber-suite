"""The operator commands. Thin by design.

Every function here maps to SDK calls and adds nothing the API does not already
mean. Where a command looks like it is deciding something, it is translating a
state the server reported into an exit code — never computing a verdict of its
own. A CLI that invented semantics would be a second, undocumented API.
"""

from __future__ import annotations

import argparse
from typing import Any

from caliber_sdk import CaliberClient
from caliber_sdk.errors import CaliberAPIError
from caliber_sdk.models import (
    WorkspaceChangeRequest,
    WorkspaceImportJob,
    WorkspaceRelease,
    WorkspaceReleaseOperationResult,
)
from caliber_sdk.waiters import WaitTimeout

from caliber_cli import exits
from caliber_cli.output import Printer


def whoami(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    """Report the identity behind the current credential.

    ``/me`` reports rather than requires, so an unusable credential comes back as
    an anonymous identity with a 200. That is right for the API and wrong for a
    CLI: a script that ran ``whoami`` to confirm its credential and got exit 0
    for "nobody" would proceed on a false premise. So an anonymous answer is
    reported honestly and exits UNAUTHENTICATED.
    """
    identity = client.me.get()
    out.data(identity)
    if identity.is_anonymous:
        out.error("no usable credential; set CALIBER_TOKEN or pass --token")
        return exits.UNAUTHENTICATED
    return exits.OK


def capabilities(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    """What this deployment supports."""
    out.data(client.capabilities_info.get())
    return exits.OK


# -- tokens ----------------------------------------------------------------


def token_list(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    out.table(
        client.auth.tokens.list(),
        columns=["token_id", "name", "scopes", "last_used_at", "expires_at"],
    )
    return exits.OK


def token_create(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    """Issue a token. The secret is shown once, and that is said out loud.

    The note goes to stderr so ``caliberctl token create ci --json | jq -r .token``
    still yields exactly the secret.
    """
    issued = client.auth.tokens.create(args.name, scopes=args.scope or None)
    out.note("this is the only time the token value is shown; store it now")
    out.data(issued)
    return exits.OK


def token_revoke(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    if not args.yes:
        out.error(f"revoking {args.token_id} is irreversible; pass --yes to confirm")
        return exits.USAGE
    revoked = client.auth.tokens.revoke(args.token_id)
    out.data({"token_id": args.token_id, "revoked": revoked})
    return exits.OK if revoked else exits.FAILURE


def token_rotate(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    """Issue a replacement and revoke the old one, in the server's single step.

    One call rather than create-then-revoke, because doing it in two would leave
    a window where a crash between them means either two live tokens or none.
    """
    issued = client.auth.tokens.rotate(args.token_id)
    out.note("the previous token is now revoked; this new value is shown once")
    out.data(issued)
    return exits.OK


# -- workflows -------------------------------------------------------------


def workflow_list(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    out.table(
        client.workflows.list(status=args.status),
        columns=["workflow_id", "name", "status", "updated_at"],
    )
    return exits.OK


def workflow_run(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    """Submit a run and, unless told otherwise, wait for it.

    Waiting is the default because the non-waiting form is only useful to
    something that will poll later, and a deploy script that forgot to wait
    reports success for work that has not happened.

    ``--idempotency-key`` is passed through rather than generated. Submission is
    the one mutating call the SDK will not retry on its own, and a key this tool
    invented would be different on the caller's retry — which is the opposite of
    what the key is for.
    """
    run = client.workflows.runs.submit(
        workflow_id=args.workflow_id,
        workflow_version_id=args.version_id,
        alias=args.alias,
        input=_maybe_json(args.input),
        idempotency_key=args.idempotency_key,
    )
    out.note(f"submitted run {run.workflow_run_id}")

    if args.no_wait:
        out.data(run)
        return exits.OK

    try:
        run = client.workflows.runs.wait(
            run.workflow_run_id, timeout=args.timeout, raise_on_failure=False
        )
    except WaitTimeout:
        out.data(client.workflows.runs.get(run.workflow_run_id))
        out.error(f"run {run.workflow_run_id} did not finish within {args.timeout}s")
        return exits.TIMEOUT

    out.data(run)
    if run.status in {"succeeded", "completed"}:
        return exits.OK
    return exits.FAILURE


def workflow_status(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    run = client.workflows.runs.get(args.run_id)
    out.data(run)
    if not run.is_terminal:
        # Still running is neither success nor failure. TIMEOUT is the code for
        # "no verdict yet", and reusing it keeps a caller's handling uniform
        # between ``status`` and a ``run`` that hit its deadline.
        return exits.TIMEOUT
    return exits.OK if run.status in {"succeeded", "completed"} else exits.FAILURE


def workflow_deployments(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    out.data(client.workflows.deployments(args.workflow_id))
    return exits.OK


def workflow_promote(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    """Point a deployment alias at a version.

    On a gated alias the server creates a pending promotion instead of
    rotating immediately -- this command reports whichever one actually
    happened rather than assuming; see ``workflow promotions`` and
    ``promotion approve``/``reject`` for the gated path.
    """
    out.data(
        client.workflows.promote_deployment(
            args.workflow_id, args.alias, version_id=args.version_id
        )
    )
    return exits.OK


def workflow_rollback(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    """Pop the deployment's checkpoint stack, restoring the prior version.

    Changes what a live alias serves right now, so it needs --yes like every
    other command here that does something to production traffic.
    """
    if not args.yes:
        out.error(
            f"rolling back {args.workflow_id}:{args.alias} changes what that alias "
            "serves right now; pass --yes to confirm"
        )
        return exits.USAGE
    out.data(client.workflows.rollback_deployment(args.workflow_id, args.alias))
    return exits.OK


def workflow_promotions(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    out.data(client.workflows.list_promotions(args.workflow_id))
    return exits.OK


# -- promotions --------------------------------------------------------------


def promotion_approve(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    out.data(client.workflows.promotions.approve(args.promotion_id, reason=args.reason))
    return exits.OK


def promotion_reject(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    out.data(client.workflows.promotions.reject(args.promotion_id, reason=args.reason))
    return exits.OK


# -- jobs ------------------------------------------------------------------


def job_list(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    out.table(
        client.jobs.list(status=args.status),
        columns=["job_id", "status", "artifact_type", "optimizer_type", "created_at"],
    )
    return exits.OK


def job_wait(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    """Wait for a refinement job, and distinguish "done" from "your turn".

    A job that reaches ``candidate_ready`` has stopped and will never advance on
    its own: applying the candidate is a human decision. Reporting that as
    success would let a pipeline continue as though someone had approved it.
    """
    try:
        job = client.jobs.wait(args.job_id, timeout=args.timeout)
    except WaitTimeout:
        out.data(client.jobs.get(args.job_id))
        out.error(f"job {args.job_id} did not settle within {args.timeout}s")
        return exits.TIMEOUT

    out.data(job)
    if job.awaits_human:
        out.note(f"job {job.job_id} is {job.status}: a person has to act")
        return exits.AWAITING_HUMAN
    if job.status in {"failed", "cancelled", "error"}:
        return exits.FAILURE
    return exits.OK


# -- releases --------------------------------------------------------------


def release_list(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    out.table(
        client.releases.candidates(),
        columns=["candidate_id", "name", "status", "score", "created_at"],
    )
    return exits.OK


def release_sign(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    """Record a go / no-go decision.

    ``--rationale`` is required by the API and therefore by this command. A
    signoff without a reason is not evidence that anyone decided anything, and a
    CLI that defaulted it to "signed via caliberctl" would manufacture exactly
    the record the requirement exists to prevent.
    """
    out.data(
        client.releases.sign(args.candidate_id, decision=args.decision, rationale=args.rationale)
    )
    return exits.OK if args.decision == "go" else exits.GATE_FAILED


# -- gate verdicts -----------------------------------------------------------


def gate_verdict_show(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    """Show the latest verdict for one version, or ``{"state": "none"}``.

    A plain read: unlike ``record``, showing a verdict does not itself produce
    a decision, so it always exits OK and leaves interpreting ``state`` to the
    caller.
    """
    out.data(client.gate_verdicts.get(args.artifact_type, args.version_key))
    return exits.OK


def gate_verdict_record(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    """Record a verdict.

    Advisory in v1 -- CALIBER never blocks alias rotation on a verdict by
    itself (see ``ARCHITECTURE.md`` Sec 4's "Gate semantics" column) -- but
    a script recording one still needs an exit code carrying the answer,
    the same way ``release sign`` does for a signoff.
    """
    verdict = client.gate_verdicts.record(args.artifact_type, args.version_key, state=args.state)
    out.data(verdict)
    return exits.OK if args.state != "fail" else exits.GATE_FAILED


# -- cookbooks -------------------------------------------------------------


def cookbook_list(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    recipes = client.cookbooks.list()
    if out.as_json:
        out.data(recipes)
        return exits.OK

    out.table(recipes, columns=["id", "title", "readiness_status"])
    # The unmet checks, named rather than summarised: "configuration_required"
    # with no cause leaves the operator to go find it.
    for recipe in recipes:
        for check in recipe.unmet_checks:
            out.note(f"{recipe.id}: {check.get('label')} — {check.get('status')}")
    return exits.OK


def cookbook_install(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    """Install a recipe, refusing one whose prerequisites are unmet.

    Readiness is checked here rather than left to the server's rejection so the
    failure names every unmet check at once, instead of one 400 per attempt.
    ``--force`` exists because readiness is computed from the live environment
    and an operator may know something it does not.
    """
    recipes = {recipe.id: recipe for recipe in client.cookbooks.list()}
    recipe = recipes.get(args.recipe_id)
    if recipe is None:
        out.error(f"no cookbook recipe {args.recipe_id!r}; available: {sorted(recipes)}")
        return exits.USAGE

    if not recipe.is_ready and not args.force:
        for check in recipe.unmet_checks:
            out.error(f"{check.get('label')}: {check.get('status')}")
        out.error(f"{recipe.id} is not ready to install; fix the above or pass --force")
        return exits.FAILURE

    result = client.cookbooks.install(recipe.id, name=args.name)
    out.data(result)
    out.note("installed paused; review its bindings before running it")
    return exits.OK


# -- prompts ---------------------------------------------------------------


def prompt_list(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    """List governed prompts and the registry coordinate each one resolves to.

    The columns are the registry coordinate, not a CALIBER row id: prompts live
    in MLflow's registry, versions are immutable, and an alias points at one of
    them -- so "which version is live" is the question an operator has.
    """
    out.table(
        client.prompts.list(),
        columns=["prompt_name", "agent_id", "version", "alias", "source"],
    )
    return exits.OK


def prompt_show(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    out.data(client.prompts.get(args.name))
    return exits.OK


# -- services --------------------------------------------------------------


def service_show(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    """Show a workflow's service configuration.

    Scoped to a workflow because that is how the server models it: management
    lives under ``/workflows/{id}/service``, and there is no unscoped service
    listing. An earlier SDK method that invented one returned 404.
    """
    out.data(client.workflows.services.get(args.workflow_id))
    return exits.OK


def service_publish(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    """Publish a workflow as an external HTTP service.

    Publishing widens the workflow's reachable surface: invocation moves to
    ``/services/{id}`` authenticated by per-service tokens rather than a user
    credential. That is not something to do by accident, so it needs --yes.
    """
    if not args.yes:
        out.error(
            f"publishing {args.workflow_id} exposes it as an external HTTP service; "
            "pass --yes to confirm"
        )
        return exits.USAGE
    out.data(client.workflows.services.publish(args.workflow_id))
    return exits.OK


def service_unpublish(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    if not args.yes:
        out.error(
            f"unpublishing {args.workflow_id} will break callers of its service "
            "endpoint; pass --yes to confirm"
        )
        return exits.USAGE
    unpublished = client.workflows.services.unpublish(args.workflow_id)
    out.data({"workflow_id": args.workflow_id, "unpublished": unpublished})
    return exits.OK if unpublished else exits.FAILURE


# -- plugins ---------------------------------------------------------------


def plugin_list(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    """Optimizers this deployment can run, and plugins it merely has installed.

    Exits FAILURE when a plugin was allowlisted and then failed to load: the
    deployment asked for it, so a green exit would report a configuration that
    is not in effect.
    """
    extensibility = client.capabilities_info.get().extensibility

    out.table(
        extensibility.optimizers,
        columns=["name", "source", "artifact_types", "requires", "experimental"],
    )

    broken = [plugin for plugin in extensibility.plugins if plugin.error]
    inert = [
        plugin for plugin in extensibility.plugins if not plugin.allowlisted and not plugin.error
    ]

    for plugin in inert:
        out.note(
            f"{plugin.distribution or plugin.name} is installed but not enabled; "
            f"add it to {extensibility.allowlist_env_var}"
        )
    for plugin in broken:
        out.error(f"{plugin.distribution or plugin.name} failed to load: {plugin.error}")

    return exits.FAILURE if broken else exits.OK


# -- workspace: imports -----------------------------------------------------


def workspace_import_create(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    """Start a bounded, digest-pinned source import and wait for it to land.

    "Bounded" means the caller pins the exact commit and supplies the bundle
    content the server imports against it, rather than the server resolving a
    branch tip that could move between request and response. ``--idempotency-
    key`` has no default for the same reason ``workflow run``'s does not: a
    key this tool invented would differ on the caller's own retry.
    """
    try:
        with open(args.bundle, "rb") as bundle_handle:
            job = client.workspaces.imports.create(
                args.project,
                repository=args.repository,
                commit_sha=args.commit_sha,
                bundle=bundle_handle,
                idempotency_key=args.idempotency_key,
                filename=args.filename,
            )
    except OSError as error:
        out.error(f"cannot read bundle {args.bundle!r}: {error.strerror or error}")
        return exits.USAGE
    out.note(f"started import {job.import_job_id}")

    if args.no_wait:
        out.data(job)
        return exits.OK

    try:
        job = client.workspaces.imports.wait(args.project, job.import_job_id, timeout=args.timeout)
    except WaitTimeout:
        out.data(client.workspaces.imports.get(args.project, job.import_job_id))
        out.error(f"import {job.import_job_id} did not finish within {args.timeout}s")
        return exits.TIMEOUT

    out.data(job)
    return _import_job_exit(job)


def workspace_import_status(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    job = client.workspaces.imports.get(args.project, args.job_id)
    out.data(job)
    if not job.is_terminal:
        return exits.TIMEOUT
    return _import_job_exit(job)


def workspace_import_list(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    page = client.workspaces.imports.list(args.project, status=args.status)
    out.table(
        page.items,
        columns=["import_job_id", "status", "repository", "commit_sha", "revision_id"],
    )
    if page.next_cursor and not out.as_json:
        out.note(f"more results: --cursor {page.next_cursor}")
    return exits.OK


def workspace_import_reconcile(
    client: CaliberClient, args: argparse.Namespace, out: Printer
) -> int:
    """Explicitly observe an import stuck in ``reconcile_required``.

    Not a retry of the import: it re-checks what actually landed against what
    the job expected and reports the observation, rather than forcing a
    verdict the server cannot yet support.
    """
    out.data(client.workspaces.imports.reconcile(args.project, args.job_id))
    return exits.OK


def _import_job_exit(job: WorkspaceImportJob) -> int:
    if job.status == "reconcile_required":
        return exits.RECONCILE_REQUIRED
    return exits.OK if job.status == "succeeded" else exits.FAILURE


# -- workspace: packages (revisions) -----------------------------------------


def workspace_package_list(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    page = client.workspaces.revisions.list(args.project, status=args.status)
    out.table(page.items, columns=["revision_id", "revision_number", "status", "revision_sha256"])
    if page.next_cursor and not out.as_json:
        out.note(f"more results: --cursor {page.next_cursor}")
    return exits.OK


def workspace_package_show(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    out.data(client.workspaces.revisions.get(args.project, args.revision_id))
    return exits.OK


def workspace_package_diff(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    out.data(client.workspaces.revisions.diff(args.project, args.revision_id, base=args.base))
    return exits.OK


# -- workspace: Change Requests ----------------------------------------------


def workspace_cr_list(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    page = client.workspaces.change_requests.list(
        args.project,
        status=args.status,
        created_by=args.created_by,
        reviewer_user_id=args.reviewer,
    )
    out.table(
        page.items,
        columns=["change_request_id", "title", "status", "current_head_revision_id"],
    )
    if page.next_cursor and not out.as_json:
        out.note(f"more results: --cursor {page.next_cursor}")
    return exits.OK


def workspace_cr_show(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    change_request = client.workspaces.change_requests.get(args.project, args.change_request_id)
    out.data(change_request)
    return _change_request_exit(change_request)


def workspace_cr_create(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    """Open a draft Change Request over a ready revision.

    Stays ``draft`` until ``workspace cr submit`` -- creating one does not by
    itself start review or claim ``--semantic-version``.
    """
    change_request = client.workspaces.change_requests.create(
        args.project,
        title=args.title,
        head_revision_id=args.head_revision_id,
        semantic_version=args.semantic_version,
        description=args.description or "",
        base_revision_id=args.base_revision_id,
        reviewer_user_ids=args.reviewer or None,
    )
    out.data(change_request)
    return _change_request_exit(change_request)


def workspace_cr_submit(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    change_request = client.workspaces.change_requests.submit(
        args.project, args.change_request_id, idempotency_key=args.idempotency_key
    )
    out.data(change_request)
    return _change_request_exit(change_request)


def workspace_cr_update(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    """Append a new ready revision as the request's next head generation.

    This is the update path for a Change Request the way ``git push`` updates
    a pull request: it appends a head generation, it never rewrites one.
    """
    change_request = client.workspaces.change_requests.update_head(
        args.project,
        args.change_request_id,
        revision_id=args.revision_id,
        expected_lock_version=args.expected_lock_version,
        change_summary=args.change_summary or "",
    )
    out.data(change_request)
    return _change_request_exit(change_request)


def workspace_cr_rebase(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    """Bring an ``out_of_date`` request back onto the current accepted head."""
    change_request = client.workspaces.change_requests.rebase(
        args.project,
        args.change_request_id,
        revision_id=args.revision_id,
        expected_lock_version=args.expected_lock_version,
        change_summary=args.change_summary or "",
        semantic_version=args.semantic_version,
    )
    out.data(change_request)
    return _change_request_exit(change_request)


def workspace_cr_review(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    """Record one reviewer's decision against a specific head.

    Exits on the decision just recorded, not a re-fetched aggregate request
    state -- the same choice ``gate_verdict_record`` makes for its own verdict.
    """
    review = client.workspaces.change_requests.submit_review(
        args.project,
        args.change_request_id,
        head_id=args.head_id,
        decision=args.decision,
        rationale=args.rationale or "",
    )
    out.data(review)
    return exits.CHANGES_REQUESTED if args.decision == "request_changes" else exits.OK


def workspace_cr_close(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    change_request = client.workspaces.change_requests.close(
        args.project,
        args.change_request_id,
        reason=args.reason,
        expected_lock_version=args.expected_lock_version,
    )
    out.data(change_request)
    return _change_request_exit(change_request)


def workspace_cr_accept(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    """Accept a Change Request, advancing the project's accepted revision.

    Takes no QA-evidence argument -- the server derives it itself from a
    durably recorded QA ``go`` decision bound to the request's current head
    (see ``ProjectChangeRequestsAPI.accept``'s own docstring on the SDK
    side and ``caliber.routes.workspace_change_requests`` on the server
    side); this command never has evidence of its own to send. A ``409``
    with ``qa_go_decision_required`` means QA has not recorded a passing
    decision for this head yet -- ``api_error_exit`` maps that to
    ``AWAITING_HUMAN`` rather than ``FAILURE`` so a caller can script
    around "not ready yet" distinctly from a hard error.
    """
    change_request = client.workspaces.change_requests.accept(args.project, args.change_request_id)
    out.data(change_request)
    return _change_request_exit(change_request)


#: Statuses in which a Change Request is simply waiting on somebody's decision
#: -- not rejected, not stale, not accepted. Maps onto AWAITING_HUMAN because
#: that is exactly what it means: the command worked, and a person has to act.
_CHANGE_REQUEST_PENDING_STATES = frozenset(
    {"draft", "open", "technically_approved", "qa_in_progress"}
)


def _change_request_exit(change_request: WorkspaceChangeRequest) -> int:
    status = change_request.status
    if status == "accepted":
        return exits.OK
    if status == "changes_requested":
        return exits.CHANGES_REQUESTED
    if status == "out_of_date":
        return exits.OUT_OF_DATE
    if status in _CHANGE_REQUEST_PENDING_STATES:
        return exits.AWAITING_HUMAN
    return exits.FAILURE


# -- workspace: releases ------------------------------------------------------


def workspace_release_list(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    releases = client.workspaces.releases.list(
        args.project, status=args.status, environment_id=args.environment_id
    )
    out.table(releases, columns=["release_id", "environment_id", "revision_id", "status"])
    return exits.OK


def workspace_release_status(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    release = client.workspaces.releases.get(args.project, args.release_id)
    out.data(release)
    return _release_exit(release)


def workspace_release_create(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    """Capture immutable release coordinates before evaluation dispatch."""
    release = client.workspaces.releases.create(
        args.project,
        revision_id=args.revision_id,
        environment_id=args.environment_id,
        environment_config_sha256=args.environment_config_sha256,
        runtime_dependencies_sha256=args.runtime_dependencies_sha256,
        policy_sha256=args.policy_sha256,
        request_idempotency_key=args.request_idempotency_key,
        change_request_id=args.change_request_id,
        change_request_head_id=args.change_request_head_id,
        version_tag_id=args.version_tag_id,
        predecessor_release_id=args.predecessor_release_id,
    )
    out.data(release)
    return _release_exit(release)


def workspace_release_evaluate(
    client: CaliberClient, args: argparse.Namespace, out: Printer
) -> int:
    """Request an evaluation attempt and, unless told otherwise, wait for it."""
    evaluation = client.workspaces.releases.evaluate(
        args.project,
        args.release_id,
        idempotency_key=args.idempotency_key,
        evaluation_plan_sha256=args.evaluation_plan_sha256,
        input_sha256=args.input_sha256,
    )
    out.note(f"requested evaluation {evaluation.evaluation_id}")

    if args.no_wait:
        out.data(evaluation)
        return exits.OK

    try:
        evaluation = client.workspaces.releases.wait_for_evaluation(
            args.project, args.release_id, evaluation.evaluation_id, timeout=args.timeout
        )
    except WaitTimeout:
        out.data(
            client.workspaces.releases.get_evaluation(
                args.project, args.release_id, evaluation.evaluation_id
            )
        )
        out.error(f"evaluation {evaluation.evaluation_id} did not finish within {args.timeout}s")
        return exits.TIMEOUT

    out.data(evaluation)
    return exits.OK if evaluation.status == "succeeded" else exits.FAILURE


def workspace_release_quality_signoff(
    client: CaliberClient, args: argparse.Namespace, out: Printer
) -> int:
    """Record a QA go/no-go decision. Requires the Reviewer project role."""
    decision = client.workspaces.releases.quality_signoff(
        args.project,
        args.release_id,
        decision=args.decision,
        gate_evidence_sha256=args.gate_evidence_sha256,
        rationale=args.rationale or "",
        change_request_head_id=args.change_request_head_id,
    )
    out.data(decision)
    return exits.OK if args.decision == "go" else exits.GATE_FAILED


def workspace_release_approve(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    """Record the final release go/no-go decision. Requires the Owner role."""
    decision = client.workspaces.releases.approve(
        args.project,
        args.release_id,
        decision=args.decision,
        gate_evidence_sha256=args.gate_evidence_sha256,
        rationale=args.rationale or "",
        change_request_head_id=args.change_request_head_id,
    )
    out.data(decision)
    return exits.OK if args.decision == "go" else exits.GATE_FAILED


def workspace_release_versions(
    client: CaliberClient, args: argparse.Namespace, out: Printer
) -> int:
    page = client.workspaces.version_tags.list(args.project)
    out.table(page.items, columns=["tag_id", "tag", "revision_id", "kind", "created_at"])
    if page.next_cursor and not out.as_json:
        out.note(f"more results: --cursor {page.next_cursor}")
    return exits.OK


def workspace_release_apply(client: CaliberClient, args: argparse.Namespace, out: Printer) -> int:
    """Prepare, execute, and (unless told otherwise) wait for an apply.

    Three server calls, not one: preparing an operation is a distinct,
    auditable compare-and-swap against the environment's lock version, and
    executing it against the provider adapter is a second step that can fail
    independently -- collapsing them would hide which one actually failed.
    """
    prepared = client.workspaces.release_operations.create(
        args.project,
        args.release_id,
        kind="apply",
        idempotency_key=args.idempotency_key,
        expected_environment_lock_version=args.expected_environment_lock_version,
        expected_current_release_id=args.expected_current_release_id,
    )
    result = client.workspaces.release_operations.apply(
        args.project, args.release_id, prepared.operation.operation_id
    )
    return _finish_release_operation(client, args, out, result)


def workspace_release_rollback(
    client: CaliberClient, args: argparse.Namespace, out: Printer
) -> int:
    """Prepare, execute, and (unless told otherwise) wait for a rollback.

    A rollback is the same operation machinery as an apply with
    ``kind="rollback"`` and an explicit ``--target-release-id`` -- there is no
    separate rollback state machine to drift from the apply path.
    """
    prepared = client.workspaces.release_operations.create(
        args.project,
        args.release_id,
        kind="rollback",
        idempotency_key=args.idempotency_key,
        expected_environment_lock_version=args.expected_environment_lock_version,
        expected_current_release_id=args.expected_current_release_id,
        target_release_id=args.target_release_id,
    )
    result = client.workspaces.release_operations.apply(
        args.project, args.release_id, prepared.operation.operation_id
    )
    return _finish_release_operation(client, args, out, result)


def workspace_release_operation_status(
    client: CaliberClient, args: argparse.Namespace, out: Printer
) -> int:
    result = client.workspaces.release_operations.get(
        args.project, args.release_id, args.operation_id
    )
    out.data(result)
    if not result.is_terminal:
        return exits.TIMEOUT
    return _release_operation_exit(result)


def _finish_release_operation(
    client: CaliberClient,
    args: argparse.Namespace,
    out: Printer,
    result: WorkspaceReleaseOperationResult,
) -> int:
    """Report an operation :meth:`apply` already executed synchronously.

    Unlike a workflow run's submit, ``apply()``/``observe()`` are not "fire
    and forget": the provider adapter runs inline and the response already
    carries a real, possibly-terminal status. ``--no-wait`` only skips
    polling for a result that is *not yet known* -- it never overrides one
    the call already returned, which would misreport a ``failed`` or
    ``reconcile_required`` outcome as success.
    """
    if result.is_terminal:
        out.data(result)
        return _release_operation_exit(result)

    if args.no_wait:
        out.data(result)
        return exits.OK

    operation_id = result.operation.operation_id
    try:
        result = client.workspaces.release_operations.wait(
            args.project, args.release_id, operation_id, timeout=args.timeout
        )
    except WaitTimeout:
        out.data(
            client.workspaces.release_operations.get(args.project, args.release_id, operation_id)
        )
        out.error(f"operation {operation_id} did not finish within {args.timeout}s")
        return exits.TIMEOUT

    out.data(result)
    return _release_operation_exit(result)


def _release_exit(release: WorkspaceRelease) -> int:
    status = release.status
    if status == "approved":
        return exits.OK
    if status == "blocked":
        return exits.GATE_FAILED
    if status in {"draft", "evaluating", "awaiting_quality_signoff", "awaiting_approval"}:
        return exits.AWAITING_HUMAN
    return exits.FAILURE


def _release_operation_exit(result: WorkspaceReleaseOperationResult) -> int:
    status = result.operation.status
    if status == "applied":
        return exits.OK
    if status == "reconcile_required":
        return exits.RECONCILE_REQUIRED
    return exits.FAILURE


# -- shared helpers --------------------------------------------------------


def _maybe_json(raw: str | None) -> Any:
    """Parse ``--input`` as JSON, falling back to the literal string.

    A workflow input is usually an object, and quoting one on a shell command
    line is already painful enough. But some workflows take a bare string, so a
    value that is not valid JSON is passed through rather than rejected — the
    server validates it, and guessing wrong here would block a legitimate call.
    """
    if raw is None:
        return None
    import json

    try:
        return json.loads(raw)
    except ValueError:
        return raw


def api_error_exit(error: CaliberAPIError) -> int:
    """Map an API failure to an exit code.

    Only 401 gets its own code on status alone, because only 401 has a
    single always-correct fix. A 403 is "this credential lacks the scope",
    which is a real failure the caller has to resolve rather than a missing
    credential.

    One additional, detail-keyed case: ``:accept``'s ``409
    qa_go_decision_required`` (``workspace_cr_accept``) is not a hard
    failure -- the command worked and QA simply has not recorded a passing
    decision for the current head yet, the same "stopped and asked a person
    to act" situation ``AWAITING_HUMAN`` already names for a Change
    Request's own pending statuses. The detail string is specific to the
    ``:accept`` route (see ``_resolve_verified_qa_evidence`` server-side),
    so keying on it here cannot misclassify a 409 from any other command.
    """
    if error.status_code == 401:
        return exits.UNAUTHENTICATED
    if error.status_code == 409 and error.detail == "qa_go_decision_required":
        return exits.AWAITING_HUMAN
    return exits.FAILURE


__all__ = [
    "api_error_exit",
    "capabilities",
    "cookbook_install",
    "cookbook_list",
    "job_list",
    "job_wait",
    "plugin_list",
    "prompt_list",
    "prompt_show",
    "release_list",
    "release_sign",
    "service_publish",
    "service_show",
    "service_unpublish",
    "token_create",
    "token_list",
    "token_revoke",
    "token_rotate",
    "whoami",
    "workflow_list",
    "workflow_run",
    "workflow_status",
]
