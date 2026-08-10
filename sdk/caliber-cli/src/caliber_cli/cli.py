"""``caliberctl`` — the argument parser and the single place errors become exits.

Named ``cli`` rather than ``main`` because the package re-exports a *function*
called ``main``, and ``from caliber_cli.main import main`` in ``__init__`` rebinds
``caliber_cli.main`` from the submodule to the function. Anything then doing
``import caliber_cli.main`` gets the function and fails on attribute access -- a
confusing failure that only appears once the package has been imported.

Two properties this file exists to guarantee.

**Non-interactive.** Nothing here prompts. A command that would do something
irreversible requires ``--yes`` and fails without it, rather than asking — a tool
that blocks on a TTY hangs a CI job instead of failing it, which is much harder
to diagnose.

**One error boundary.** Every command runs inside :func:`_dispatch`, so an API
failure, a transport failure, and an unexpected exception each become a stated
message and a meaningful exit code exactly once. Commands do not catch API
errors themselves; scattering that would make the exit-code contract impossible
to read.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from typing import Any

from caliber_sdk import CaliberClient
from caliber_sdk.errors import CaliberAPIError, CaliberConfigError, CaliberTransportError

from caliber_cli import commands, exits
from caliber_cli.output import Printer

Handler = Callable[[CaliberClient, argparse.Namespace, Printer], int]

__version__ = "0.1.0.dev0"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="caliberctl",
        description=(
            "Non-interactive operator commands for CALIBER. Every command "
            "supports --json; in JSON mode stdout carries only JSON."
        ),
        epilog=(
            "Exit codes: 0 ok, 1 failure, 2 usage, 3 awaiting a human decision, "
            "4 gate said no, 5 timed out, 6 no usable credential."
        ),
    )
    parser.add_argument("--version", action="version", version=f"caliberctl {__version__}")

    # Connection options are global rather than per-command: they describe where
    # you are pointing the tool, not what you are asking it to do.
    parser.add_argument(
        "--base-url",
        help="deployment URL; defaults to $CALIBER_BASE_URL",
    )
    parser.add_argument(
        "--token",
        help=(
            "personal access token; defaults to $CALIBER_TOKEN. Prefer the "
            "environment variable — an argument is visible in the process list."
        ),
    )
    parser.add_argument("--project", help="active project; defaults to $CALIBER_PROJECT")
    parser.add_argument(
        "--json", action="store_true", dest="as_json", help="emit JSON on stdout and nothing else"
    )
    parser.add_argument("--quiet", action="store_true", help="suppress progress notes on stderr")

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    _add(subparsers, "whoami", commands.whoami, "show the identity behind the current credential")
    _add(subparsers, "capabilities", commands.capabilities, "show what this deployment supports")

    # -- token ---------------------------------------------------------------
    token = _group(subparsers, "token", "issue, list, rotate, and revoke access tokens")
    _add(token, "list", commands.token_list, "list tokens (never their secrets)")

    create = _add(token, "create", commands.token_create, "issue a token; the secret is shown once")
    create.add_argument("name", help="human label, e.g. 'ci' or 'nightly-eval'")
    create.add_argument(
        "--scope",
        action="append",
        help=(
            "scope to grant, repeatable. Omit to inherit your own scopes. Scopes "
            "are a ceiling: the token can never exceed what you hold now."
        ),
    )

    rotate = _add(token, "rotate", commands.token_rotate, "replace a token and revoke the old one")
    rotate.add_argument("token_id")

    revoke = _add(token, "revoke", commands.token_revoke, "revoke a token (irreversible)")
    revoke.add_argument("token_id")
    revoke.add_argument("--yes", action="store_true", help="confirm the irreversible action")

    # -- workflow ------------------------------------------------------------
    workflow = _group(subparsers, "workflow", "list and run workflows")
    listing = _add(workflow, "list", commands.workflow_list, "list workflows")
    listing.add_argument("--status")

    run = _add(workflow, "run", commands.workflow_run, "submit a run and wait for it")
    target = run.add_mutually_exclusive_group(required=True)
    target.add_argument("--workflow-id", help="run the deployment alias of this workflow")
    target.add_argument("--version-id", help="run this exact version")
    run.add_argument("--alias", help="deployment alias to run; requires --workflow-id")
    run.add_argument("--input", help="run input as JSON, or a bare string")
    run.add_argument(
        "--idempotency-key",
        help=(
            "pass through to make submission safely retryable. Not generated for "
            "you: a key this tool invented would differ on your retry."
        ),
    )
    run.add_argument("--no-wait", action="store_true", help="submit and exit without waiting")
    run.add_argument("--timeout", type=float, default=900.0, help="seconds to wait (default 900)")

    status = _add(workflow, "status", commands.workflow_status, "show one run")
    status.add_argument("run_id")

    # -- job -----------------------------------------------------------------
    job = _group(subparsers, "job", "inspect and wait on background jobs")
    job_listing = _add(job, "list", commands.job_list, "list jobs")
    job_listing.add_argument("--status")

    wait = _add(job, "wait", commands.job_wait, "wait until a job settles or needs a person")
    wait.add_argument("job_id")
    wait.add_argument("--timeout", type=float, default=900.0, help="seconds to wait (default 900)")

    # -- release -------------------------------------------------------------
    release = _group(subparsers, "release", "release candidates and signoff")
    _add(release, "list", commands.release_list, "list release candidates")

    sign = _add(release, "sign", commands.release_sign, "record a go / no-go decision")
    sign.add_argument("candidate_id")
    sign.add_argument("--decision", choices=["go", "no-go"], required=True)
    sign.add_argument(
        "--rationale",
        required=True,
        help="why. Required by the API: a signoff without a reason is not evidence.",
    )

    # -- cookbook ------------------------------------------------------------
    cookbook = _group(subparsers, "cookbook", "browse and install example workflows")
    _add(cookbook, "list", commands.cookbook_list, "list recipes and their readiness")

    install = _add(cookbook, "install", commands.cookbook_install, "install a recipe")
    install.add_argument("recipe_id")
    install.add_argument("--name", help="name for the installed workflow")
    install.add_argument(
        "--force", action="store_true", help="install despite unmet readiness checks"
    )

    # -- prompt --------------------------------------------------------------
    prompt = _group(subparsers, "prompt", "inspect governed prompts")
    _add(prompt, "list", commands.prompt_list, "list prompts and their live versions")
    show = _add(prompt, "show", commands.prompt_show, "show one prompt")
    show.add_argument("name", help="prompt name or agent id")

    # -- service -------------------------------------------------------------
    service = _group(subparsers, "service", "publish a workflow as an HTTP service")
    service_show = _add(service, "show", commands.service_show, "show a workflow's service")
    service_show.add_argument("workflow_id")

    publish = _add(service, "publish", commands.service_publish, "publish a workflow's service")
    publish.add_argument("workflow_id")
    publish.add_argument("--yes", action="store_true", help="confirm exposing an external endpoint")

    unpublish = _add(
        service, "unpublish", commands.service_unpublish, "withdraw a workflow's service"
    )
    unpublish.add_argument("workflow_id")
    unpublish.add_argument("--yes", action="store_true", help="confirm breaking its callers")

    # -- plugin --------------------------------------------------------------
    plugin = _group(subparsers, "plugin", "optimizers and third-party plugins")
    _add(plugin, "list", commands.plugin_list, "list optimizers and installed plugins")

    return parser


def _group(subparsers: Any, name: str, help_text: str) -> Any:
    """A command with sub-commands, whose bare form prints its own help.

    Without the ``_needs_subcommand`` handler, ``caliberctl token`` would exit 0
    having done nothing, which reads as success.
    """
    parser = subparsers.add_parser(name, help=help_text, description=help_text)
    nested = parser.add_subparsers(dest=f"{name}_command", metavar="<subcommand>")
    parser.set_defaults(handler=_needs_subcommand, parser=parser)
    return nested


def _add(subparsers: Any, name: str, handler: Handler, help_text: str) -> Any:
    parser = subparsers.add_parser(name, help=help_text, description=help_text)
    parser.set_defaults(handler=handler, parser=parser)
    return parser


def _needs_subcommand(
    client: CaliberClient, args: argparse.Namespace, out: Printer
) -> int:  # pragma: no cover - exercised through main()
    raise AssertionError("handled before a client is built")


def main(argv: Sequence[str] | None = None) -> int:
    """Parse, connect, dispatch, and translate every failure into an exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    handler: Handler | None = getattr(args, "handler", None)
    if handler is None or handler is _needs_subcommand:
        # A bare ``caliberctl`` or ``caliberctl token``: print that parser's help
        # and exit USAGE. Help on stdout, because the user asked for it by
        # invoking an incomplete command, and exit non-zero so a script does not
        # read it as work performed.
        (getattr(args, "parser", None) or parser).print_help()
        return exits.USAGE

    out = Printer(as_json=args.as_json, quiet=args.quiet)
    return _dispatch(handler, args, out)


def _dispatch(handler: Handler, args: argparse.Namespace, out: Printer) -> int:
    """The single error boundary.

    ``CaliberConfigError`` is separated from the rest because it means the tool
    was never able to try — no base URL, no credential — and the message should
    not look like the deployment refused something.
    """
    try:
        client = CaliberClient(
            base_url=args.base_url,
            token=args.token,
            project=args.project,
        )
    except CaliberConfigError as error:
        out.error(str(error))
        out.error("set CALIBER_BASE_URL and CALIBER_TOKEN, or pass --base-url and --token")
        return exits.USAGE

    try:
        with client:
            return handler(client, args, out)
    except CaliberAPIError as error:
        out.error(str(error))
        return commands.api_error_exit(error)
    except CaliberTransportError as error:
        # No response at all: DNS, connection refused, a timeout. Named
        # separately because there is no server verdict to report and the cause
        # is usually the URL or the network rather than the request.
        out.error(str(error))
        return exits.FAILURE
    except KeyboardInterrupt:
        out.error("interrupted")
        return exits.FAILURE


def run() -> None:  # pragma: no cover - console-script shim
    sys.exit(main())


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = ["build_parser", "main"]
