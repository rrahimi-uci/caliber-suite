"""Agent long-term memory for CALIBER, backed by mem0.

See :mod:`caliber.memory.service`. Default-off and lazy-imported — the core
package never imports ``mem0`` unless memory is enabled and the ``[memory]``
extra is installed.
"""

from caliber.memory.service import (
    PARTITION_KEYS,
    MemoryDependencyError,
    MemoryDisabledError,
    MemoryLifecycleUnavailableError,
    MemoryService,
    build_mem0_config,
    user_namespace,
)

__all__ = [
    "PARTITION_KEYS",
    "MemoryDependencyError",
    "MemoryDisabledError",
    "MemoryLifecycleUnavailableError",
    "MemoryService",
    "build_mem0_config",
    "user_namespace",
]
