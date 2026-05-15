# `core/webserver` — Internal Architecture

> FastAPI app composition, route registry, middleware stack, lifespan, and SPA static-file serving.

## Purpose

`core/webserver` is yaaof's HTTP boundary. It owns:

- The FastAPI app factory.
- The lifespan implementation (runs the documented bootstrap order).
- The middleware stack (exception handler, request-ID, OTel, CORS).
- The route registry that domain modules + plugins plug into via `register_routes(RouteSpec(...))`.
- Static file serving for the built React SPA (`apps/web/dist`).

No business logic. No data tables. Domain modules and plugins register their routers; webserver mounts them at startup.

## Public interface (`__all__`)

```python
"RouteSpec",
"register_routes",
"create_app",        # FastAPI app factory; called by main.py
```

## `RouteSpec`

Pydantic model — params bag for route registration.

```python
class RouteSpec(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)  # for APIRouter

    module_name: str               # e.g., "repos", "github" — used for OpenAPI tag + telemetry
    url_prefix: str | None = None  # optional override; defaults to f"/api/{module_name}"
    router: APIRouter              # the module's FastAPI router — MUST NOT carry its own prefix
    on_startup: list[Callable[[], Awaitable[None]]] = []    # crash-recovery + warm-up hooks
    on_shutdown: list[Callable[[], Awaitable[None]]] = []   # optional async cleanup hooks
```

Pydantic validates `module_name` is non-empty. `arbitrary_types_allowed` is needed because FastAPI's `APIRouter` isn't a Pydantic type. The effective prefix used at mount time is `spec.url_prefix or f"/api/{spec.module_name}"`.

## One URL prefix per module (enforced)

**Rule:** each module owns **exactly one** top-level URL namespace under `/api/`, and that namespace belongs to that module alone. No module may register multiple prefixes; no two modules may share or overlap a prefix.

This makes the URL tree mirror the module map: looking at a URL tells you which module handles it; looking at a module tells you exactly which URLs it serves. The OpenAPI doc, the frontend's API client, and the audit log all benefit from the bijection.

**How it's enforced** — `register_routes` validates at registration time and raises if any rule is violated, so the failure surfaces at import (before the app finishes booting). Specifically:

1. **`router.prefix` must be empty.** The router passed in must not carry its own prefix; the webserver applies `spec.url_prefix` (or the default) when mounting. A module that pre-prefixes its router is bypassing the registry's enforcement → raise.
2. **`module_name` is unique** across all `RouteSpec`s. A second registration with the same `module_name` → raise. (Modules register exactly once.)
3. **The effective prefix is unique**. No two specs may have the same `url_prefix` (after defaulting), and no prefix may be a path-prefix of another (e.g., `/api/foo` and `/api/foo/bar` would overlap) → raise.
4. **The effective prefix must start with `/api/`** and must not end with `/`. SPA catch-all routes everything else; non-`/api/` server routes are forbidden.
5. **The default is `/api/<module_name>`.** Overrides are allowed (pluralization, hyphenation, legacy paths) but discouraged — pick a module name that matches the desired URL.

Validation runs in `register_routes` itself, not at lifespan startup, so the offending module's import stack frame is in the traceback.

## `register_routes()` and the registry

```python
_specs: dict[str, RouteSpec] = {}            # keyed by module_name for O(1) uniqueness check
_claimed_prefixes: dict[str, str] = {}       # effective_prefix → module_name

def register_routes(spec: RouteSpec) -> None:
    """Called at module import time (in each module's __init__.py or web.py).
    Validates the one-prefix-per-module rule. Raises ValueError on any violation."""
    if spec.router.prefix:
        raise ValueError(
            f"{spec.module_name}: router must not carry its own prefix "
            f"(got {spec.router.prefix!r}); set url_prefix on the RouteSpec instead."
        )
    if spec.module_name in _specs:
        raise ValueError(f"module {spec.module_name!r} already registered routes")
    prefix = spec.url_prefix or f"/api/{spec.module_name}"
    if not prefix.startswith("/api/") or prefix.endswith("/"):
        raise ValueError(
            f"{spec.module_name}: url_prefix must start with '/api/' and not end with '/' (got {prefix!r})"
        )
    for claimed, claimant in _claimed_prefixes.items():
        if prefix == claimed or prefix.startswith(claimed + "/") or claimed.startswith(prefix + "/"):
            raise ValueError(
                f"{spec.module_name}: prefix {prefix!r} overlaps with {claimed!r} (owned by {claimant!r})"
            )
    _specs[spec.module_name] = spec
    _claimed_prefixes[prefix] = spec.module_name
```

The registry is module-level. Modules register at import time, before the lifespan runs.

## `create_app()` — app factory

```python
def create_app() -> FastAPI:
    app = FastAPI(lifespan=_lifespan, title="yaaof")
    _install_middleware(app)
    return app
```

That's it. The lifespan handles the rest.

## Lifespan implementation

```python
@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Steps 1-3 of the bootstrap order have already run by the time this fires
    # (env loaded, infrastructure configured, runtimes initialized — in main.py
    # before create_app() is called).

    # Steps 4-5: import domain modules + plugins.
    # These are already imported by the time main.py reaches `create_app()`;
    # their `register_routes(RouteSpec(...))` calls populated `_specs` at
    # import-time.

    # Step 6: mount routers.
    for spec in _specs.values():
        prefix = spec.url_prefix or f"/api/{spec.module_name}"
        app.include_router(spec.router, prefix=prefix, tags=[spec.module_name])

    # Step 7: run on_startup hooks. Each module's hook handles its own
    # crash-recovery (e.g., reviewer marks pre-restart 'running' rows as
    # 'failed' and respawns 'queued' rows; workspace flips orphaned states
    # to 'expired'). A hook that raises crashes the boot — fix the bug.
    for spec in _specs.values():
        for handler in spec.on_startup:
            await handler()

    # Mount SPA static files + catch-all (see "SPA serving" below).
    _install_spa_serving(app)

    yield

    # On shutdown — call each spec's on_shutdown handlers.
    for spec in _specs.values():
        for handler in spec.on_shutdown:
            try:
                await handler()
            except Exception:
                # Log but don't propagate — best-effort POC cleanup.
                log.exception("on_shutdown handler raised", module=spec.module_name)
```

See [../patterns.md § Bootstrap composition order](../patterns.md#bootstrap-composition-order) for the order context.

## Middleware stack

Installed in order (outer-most first):

1. **OTel `FastAPIInstrumentor`** — auto-instruments every request as a span. From the `opentelemetry-instrumentation-fastapi` contrib package.
2. **Request-ID middleware** — generates a UUID per request, sets `request_meta_var` ContextVar (from `core/observability`) with `{request_id, ...}`. Log lines + spans inside the request carry it.
3. **CORS middleware** — `fastapi.middleware.cors.CORSMiddleware`. M01 default: `allow_origins=["*"]` in dev (`YAAOF_ENV=dev`); allowlist from `YAAOF_CORS_ORIGINS` env in non-dev.
4. **Exception handler** — registered via `app.add_exception_handler(Exception, _handle_unhandled)`. Catches anything not caught earlier, logs `kind='http.unhandled_exception'` with structured fields, returns `JSONResponse(status_code=500, content={"error": "internal_server_error"})`.

**No CSRF middleware.** M01 has no auth. When auth lands, the standard SPA pattern is JWT in `Authorization` header — bearer tokens aren't auto-attached by browsers, so CSRF doesn't apply. Re-evaluate only if cookie-based session auth is ever adopted.

## SPA serving

```python
def _install_spa_serving(app: FastAPI):
    spa_dist = Path("apps/web/dist")
    if not spa_dist.exists():
        # Dev mode: SPA is served by Vite separately. Skip mounting.
        return
    app.mount("/assets", StaticFiles(directory=spa_dist / "assets"), name="assets")
    # Catch-all for client-side routing
    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa_catchall(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(404)
        return FileResponse(spa_dist / "index.html")
```

- `/assets/*` serves built JS/CSS files.
- Any other non-`/api/` GET returns `index.html` so the client-side router handles it.
- In dev (when `apps/web/dist` doesn't exist because Vite is serving on a different port), the catch-all isn't installed; requests to non-`/api/` paths 404. Developer runs `pnpm dev` separately.

## Lifecycle of registration

1. **At import time** (M01: in each `__init__.py` or `web.py`): the module's code calls `register_routes(RouteSpec(...))`. This appends to the module-level `_specs` list.
2. **At lifespan startup**: webserver iterates `_specs` and calls `app.include_router(...)` for each. After this point, the FastAPI app has all routes mounted.
3. **At lifespan shutdown**: webserver iterates `_specs` again and runs every `on_shutdown` handler.

## What `core/webserver` does NOT do

- Does not own auth. No middleware for authn/authz in M01.
- Does not own rate limiting. Add when a real abuse vector exists.
- Does not parse webhook payloads. Plugins register their own routes for that (the github plugin's webhook receiver is just another `RouteSpec`).
- Does not handle SSE — SSE endpoints are normal FastAPI routes registered by `core/events` (or a domain module that surfaces events).
- Does not write to the database. Stateless.

## What it explicitly does in POC mode

- Permissive CORS (`*`) when `YAAOF_ENV=dev`. Will tighten for prod deploys.
- Single-process, single-image. No nginx fronting. No graceful drain on shutdown.
- Catch-all SPA serving is naive: any non-`/api/` path returns `index.html`. No 404 page for genuinely-missing client routes — the React app handles that.

## Decisions

### 2026-05-14 — `RouteSpec` is a Pydantic model; `register_routes(spec)` is the single entry point
Type-safe params bag; future fields can be added without changing the function signature. Functional registry.

### 2026-05-15 — `RouteSpec.on_startup` for crash-recovery hooks
`RouteSpec` carries an optional `on_startup: list[Callable[[], Awaitable[None]]]` symmetric to `on_shutdown`. Modules with startup recovery work (e.g., `domain/reviewer` marking pre-restart `running` review_jobs as `failed`) register their hooks via the same `RouteSpec` they already use for routes. Hooks run after routers are mounted, before SPA serving + `yield`. A hook that raises crashes the boot — startup failures should be loud.
**Why:** crash recovery is per-module and needs a registry; reusing `RouteSpec` keeps the surface minimal (no second registry), and every module needing startup work already registers routes.

### 2026-05-15 — One URL prefix per module, enforced at registration time
Each module owns exactly one top-level `/api/<name>` namespace. `register_routes` validates that (a) the passed router carries no prefix, (b) `module_name` is unique, (c) the effective `url_prefix` doesn't equal or overlap any other registered prefix, (d) the prefix starts with `/api/` and doesn't end with `/`. Default prefix is `/api/<module_name>`; overrides allowed but discouraged.
**Why:** without enforcement, two modules can silently mount overlapping routes, or one module can sprawl across multiple namespaces. Both make the URL tree stop mirroring the module map. Validation at import time catches it before the app finishes booting, with the offending module in the traceback.

### 2026-05-14 — Single Docker image; FastAPI serves SPA
`apps/web/dist` mounted under `/assets`; catch-all serves `index.html`. No nginx, no second container in POC.

### 2026-05-14 — Four-middleware stack: OTel, request-id, CORS, exception handler
No CSRF (header-auth SPA pattern sidesteps it). No rate limiting (no abuse vector in POC). No auth middleware (no auth in M01).

### 2026-05-14 — Lifespan implements steps 4–6 of the bootstrap order
Steps 1–3 are `main.py`'s responsibility (run before `create_app()` returns); steps 4–5 happen at module-import time (`register_routes` calls fire); step 6 (mount routers) runs in the lifespan startup phase.
