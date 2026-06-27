"""Agent long-term memory for CALIBER, backed by mem0.

See :mod:`caliber.memory.service`. Default-off and lazy-imported — the core
package never imports ``mem0`` unless memory is enabled and the ``[memory]``
extra is installed.
"""

from caliber.memory.service import (
    MemoryDependencyError,
    MemoryDisabledError,
    MemoryService,
    build_mem0_config,
)

__all__ = [
    "MemoryDependencyError",
    "MemoryDisabledError",
    "MemoryService",
    "build_mem0_config",
]
