"""Secret-source resolver.

Every config field that names a secret (``webhook_signing_secret_env``,
``csrf_signing_secret_env``, ``llm_api_key_env``) names a *source*, not
a value. The source flows through :func:`resolve_secret` which knows
how to read from each backend.

Backwards compatible: a bare string with no scheme (e.g.
``CALIBER_WEBHOOK_SIGNING_SECRET``) is still treated as an env var
name — that's the original behavior. Operators who want to source a
secret from somewhere else opt in by writing an explicit URI:

* ``env://VAR_NAME`` — read from the named env variable. Equivalent to
  the bare-string form, but explicit.
* ``file:///abs/path`` — read the file's contents (whitespace
  stripped). Common pattern for secrets mounted into containers as
  files (Kubernetes ``Secret`` volume mount, Docker Compose
  ``secrets:`` block, Vault Agent template, etc.).

Two backends ship today. The interface is designed so future
``vault://``, ``awssm://``, ``gcpsm://`` schemes plug in without
disrupting callers — they swap a branch in :func:`_dispatch_scheme`
and add a backend function, that's it.

Design notes
------------

* The resolver returns ``Optional[str]`` so callers can distinguish
  "secret missing" from "secret resolved to empty string." Builders
  for opt-in features (webhooks, CSRF) treat ``None`` as "disabled"
  and log a clear warning.
* The actual secret value is *never* logged. Log lines reference the
  *source* (``env://X``, ``file:///path``) so an audit trail is
  available without leaking the value.
* No retries / no caching. The resolver is called once per builder at
  app startup; if the source is transiently unavailable, the operator
  restarts. A future enhancement could add a cache for backends with
  rate limits (Vault), but adding it preemptively would just be
  surface area to mis-configure.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final

logger = logging.getLogger("caliber.secrets")

_ENV_SCHEME: Final[str] = "env://"
_FILE_SCHEME: Final[str] = "file://"


def resolve_secret(
    source: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve a secret-source string to its value.

    Parameters
    ----------
    source:
        Either a URI (``env://VAR``, ``file:///path``) or a bare string
        treated as an env var name. Empty source returns ``None``.
    environ:
        Optional environment mapping for ``env://`` resolution. Defaults
        to :data:`os.environ`. Parameterized for tests.

    Returns
    -------
    str | None
        The resolved value, or ``None`` if the source couldn't be
        resolved (env var missing, file missing). ``None`` is distinct
        from empty string — see module docstring for rationale.
    """
    if not source:
        return None
    env = environ if environ is not None else os.environ

    if source.startswith(_ENV_SCHEME):
        return _resolve_env(source.removeprefix(_ENV_SCHEME), env, source_label=source)
    if source.startswith(_FILE_SCHEME):
        return _resolve_file(source.removeprefix(_FILE_SCHEME), source_label=source)

    # Bare string — backwards-compatible env-var-name interpretation.
    # The original ``*_signing_secret_env`` fields took an env var name
    # by convention; preserving that means existing deployments don't
    # have to change anything on upgrade.
    return _resolve_env(source, env, source_label=f"env://{source}")


def _resolve_env(
    var_name: str,
    environ: Mapping[str, str],
    *,
    source_label: str,
) -> str | None:
    """Read ``var_name`` from ``environ``. Returns ``None`` if unset or empty."""
    if not var_name:
        logger.warning("secret source %r has empty env var name", source_label)
        return None
    value = environ.get(var_name)
    if not value:
        # Logged at DEBUG so it doesn't fire on every healthy
        # disabled-feature build_*. The builder logs at WARNING when
        # the feature is enabled but the source is empty — that's the
        # actionable case.
        logger.debug("secret source %r resolves to empty/unset env var", source_label)
        return None
    return value


def _resolve_file(
    path_str: str,
    *,
    source_label: str,
) -> str | None:
    """Read the contents of ``path_str``.

    Whitespace is stripped so a trailing newline (which ``echo "..." >
    file`` puts there by default) doesn't end up in the secret. If the
    file is missing or unreadable, log at WARNING and return ``None``
    — the feature gates itself off the same way it does for missing
    env vars.
    """
    if not path_str:
        logger.warning("secret source %r has empty file path", source_label)
        return None
    path = Path(path_str)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("secret source %r: file does not exist", source_label)
        return None
    except OSError as exc:
        # Permission denied, IO error, etc. Treat the same as missing
        # — the feature self-disables and the operator fixes the
        # permission.
        logger.warning("secret source %r: cannot read file (%s)", source_label, exc)
        return None
    stripped = raw.strip()
    if not stripped:
        logger.warning("secret source %r: file is empty", source_label)
        return None
    return stripped
