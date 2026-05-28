"""HTTP wiring for `core/identity` — the on-startup hook that spawns the
periodic cleanup loop.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.identity.scheduler import run_cleanup_loop
from app.core.observability import spawn
from app.core.webserver import RouteSpec, register_routes

router = APIRouter()


async def _start_cleanup() -> None:
    spawn("identity.cleanup", run_cleanup_loop())


register_routes(RouteSpec(module_name="identity", router=router, on_startup=[_start_cleanup]))
