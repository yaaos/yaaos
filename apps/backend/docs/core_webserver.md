# core/webserver

> FastAPI app factory, lifespan, route registry, middleware, and SPA serving.

## Purpose

yaaos's HTTP boundary. Owns the FastAPI app factory, lifespan, middleware stack, the `RouteSpec` registry every module plugs into, and static-file serving for the built React SPA. No business logic; no tables.

## Public interface

Exports `RouteSpec`, `register_routes`, `create_app`, `ShutdownHook`, `register_web_shutdown_hook`, `iter_web_shutdown_hooks`, `register_worker_shutdown_hook`, `iter_worker_shutdown_hooks`. See `apps/backend/app/core/webserver/__init__.py`.

- `RouteSpec` — Pydantic model with `module_name`, optional `url_prefix`, `router`, optional `on_startup` / `on_shutdown` hooks.
- `register_routes(spec)` — called at module import; validates one-prefix-per-module.
- `create_app()` — returns the FastAPI app; called from `app/web.py` after all modules import.
- `ShutdownHook` — `Callable[[], Awaitable[None]]` type alias. Re-exported from `core/shutdown_registry`.
- `register_web_shutdown_hook(hook)` / `iter_web_shutdown_hooks()` — web shutdown registry. Re-exported from `core/shutdown_registry`.
- `register_worker_shutdown_hook(hook)` / `iter_worker_shutdown_hooks()` — worker shutdown registry. Re-exported from `core/shutdown_registry`.

`/api/health` is a framework carve-out owned by `core/webserver/health.py` and does NOT go through the registry.

## Module architecture

### One URL prefix per module (enforced)

Each module owns exactly one top-level `/api/` namespace. `register_routes` validates at registration; violations surface in the offending module's import traceback. Rules:

1. The passed `router.prefix` MUST be empty — `RouteSpec.url_prefix` (default `/api/{module_name}`) is applied.
2. `module_name` is unique across registrations.
3. Effective prefix doesn't equal or overlap any other (`/api/foo` and `/api/foo/bar` cannot both register).
4. Prefix starts with `/api/` and doesn't end with `/`.

Registry is module-level: a `_specs` map keyed by `module_name` and a `_claimed_prefixes` map by effective prefix. See `apps/backend/app/core/webserver/registry.py`.

### Lifespan

Boot order:
1. Mount each registered router at its prefix.
2. Run every `on_startup` hook — raising crashes the boot (loud-by-design).
3. Mount SPA `/assets` + catch-all if `apps/web/dist` exists.
4. Yield.
5. Iterate `iter_web_shutdown_hooks()` in reverse registration order, calling each hook. Errors are logged and swallowed so all hooks run. See [patterns.md § Two process lifecycles, two registries](patterns.md).

By the time the lifespan fires, every module has been imported by `app/web.py` so `register_routes(...)` calls have populated `_specs`. Side-effect: each module's `__init__` also calls `register_web_shutdown_hook(shutdown)` so the lifespan loop covers every registered resource.

### Middleware stack

Installed outer-most first:

1. `FastAPIInstrumentor` — OTel auto-instruments every request.
2. Request-ID middleware — generates a UUID per request, sets `request_meta_var` ContextVar. Log lines + spans inside the request carry it.
3. CORS — `allow_origins=["*"]` in dev; allowlist from `YAAOS_CORS_ORIGINS` otherwise.
4. Exception handler — catches anything not caught earlier; logs `kind="http.unhandled_exception"`; returns 500 JSON.

No CSRF, no rate limiting, no auth — the security baseline is encryption-at-rest + HMAC webhook verification + parametrized SQL (see [`docs/system-architecture.md`](../../../docs/system-architecture.md)).

### SPA serving

- `apps/web/dist/assets/*` mounted at `/assets/`.
- Non-`/api` paths: serve the matching real file from `dist/` when one exists (favicon.svg, og-image.png, robots.txt — anything Vite copies from `public/`); otherwise fall through to `index.html` (client router takes over). Path-traversal guarded by `relative_to(dist)`.
- `/api/*` 404s from the catch-all when no route matches.

In dev (no `apps/web/dist`), the catch-all isn't installed; non-`/api/` paths 404. Developer runs `pnpm dev` separately.

### Cache-Control on SPA files

- `/assets/*` → `Cache-Control: public, max-age=31536000, immutable`. Safe because Vite content-hashes every bundle (`index-<hash>.js`) — the URL changes whenever the content does.
- `index.html` and other dist-root files (favicon, og-image) → `Cache-Control: public, max-age=60, must-revalidate`. Not content-hashed, so a new deploy must be picked up quickly.

Cloudflare honors these standard headers; no CDN-specific config required.

### `/api/health` carve-out

Owned by `core/webserver/health.py` directly — not via `RouteSpec`. Returns `{status: "ok"|"degraded", db_ok: bool, redis_ok: bool, version: str}`. Pings both [`core/database`](core_database.md) and [`core/redis`](core_redis.md); status is `degraded` if either is unreachable. Kept out of the registry so it survives even if no module registers anything.

## Data owned

None. The registry is in-memory only.

## How it's tested

`app/core/webserver/test/` covers `RouteSpec` validation (prefix overlap, missing prefix, etc.), `create_app()` boots cleanly with no registered routes, health returns `200`, shutdown registry registration and iteration, and lifespan teardown calling hooks in reverse order. Lifespan ordering + module-mounting is covered indirectly by every integration test running through `TestClient`.
