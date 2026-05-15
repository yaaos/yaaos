"""FastAPI app factory + lifespan composition.

Lifespan order (matches `plan/milestones/M01-code-review/patterns.md` § Bootstrap):
  1. Mount registered domain routers (from RouteSpec registry).
  2. Run on_startup hooks.
  3. Mount SPA static files (production only; skipped when dist is absent).
  4. yield.
  5. Run on_shutdown hooks.
  6. Dispose the DB engine.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request

from app.core import database
from app.core.config import get_settings
from app.core.observability import get_logger
from app.core.webserver.health import health_router
from app.core.webserver.registry import get_specs

log = get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()

    # 0. Ensure the schema_migrations bootstrap table exists. (Real migrations
    # are run separately via bin/migrate; this is only the tracking table.)
    await database.ensure_schema_migrations_table()

    # 1. Mount routers registered by domain modules.
    for spec in get_specs().values():
        prefix = spec.url_prefix or f"/api/{spec.module_name}"
        app.include_router(spec.router, prefix=prefix, tags=[spec.module_name])

    # The framework /api/health carve-out (NOT in the RouteSpec registry).
    app.include_router(health_router)

    # 2. Per-module startup hooks (crash-recovery, warm-up).
    for spec in get_specs().values():
        for handler in spec.on_startup:
            await handler()

    # 3. SPA static files. Production only — in dev, Vite serves the SPA on
    # a separate port and proxies /api/* to FastAPI.
    _install_spa_serving(app)

    log.info("yaaof.boot.complete", env=settings.yaaof_env, port=settings.yaaof_port)
    yield

    # 5. Per-module shutdown hooks.
    for spec in get_specs().values():
        for handler in spec.on_shutdown:
            try:
                await handler()
            except Exception:
                log.exception("on_shutdown handler raised", module=spec.module_name)

    # 6. Close DB engine.
    await database.dispose()


def _install_spa_serving(app: FastAPI) -> None:
    """Mount /assets/* + a catch-all that returns index.html for client-side routes.

    If `apps/web/dist` doesn't exist (dev workflow), this is a no-op.
    """
    spa_dist = Path(__file__).resolve().parents[4] / "web" / "dist"
    if not spa_dist.exists():
        return
    app.mount("/assets", StaticFiles(directory=spa_dist / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa_catchall(full_path: str) -> FileResponse:
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404)
        return FileResponse(spa_dist / "index.html")


def _install_middleware(app: FastAPI) -> None:
    settings = get_settings()

    # OTel auto-instrumentation (no-op if OTel isn't initialized).
    try:
        # lazy: OTel is optional in M01
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: PLC0415

        FastAPIInstrumentor.instrument_app(app)
    except Exception:
        # OTel not installed or already instrumented — non-fatal.
        pass

    # CORS.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Unhandled-exception handler — log + return JSON 500.
    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logging.getLogger("yaaof").exception("http.unhandled_exception", exc_info=exc)
        return JSONResponse(status_code=500, content={"error": "internal_server_error"})


def create_app() -> FastAPI:
    """FastAPI app factory. Called from main.py after all module imports have run."""
    app = FastAPI(title="yaaof", version="0.0.1", lifespan=_lifespan)
    _install_middleware(app)
    return app
