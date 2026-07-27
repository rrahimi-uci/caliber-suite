"""Write-only containment helpers for MCP connection configuration.

MCP server rows predate CALIBER's eventual secret-reference service and may
therefore contain literal credentials in ``env``, ``headers``, or
``auth_config``.  The runtime still needs those stored values, but API and audit
surfaces must never serialize them.  This module provides the narrow transition
contract used until all MCP credentials are migrated to first-class references:

* literal leaves are returned as :data:`MCP_WRITE_ONLY_SENTINEL`;
* exact ``${ENV_VAR}`` references and ``*_env_var`` names remain visible; and
* sending the sentinel back in a PATCH means "preserve the existing leaf".

The sentinel is deliberately not accepted when no stored value exists, so it
can never become a credential accidentally.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Final

MCP_WRITE_ONLY_SENTINEL: Final[str] = "__CALIBER_WRITE_ONLY__"
MCP_SENSITIVE_CONFIG_FIELDS: Final[frozenset[str]] = frozenset({"env", "headers", "auth_config"})

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_REFERENCE_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_MISSING = object()


class InvalidWriteOnlySentinelError(ValueError):
    """Raised when a write-only sentinel has no stored value to preserve."""


def sanitize_mcp_config(value: Any, *, key: str | None = None) -> Any:
    """Return a JSON-shaped MCP config with literal leaves made write-only.

    Mapping keys and container structure are retained so clients can understand
    which fields are configured.  Only explicit reference forms are safe to
    serialize: an exact ``${VAR}`` expression, or a valid environment-variable
    name stored under a key such as ``token_env_var``.
    """

    if isinstance(value, Mapping):
        return {
            str(child_key): sanitize_mcp_config(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, list | tuple):
        return [sanitize_mcp_config(item, key=key) for item in value]
    if _is_safe_reference(key, value):
        return value
    return MCP_WRITE_ONLY_SENTINEL


def merge_write_only_update(incoming: Any, stored: Any = _MISSING) -> Any:
    """Resolve sentinel leaves in a replacement value against stored config.

    Dict/list shapes otherwise keep normal replacement semantics: omitted keys
    are removed, explicit empty containers clear a field, and literal new values
    replace their stored counterparts.
    """

    if incoming == MCP_WRITE_ONLY_SENTINEL:
        if stored is _MISSING:
            raise InvalidWriteOnlySentinelError(
                "write-only sentinel can only preserve an existing MCP secret value"
            )
        return deepcopy(stored)

    if isinstance(incoming, Mapping):
        stored_mapping = stored if isinstance(stored, Mapping) else {}
        return {
            str(key): merge_write_only_update(value, stored_mapping.get(key, _MISSING))
            for key, value in incoming.items()
        }

    if isinstance(incoming, list | tuple):
        stored_items = stored if isinstance(stored, list | tuple) else ()
        return [
            merge_write_only_update(
                item,
                stored_items[index] if index < len(stored_items) else _MISSING,
            )
            for index, item in enumerate(incoming)
        ]

    return deepcopy(incoming)


def sanitize_mcp_audit_details(value: Any) -> Any:
    """Remove literal MCP config values from new or historical audit details.

    New update/delete records are sanitized before persistence.  The history
    route also applies this walker so records written by older CALIBER versions
    cannot disclose credentials after an upgrade.
    """

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if key in MCP_SENSITIVE_CONFIG_FIELDS:
                # Update diffs store {"from": <config>, "to": <config>} under
                # the sensitive field name; snapshots store the config directly.
                if isinstance(item, Mapping) and set(item).issubset({"from", "to"}):
                    result[key] = {
                        str(side): sanitize_mcp_config(side_value)
                        for side, side_value in item.items()
                    }
                else:
                    result[key] = sanitize_mcp_config(item)
            else:
                result[key] = sanitize_mcp_audit_details(item)
        return result
    if isinstance(value, list | tuple):
        return [sanitize_mcp_audit_details(item) for item in value]
    return deepcopy(value)


def _is_safe_reference(key: str | None, value: Any) -> bool:
    if not isinstance(value, str):
        return False
    if value == MCP_WRITE_ONLY_SENTINEL:
        return False
    if _ENV_REFERENCE_RE.fullmatch(value):
        return True
    return bool(key and key.endswith("_env_var") and _ENV_NAME_RE.fullmatch(value))
