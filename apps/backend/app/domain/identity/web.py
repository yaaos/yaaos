"""HTTP wiring for `domain/identity` — currently just the on-startup hook that
spawns the periodic cleanup loop.

Concrete auth endpoints (`/api/auth/*`) land in Phase 4 once the OAuth
provider plugins exist.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.primitives import spawn
from app.core.webserver import RouteSpec, register_routes
from app.domain.identity.scheduler import run_cleanup_loop

router = APIRouter()


async def _start_cleanup() -> None:
    spawn("identity.cleanup", run_cleanup_loop())


register_routes(RouteSpec(module_name="identity", router=router, on_startup=[_start_cleanup]))
