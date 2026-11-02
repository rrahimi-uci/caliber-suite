"""Host paths in the reference profile, for code that runs where the workflow runtime does not (B12.4c, #770).

``caliber.workflows.runtime.confine_to_file_root`` confines the unmanaged host-filesystem *nodes* (``file_input``,
``folder_input``, ``output_folder``) in the process that runs the workflow. A shipped tool that opens a host path is a
different caller: it runs in the tool-sandbox child, a separate interpreter with an empty environment, which knows neither the
profile nor the root. ``LocalSubprocessToolSandbox`` therefore hands a reference-profile child exactly two variables,
``CALIBER_PROFILE`` and ``CALIBER_WORKFLOW_FILE_ROOT``, and a tool that reads a host path calls
:func:`confine_in_reference_profile` before it opens anything. Outside the reference profile it returns the path unchanged
(the development profile is untouched).

The module imports only the standard library and ``caliber.config`` (not the workflow runtime), because the child imports it.
"""

from __future__ import annotations

from pathlib import Path

from caliber.effects.refusals import (
    UNSUPPORTED_IN_REFERENCE_PROFILE,
    profile_from_environment,
    reference_profile_active,
)

#: The environment a reference-profile sandbox child is given (and the only part of the parent's it sees).
CHILD_ENVIRONMENT_VARIABLES = ("CALIBER_PROFILE", "CALIBER_WORKFLOW_FILE_ROOT")


class PathOutsideRoot(ValueError):  # noqa: N818 - the name the reference-profile contract fixes (#763)
    """A host path resolved outside ``CALIBER_WORKFLOW_FILE_ROOT`` (B12.4b, #763).

    A ``ValueError`` so every caller that already handles the refusal keeps working; ``path`` is the resolved path and
    ``root`` the configured root.
    """

    def __init__(self, message: str, *, path: Path, root: Path) -> None:
        super().__init__(message)
        self.path = path
        self.root = root


def confine_in_reference_profile(path: Path, *, what: str) -> Path:
    """The resolved ``path`` when it is inside the configured root; otherwise a refusal. A no-op outside the reference profile.

    The path is resolved first (``..`` and symlinks included) and compared by path components, so a sibling directory that
    merely starts with the root's name (``root-evil`` next to ``root``) is outside. With no root configured the read is
    refused, and ``unconfined`` is not a state of the reference profile (it is refused with the profile's one code).
    """
    if not reference_profile_active(profile_from_environment()):
        return path
    from caliber.config import (  # noqa: PLC0415 - config is imported only when the profile asks for confinement
        workflow_file_root,
        workflow_host_paths_allowed,
    )

    if not workflow_host_paths_allowed():
        raise ValueError(
            f"{what} path is refused: unmanaged host-filesystem reads are disabled because CALIBER_WORKFLOW_FILE_ROOT is "
            "unset. Bind a managed project file, or set CALIBER_WORKFLOW_FILE_ROOT to a directory these reads may use"
        )
    root = workflow_file_root()
    if root is None:
        raise ValueError(
            f"{UNSUPPORTED_IN_REFERENCE_PROFILE}: {what} path is refused: CALIBER_WORKFLOW_FILE_ROOT is 'unconfined', which "
            "the reference profile does not support. Set CALIBER_WORKFLOW_FILE_ROOT to a directory these reads may use, or "
            "bind a managed project file"
        )
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise PathOutsideRoot(
            f"{what} path {str(resolved)!r} is outside the configured workflow file root {str(root)!r}. Bind a managed "
            "project file, or widen CALIBER_WORKFLOW_FILE_ROOT.",
            path=resolved,
            root=root,
        )
    return resolved


__all__ = ["CHILD_ENVIRONMENT_VARIABLES", "PathOutsideRoot", "confine_in_reference_profile"]
