"""Framework-level `/api/health` carve-out.

`/api/health` does NOT go through the `register_routes` registry — it is owned
by `core/webserver` itself as a framework concern (alongside `/openapi.json`,
`/docs`, etc.). The one-URL-prefix-per-module rule applies to domain modules
only.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.core import database
from app.core import redis as redis_client

_VERSION = "0.0.1"

health_router = APIRouter()


class HealthResponse(BaseModel):
    status: str  # "ok" | "degraded"
    db_ok: bool
    redis_ok: bool
    version: str


@health_router.get("/api/health", response_model=HealthResponse, tags=["health"])
async def get_health() -> HealthResponse:
    db_ok = await database.ping()
    redis_ok = await redis_client.ping()
    return HealthResponse(
        status="ok" if (db_ok and redis_ok) else "degraded",
        db_ok=db_ok,
        redis_ok=redis_ok,
        version=_VERSION,
    )
