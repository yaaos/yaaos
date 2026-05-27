"""Web-process shutdown registry — thin re-export of `core.shutdown_registry`.

Consumers import from `core.webserver` (the published interface) or from
`core.shutdown_registry` directly (the zero-dep module). Both routes work.
"""

from app.core.shutdown_registry import (  # noqa: F401
    ShutdownHook,
    iter_web_shutdown_hooks,
    iter_worker_shutdown_hooks,
    register_web_shutdown_hook,
    register_worker_shutdown_hook,
)
