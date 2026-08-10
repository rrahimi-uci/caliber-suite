"""Experimental contracts for writing CALIBER extensions.

**This contract is experimental.** It will change until it has survived a
release, and CALIBER marks every plugin-provided optimizer ``experimental``
regardless of what the plugin says about itself. Pin an exact version.

A plugin is three things:

1. A class with an ``optimize`` method matching
   :class:`~caliber_plugin_sdk.contracts.Optimizer`.
2. A :class:`~caliber_plugin_sdk.declaration.PluginDeclaration` saying what it
   is called and what it can target.
3. An entry point in the ``caliber.optimizers`` group pointing at that
   declaration.

Then the deployment must name your **distribution** in
``CALIBER_PLUGIN_ALLOWLIST``. Installing the wheel is not enough, on purpose:
an optimizer writes the artifact CALIBER promotes to production, so enabling one
is a decision an operator makes rather than a consequence of dependency
resolution.

Check your plugin against the contract before shipping it:

.. code-block:: python

    from caliber_plugin_sdk.conformance import check_declaration

    for problem in check_declaration(my_declaration):
        print(problem)
"""

from __future__ import annotations

from caliber_plugin_sdk.contracts import (
    Diagnosis,
    OptimizationRequest,
    OptimizationResult,
    Optimizer,
    OptimizerUnavailable,
)
from caliber_plugin_sdk.declaration import (
    ALLOWLIST_ENV_VAR,
    ENTRY_POINT_GROUP,
    VALID_ARTIFACT_TYPES,
    DeclarationError,
    PluginDeclaration,
    declare,
)

__version__ = "0.1.0.dev0"

__all__ = [
    "ALLOWLIST_ENV_VAR",
    "ENTRY_POINT_GROUP",
    "VALID_ARTIFACT_TYPES",
    "DeclarationError",
    "Diagnosis",
    "OptimizationRequest",
    "OptimizationResult",
    "Optimizer",
    "OptimizerUnavailable",
    "PluginDeclaration",
    "__version__",
    "declare",
]
