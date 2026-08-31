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

    Only 401 gets its own code, because only 401 has a single always-correct
    fix. A 403 is "this credential lacks the scope", which is a real failure the
    caller has to resolve rather than a missing credential.
    """
    if error.status_code == 401:
        return exits.UNAUTHENTICATED
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
