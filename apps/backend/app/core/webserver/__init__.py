"""core/webserver — FastAPI app factory, RouteSpec registry, SPA serving."""

from app.core.webserver.app_factory import create_app, mount_specs
from app.core.webserver.registry import RouteSpec, get_specs, register_routes
from app.core.webserver.service import (
    ShutdownHook,
    iter_web_shutdown_hooks,
    iter_worker_shutdown_hooks,
    register_web_shutdown_hook,
    register_worker_shutdown_hook,
)

__all__ = [
    "RouteSpec",
    "ShutdownHook",
    "create_app",
    "get_specs",
    "iter_web_shutdown_hooks",
    "iter_worker_shutdown_hooks",
    "mount_specs",
    "register_routes",
    "register_web_shutdown_hook",
    "register_worker_shutdown_hook",
]
