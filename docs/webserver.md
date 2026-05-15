# `core/webserver`

FastAPI app factory, `RouteSpec` registry, `/api/health` carve-out, SPA serving, lifespan composition.

## Public interface

```python
from app.core.webserver import create_app, register_routes, RouteSpec
```

- `create_app()` — FastAPI app factory. Called from `main.py` after all module imports have triggered their `register_routes(RouteSpec(...))` side effects.
- `RouteSpec` — Pydantic model carrying `module_name`, `url_prefix?`, `router`, `on_startup`, `on_shutdown`.
- `register_routes(spec)` — validates the spec and stores it in the module-level registry. Domain modules call this at import time from their `__init__.py`.

## One URL prefix per module (enforced)

`register_routes` validates at import time:

1. `router` must NOT carry its own prefix (webserver applies it).
2. `module_name` must be unique across all registrations.
3. The effective prefix (= `url_prefix or f"/api/{module_name}"`) must be unique and non-overlapping with any other.
4. The effective prefix must start with `/api/` and not end with `/`.

Violations raise `ValueError` immediately so the offending module's stack frame is in the traceback.

## Framework carve-out

`/api/health` is owned by `core/webserver` directly (the `health_router` in `health.py`), bypassing the `register_routes` registry. The one-URL-prefix-per-module rule applies to domain modules only — framework routes (`/api/health`, `/openapi.json`, `/docs`) live on the webserver itself.

`GET /api/health` returns:

```json
{ "status": "ok", "db_ok": true, "version": "0.0.1" }
```

`status` is `"ok"` when `db_ok` is true, `"degraded"` otherwise. Always returns HTTP 200.

## SPA serving

In production (when `apps/web/dist` exists), the lifespan mounts `/assets/*` and a catch-all that serves `index.html` for any non-`/api/` path. In dev (no `dist` directory), the catch-all is skipped — Vite serves the SPA on `:5173` and proxies `/api/*` to FastAPI on `:8080`.

## Owned data

None.

## Tests

`app/core/webserver/test/test_health.py` — integration test for the health endpoint via `TestClient`. Asserts the response shape and that `status` matches `db_ok`.
