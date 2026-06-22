"""Framework-level `/api/health` carve-out.

`/api/health` does NOT go through the `register_routes` registry — it is owned
by `core/webserver` itself as a framework concern (alongside `/openapi.json`,
`/docs`, etc.). The one-URL-prefix-per-module rule applies to domain modules
only.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import app.core.database as database
import app.core.redis as redis_client
from app.core.config import get_settings

health_router = APIRouter()


class HealthResponse(BaseModel):
    status: str  # "ok" | "degraded"
    db_ok: bool
    redis_ok: bool
    version: str


async def _db_ping() -> bool:
    return await database.ping()


async def _redis_ping() -> bool:
    return await redis_client.ping()


@health_router.get("/api/health", response_model=HealthResponse, tags=["health"])
async def get_health(
    db_ok: Annotated[bool, Depends(_db_ping)],
    redis_ok: Annotated[bool, Depends(_redis_ping)],
) -> JSONResponse:
    healthy = db_ok and redis_ok
    body = HealthResponse(
        status="ok" if healthy else "degraded",
        db_ok=db_ok,
        redis_ok=redis_ok,
        version=get_settings().service_version,
    )
    return JSONResponse(
        content=body.model_dump(),
        status_code=200 if healthy else 503,
    )
