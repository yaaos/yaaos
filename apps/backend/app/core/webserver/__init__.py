"""core/webserver — FastAPI app factory, RouteSpec registry, SPA serving."""

from app.core.webserver.app_factory import create_app
from app.core.webserver.registry import RouteSpec, register_routes

__all__ = ["RouteSpec", "create_app", "register_routes"]
