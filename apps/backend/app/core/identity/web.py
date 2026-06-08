"""HTTP wiring for `core/identity`.

The periodic cleanup is registered as a `@scheduled` task in
`scheduler.py` — imported here for its decorator side effect (`@scheduled`
binds the broker task body + scheduler registry entry at import time).
"""

from __future__ import annotations

from fastapi import APIRouter

# Side-effect import: registers the hourly `identity_purge` @scheduled task
# with the broker + scheduler registry.
from app.core.identity import scheduler as _scheduler  # noqa: F401
from app.core.webserver import RouteSpec, register_routes

router = APIRouter()


register_routes(RouteSpec(module_name="identity", router=router))
