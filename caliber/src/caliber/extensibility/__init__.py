"""Extension points third-party code can register against.

CALIBER's refinement loop chooses an optimizer, runs it, and promotes what it
produces. That makes an optimizer a piece of code with authority over
production artifacts, so this package treats third-party registration as a
supply-chain surface rather than a convenience: a plugin is discovered
automatically but **never enabled** automatically.

See :mod:`caliber.extensibility.registry` for the contract and
:mod:`caliber.extensibility.entrypoints` for how discovery is gated.
"""

from __future__ import annotations

from caliber.extensibility.registry import (
    OptimizerRegistry,
    OptimizerSpec,
    PluginError,
    optimizer_registry,
)

__all__ = [
    "OptimizerRegistry",
    "OptimizerSpec",
    "PluginError",
    "optimizer_registry",
]
