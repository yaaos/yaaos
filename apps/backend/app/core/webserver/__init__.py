"""core/webserver — FastAPI app factory, RouteSpec registry, SPA serving."""

from app.core.webserver.app_factory import create_app, mount_specs
from app.core.webserver.csp import CSP_POLICY, CSPMiddleware
from app.core.webserver.registry import RouteSpec, get_specs, register_routes
from app.core.webserver.service import (
    ShutdownHook,
    iter_web_shutdown_hooks,
    iter_worker_shutdown_hooks,
    register_web_shutdown_hook,
    register_worker_shutdown_hook,
)
from app.core.webserver.testing_mount import (
    assert_no_testing_routes_in_prod,
    mount_testing_endpoints,
)

__all__ = [
    "CSP_POLICY",
    "CSPMiddleware",
    "RouteSpec",
    "ShutdownHook",
    "assert_no_testing_routes_in_prod",
    "create_app",
    "get_specs",
    "iter_web_shutdown_hooks",
    "iter_worker_shutdown_hooks",
    "mount_specs",
    "mount_testing_endpoints",
    "register_routes",
    "register_web_shutdown_hook",
    "register_worker_shutdown_hook",
]
