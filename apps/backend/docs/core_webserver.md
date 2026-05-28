# core/webserver

> FastAPI app factory, lifespan, route registry, middleware, and SPA serving.

## Scope

- Owns: `RouteSpec` registry, `create_app()`, lifespan, middleware stack, SPA static serving, shutdown registries.
- Does NOT own: auth/CSRF/rate limiting (those are in [`core/auth`](core_auth.md)).
- `/api/health` is a framework carve-out (`core/webserver/health.py`) — not registered via `RouteSpec`.

## Why / invariants

**One URL prefix per module** — `register_routes(spec)` enforces: empty `router.prefix`, unique `module_name`, non-overlapping effective prefix, starts with `/api/`, no trailing `/`. Violations surface at import time. See `app/core/webserver/registry.py`.

**Lifespan order:**
1. Mount each router at its prefix.
2. Run `on_startup` hooks — raising crashes boot (loud-by-design).
3. Mount SPA if `apps/web/dist` exists.
4. On shutdown: iterate `iter_web_shutdown_hooks()` in reverse registration order; errors logged + swallowed so all hooks run.

**Cache-Control on SPA files:**
- `/assets/*` → `max-age=31536000, immutable` — Vite content-hashes bundles; URL changes when content does.
- `index.html` + dist-root files → `max-age=60, must-revalidate` — not content-hashed; new deploys must be picked up quickly.

**Middleware stack** (outermost-first): OTel `FastAPIInstrumentor` → request-ID middleware → CORS (`["*"]` in dev; `YAAOS_CORS_ORIGINS` otherwise) → unhandled-exception handler (500 JSON + log).

## Gotchas

- In dev (no `apps/web/dist`), non-`/api/` paths 404. Run `pnpm dev` separately.
- SPA catch-all guards path traversal via `relative_to(dist)`.
- `RouteSpec` is a Pydantic model; construction validates prefix rules before anything is registered.

