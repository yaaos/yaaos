# core/auth

> Default-deny HTTP middleware + identity contextvars + the `Action` enum + the route-security taxonomy. Pure infrastructure — no DB access, no domain knowledge.

## Purpose

Owns the security middleware mounted on the FastAPI app, the contextvars that thread `org_id` / `user_id` / `actor_kind` / `actor_id` through one request, the central `Action` enum that names every distinct privilege check, and the `RouteSecurity` taxonomy that classifies every `/api/*` path into one of three categories. The dependency factories that actually resolve sessions and memberships live in [`core/sessions`](core_sessions.md) (they need `core/identity` + `domain/orgs`); `core/auth` is the contract those deps render against.

## Public interface

Exported from `app/core/auth/__init__.py`:

- `AuthMiddleware` — pure-ASGI middleware. Installed once in the app factory.
- `Action` — single enum naming every distinct privilege check.
- `RouteSecurity` — three-value enum: `PUBLIC`, `USER_SCOPED`, `ORG_SCOPED`.
- Contextvars — `org_id_var`, `user_id_var`, `actor_kind_var`, `actor_id_var`, `route_security_resolved`.
- Helpers — `current_org_id()`, `current_user_id()`, `current_actor_kind()`.
- `org_context(org_id, actor_kind, actor_id)` async context manager — background-job entrypoint that sets the same contextvars HTTP middleware sets.
- Route classifier — `classify_route(path, method)`, `is_org_scoped_path(path, method)`. Backed by `PUBLIC_PREFIXES` / `PUBLIC_EXACT`, `USER_SCOPED_PREFIXES` / `USER_SCOPED_EXACT` / `USER_SCOPED_METHOD_EXACT`, `ORG_SCOPED_PREFIXES`.

No HTTP routes. No tables.

## Module architecture

### Route categories

Every `/api/*` path classifies as exactly one of three:

| `RouteSecurity` | Session required? | `X-Org-Slug` required? | Role check? | Examples |
|---|---|---|---|---|
| `PUBLIC` | no | no | n/a | `/api/auth/login`, `/api/auth/logout`, `/api/health`, `/api/sso/*`, `/api/mcp/*`, OAuth callbacks |
| `USER_SCOPED` | yes (via route dep) | **no** | n/a | `/api/user/*`, `/api/auth/me`, `/api/notifications`, `/api/orgs/mine`, `POST /api/orgs` |
| `ORG_SCOPED` | yes (via route dep) | **yes** | yes (`require(action)`) | `/api/memberships/*`, `/api/audit`, `/api/vcs/*`, `/api/coding-agents/*`, `GET /api/orgs/*`, `/api/tickets/*`, `/api/lessons/*`, `/api/reviewer/*` |

`classify_route(path, method)` is the single source of truth. Method-specific exact matches win over exact matches; exact wins over prefix. `POST /api/orgs` lives in `USER_SCOPED_METHOD_EXACT` so org-create works before the SPA has selected an org, while `GET /api/orgs` stays `ORG_SCOPED`.

Unclassified `/api/*` paths fall through as `PUBLIC` — legacy routers (intake, parts of `/api/reviewer` not yet backfilled) keep working. The post-response guard catches any 2xx response that escapes without `route_security_resolved` being set.

### Middleware order on `/api/*`

1. Reset every identity contextvar (defensive — ASGI may reuse the task).
2. Call `classify_route(path, request.method)`:
   - **`PUBLIC`** — set `route_security_resolved = "public"`, pass through.
   - **`USER_SCOPED`** — set `route_security_resolved = "user_scoped"`. Run CSRF double-submit check on mutating methods. Pass through to the route dep, which enforces session presence.
   - **`ORG_SCOPED`** — if `X-Org-Slug` is missing, return 400. Run CSRF check on mutating methods. Pass through. The route's `Depends(require(action))` resolves the slug, loads the membership, checks the role, and sets the identity contextvars + `route_security_resolved = "org_scoped"`.
3. **Post-response guard (any `/api/*`).** If the response status is 2xx and `route_security_resolved` is still `None`, the route forgot its security declaration — substitute a 500 + log. 4xx/5xx pass through untouched (the dep raised an intended `HTTPException`).
4. Tag the active OTel span with `yaaos.org_id`, `yaaos.user_id`, `yaaos.actor_kind`.

The post-response guard catches "I added a new endpoint and forgot the security dep" the moment a test hits it. Forgetting protection crashes, not leaks.

Implemented as a pure-ASGI middleware (not Starlette's `BaseHTTPMiddleware`) so contextvars set inside the route handler propagate back to the middleware on the way out — `BaseHTTPMiddleware` runs the downstream in a separate task and the mutations are invisible to the dispatch task.

### `Action` enum

Single grep-able catalogue of every distinct privilege check used by `ORG_SCOPED` routes. Adding a new org-scoped endpoint with role gating means:

1. Add the action to `Action` (e.g., `MEMBERS_INVITE = "members.invite"`).
2. Map it to its minimum `Role` in `core/sessions/dependencies._REQUIRED_ROLE`.
3. Write `Depends(require(Action.MEMBERS_INVITE))` on the route.

A unit test asserts every `Action` member has a matching `_REQUIRED_ROLE` entry; forgetting the mapping breaks CI.

`USER_SCOPED` routes don't need an `Action` — there's no role check. They use `Depends(require_session)` (or, for the `/api/orgs/mine` + `/api/auth/me` pattern, the handler resolves the cookie itself with `Depends(public_route)` as the security marker).

### Contextvars

Set by `require(...)` on success, by `org_context(...)` for background work, or by the middleware itself based on `RouteSecurity`. `route_security_resolved` takes one of:

- `"public"` — middleware classification or `Depends(public_route)`.
- `"user_scoped"` — middleware classification.
- `"org_scoped"` — `require(action)` resolved the membership.
- `"background"` — `org_context(...)` for background jobs.

Consumed by:

- Logging — structlog injects identity into every log line.
- OTel — middleware copies them onto the active span.
- Audit-log writes — `current_actor()` reads `user_id_var` to build an `Actor.user(...)`.

The vars reset to `None` at the start of every request so a leak from one request to the next is impossible.

### `org_context()`

Async context manager for background jobs: sets all four identity vars + `route_security_resolved = "background"` + OTel span attrs + structlog `bind_contextvars`, and resets them on exit.

### Rate limiting

`slowapi` wraps the ASGI app in `prod`/`dev`; skipped in `test`. Limits are per-key with two keys:

- **Per-IP** on `/api/auth/*` — anonymous endpoints (`/login`, `/callback/*`, `/totp/challenge`, `/providers`). Default: 30/minute. Catches credential-stuffing + replay.
- **Per-user** on mutating `/api/*` — every `POST`/`PUT`/`PATCH`/`DELETE` keyed by the `yaaos_session` cookie value (falls back to per-IP for anonymous mutations). Default: 120/minute.

Limits live as `@limiter.limit("...")` decorators on handlers; the global default is empty so legacy routes don't suddenly throttle. Exceeded → 429 `Retry-After: <s>`.

### Secret hygiene

`create_app()` calls `_check_required_prod_secrets()` before mounting middleware. In `prod`, missing/stub values for `YAAOS_OAUTH_STATE_SECRET`, `YAAOS_INVITATION_TOKEN_SECRET`, `YAAOS_OAUTH_GITHUB_CLIENT_ID`, `YAAOS_OAUTH_GITHUB_CLIENT_SECRET`, or `YAAOS_TOTP_MASTER_KEY` raise `RuntimeError` at boot. Dev/test boot with the bundled defaults. See [`docs/runbooks/secret-rotation.md`](../../../docs/runbooks/secret-rotation.md) for rotation procedure.

## Data owned

None.

## How it's tested

`apps/backend/app/core/sessions/test/test_middleware.py` exercises every middleware behavior via an ad-hoc FastAPI app driven by `httpx.AsyncClient` over an ASGI transport (asyncpg refuses to straddle event loops, so `fastapi.testclient.TestClient` is unsuitable).

Coverage:
- Missing `X-Org-Slug` on `ORG_SCOPED` → 400. Missing on `USER_SCOPED` → 200 (or 401 if no session).
- Unknown slug → 404 (same shape as "no membership" — existence is not leaked).
- Wrong role → 403.
- Success → 200, with contextvars set.
- Route under an `ORG_SCOPED` prefix with no security dep → 500.
- `PUBLIC` paths bypass header + session checks; handler can still 401 if it does its own session lookup.
- Legacy unclassified `/api/*` paths pass through as `PUBLIC`.
- `Action` enum coverage check — every member must map to a `Role`.
- `apps/backend/app/core/auth/test/test_route_security_triplet.py` enumerates every `ORG_SCOPED` route at registration time and asserts the anonymous trio (no header → 400; header but no session → 401; never 2xx without auth).
