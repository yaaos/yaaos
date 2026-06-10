"""FastAPI app factory + lifespan composition.

Lifespan order (matches `Bootstrap):
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
from app.core.shutdown_registry import iter_web_shutdown_hooks
from app.core.webserver.health import health_router
from app.core.webserver.registry import get_specs

log = get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()

    # 0. Apply pending migrations (idempotent).
    await database.migrate()

    # 1. Mount routers registered by domain modules.
    mount_specs(app)

    # The framework /api/health carve-out (NOT in the RouteSpec registry).
    app.include_router(health_router)

    # 2. Per-module startup hooks (crash-recovery, warm-up).
    for spec in get_specs().values():
        for handler in spec.on_startup:
            await handler()

    # 3. SPA static files. Production only — in dev, Vite serves the SPA on
    # a separate port and proxies /api/* to FastAPI.
    _install_spa_serving(app)

    log.info("yaaos.boot.complete", env=settings.app_mode, port=settings.yaaos_port)
    yield

    # 5. Per-module shutdown hooks.
    for spec in get_specs().values():
        for handler in spec.on_shutdown:
            try:
                await handler()
            except Exception:
                log.exception("on_shutdown handler raised", module=spec.module_name)

    # 6. Shutdown registry — each runtime-state module appended its shutdown()
    # at import time. Run in reverse-registration order (most-dependent first).
    for hook in reversed(iter_web_shutdown_hooks()):
        try:
            await hook()
        except Exception:
            log.exception(
                "web shutdown hook failed",
                hook=getattr(hook, "__qualname__", repr(hook)),
            )


class _ImmutableStaticFiles(StaticFiles):
    """StaticFiles that stamps `Cache-Control: public, max-age=1y, immutable`.

    Safe because Vite content-hashes every file under /assets/ (filenames like
    `index-a3f2b1c8.js`) — the URL changes whenever the content does, so we
    never serve stale.
    """

    async def get_response(self, path: str, scope):  # type: ignore[no-untyped-def, override]
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


# index.html and Vite-copied public/ assets (favicon, og-image, etc.) live at
# dist root and are NOT content-hashed — they need a short, revalidating TTL
# so a new deploy is picked up quickly.
_INDEX_CACHE_CONTROL = "public, max-age=60, must-revalidate"


def _install_spa_serving(app: FastAPI) -> None:
    """Mount /assets/* + a catch-all that returns index.html for client-side routes.

    If `apps/web/dist` doesn't exist (dev workflow), this is a no-op.
    """
    spa_dist = Path(__file__).resolve().parents[4] / "web" / "dist"
    if not spa_dist.exists():
        return
    app.mount("/assets", _ImmutableStaticFiles(directory=spa_dist / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa_catchall(full_path: str) -> FileResponse:
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404)
        # Vite copies `apps/web/public/` (favicon.svg, og-image.png, etc.)
        # to `dist/` root at build time. Serve those real files when they
        # exist instead of returning index.html — otherwise the browser
        # would parse HTML as an image and silently drop the favicon.
        # Restrict to a file inside dist (no traversal) and ignore paths
        # that resolve to a directory.
        if full_path:
            candidate = (spa_dist / full_path).resolve()
            try:
                candidate.relative_to(spa_dist.resolve())
            except ValueError:
                pass  # outside dist — fall through to index.html
            else:
                if candidate.is_file():
                    return FileResponse(candidate, headers={"Cache-Control": _INDEX_CACHE_CONTROL})
        return FileResponse(spa_dist / "index.html", headers={"Cache-Control": _INDEX_CACHE_CONTROL})


def _install_middleware(app: FastAPI) -> None:
    settings = get_settings()

    # OTel auto-instrumentation (no-op if OTel isn't initialized).
    try:
        # OTel is optional.
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

    # Default-deny auth middleware (see core/auth/middleware.py).
    from app.core.auth import AuthMiddleware  # noqa: PLC0415

    app.add_middleware(AuthMiddleware)

    # Slow-request log — forensic trail for intermittent hangs. Wraps every
    # request; emits one warn line per request taking >500ms.
    from app.core.observability import SlowRequestLogMiddleware  # noqa: PLC0415

    app.add_middleware(SlowRequestLogMiddleware)

    # # slowapi rate limiting. Per-IP on /api/auth/* (anonymous
    # endpoints); per-user on mutating /api/* paths. Limits live on
    # individual route decorators (`@limiter.limit(AUTH_LIMIT)`). Only
    # mounted in production so dev + Playwright suites aren't throttled.
    if settings.is_production:
        from slowapi import _rate_limit_exceeded_handler  # noqa: PLC0415
        from slowapi.errors import RateLimitExceeded  # noqa: PLC0415
        from slowapi.middleware import SlowAPIMiddleware  # noqa: PLC0415

        from app.core.auth import limiter as _limiter  # noqa: PLC0415

        app.state.limiter = _limiter
        app.add_middleware(SlowAPIMiddleware)
        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # Cloudflare-only ingress gate. Outermost SECURITY layer — rejects direct
    # .fly.dev / Fly-IP hits with 403; /api/health is exempt so Fly's internal
    # machine checker still passes. No-op when the secret is empty (dev/test/e2e).
    # Registered second-to-last so CSPMiddleware (registered last → runs outermost)
    # still sets the CSP header on Cloudflare's 403 responses.
    from app.core.auth import CloudflareIngressMiddleware  # noqa: PLC0415

    app.add_middleware(CloudflareIngressMiddleware)

    # Content-Security-Policy header injection. Must be the LAST add_middleware
    # call so it runs OUTERMOST (FastAPI reverses registration order) — that way
    # it sets the CSP header on every response, including Cloudflare's 403s,
    # auth 401s, and rate-limit 429s. Mode (`report-only` / `enforce`) is
    # controlled by `YAAOS_CSP_MODE`.
    from app.core.webserver.csp import CSPMiddleware  # noqa: PLC0415

    app.add_middleware(CSPMiddleware)

    # AuthFailure → 401 with cleared yaaos_session + yaaos_csrf cookies.
    # Registered before the catch-all Exception handler below so it gets
    # first crack at the typed subclass.
    from app.core.auth import register_handler as _register_auth_failure  # noqa: PLC0415

    _register_auth_failure(app)

    # Unhandled-exception handler — log + return JSON 500.
    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logging.getLogger("yaaos").exception("http.unhandled_exception", exc_info=exc)
        return JSONResponse(status_code=500, content={"error": "internal_server_error"})


def _check_required_prod_secrets() -> None:
    """In production, refuse to start when any required secret is at its dev
    default. `dev`/`test` get to boot with stubs so the suite + onboarding
    flow keep working."""
    s = get_settings()
    if not s.is_production:
        return
    missing: list[str] = []
    if s.yaaos_oauth_state_secret.get_secret_value() == "dev-only-oauth-state-secret":
        missing.append("YAAOS_OAUTH_STATE_SECRET")
    if s.yaaos_invitation_token_secret.get_secret_value() == "dev-only-invitation-secret":
        missing.append("YAAOS_INVITATION_TOKEN_SECRET")
    if not s.yaaos_github_app_id:
        missing.append("YAAOS_GITHUB_APP_ID")
    if not s.yaaos_github_app_slug:
        missing.append("YAAOS_GITHUB_APP_SLUG")
    if not s.yaaos_github_app_private_key.get_secret_value():
        missing.append("YAAOS_GITHUB_APP_PRIVATE_KEY")
    if not s.yaaos_github_app_webhook_secret.get_secret_value():
        missing.append("YAAOS_GITHUB_APP_WEBHOOK_SECRET")
    if not s.yaaos_github_oauth_client_id:
        missing.append("YAAOS_GITHUB_OAUTH_CLIENT_ID")
    if not s.yaaos_github_oauth_client_secret.get_secret_value():
        missing.append("YAAOS_GITHUB_OAUTH_CLIENT_SECRET")
    if not s.yaaos_totp_master_key.get_secret_value():
        missing.append("YAAOS_TOTP_MASTER_KEY")
    # Cloudflare ingress secret: empty → CloudflareIngressMiddleware becomes a
    # transparent pass-through, silently disabling the outermost defense layer.
    if not s.yaaos_cloudflare_ingress_secret.get_secret_value():
        missing.append("YAAOS_CLOUDFLARE_INGRESS_SECRET")
    if missing:
        raise RuntimeError(f"yaaos refuses to start in prod with missing/stub secrets: {', '.join(missing)}")


def mount_specs(app: FastAPI, *, only: set[str] | None = None) -> None:
    """Mount every registered RouteSpec onto `app` at its effective prefix.

    Single source of truth for prefix resolution. Both the production lifespan
    and any test that builds a partial app must call this — never reimplement
    the prefix-derivation rule, or the test will silently diverge from prod
    (and silently pass against the wrong URL).

    `only` optionally restricts mounting to the named modules (useful for
    fast unit tests that don't need the full app).
    """
    for spec in get_specs().values():
        if only is not None and spec.module_name not in only:
            continue
        app.include_router(spec.router, prefix=spec.effective_prefix, tags=[spec.module_name])


def create_app() -> FastAPI:
    """FastAPI app factory. Called from main.py after all module imports have run."""
    _check_required_prod_secrets()
    app = FastAPI(title="yaaos", version="0.0.1", lifespan=_lifespan)
    _install_middleware(app)
    return app
