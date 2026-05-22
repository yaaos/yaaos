"""plugins/in_memory_workspace — tempdir-based WorkspaceProvider for POC."""

from app.plugins.in_memory_workspace.service import (
    InMemoryWorkspaceProvider,
    bootstrap,
    get_provider,
)

__all__ = ["InMemoryWorkspaceProvider", "bootstrap", "get_provider"]

# Registration runs at import time.
bootstrap()

# Side-effect import: register HTTP routes (/api/in_process/health).
from app.plugins.in_memory_workspace import web  # noqa: E402, F401
