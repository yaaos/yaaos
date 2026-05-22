# core/auth

> Default-deny HTTP middleware + identity contextvars + the `Action` enum. Pure infrastructure — no DB access, no domain knowledge.

## Purpose

Owns the security middleware mounted on the FastAPI app, the contextvars that thread `org_id` / `user_id` / `actor_kind` / `actor_id` through one request, and the central `Action` enum that names every distinct privilege check in the codebase. The dependency factories that actually resolve sessions and memberships live in [`domain/sessions`](domain_sessions.md) (they need `domain/identity` + `domain/orgs`); `core/auth` is the contract those deps render against.

## Public interface

Exported from `app/core/auth/__init__.py`:

- `AuthMiddleware` — pure-ASGI middleware. Installed once in the app factory.
- `Action` — single enum naming every distinct privilege check.
- Contextvars — `org_id_var`, `user_id_var`, `actor_kind_var`, `actor_id_var`, `route_security_resolved`.
- Helpers — `current_org_id()`, `current_user_id()`, `current_actor_kind()`.
- `org_context(org_id, actor_kind, actor_id)` async context manager — background-job entrypoint that sets the same contextvars HTTP middleware sets. Phase 9 extends this with OTel + structlog wiring.
- Path predicates — `is_public_path(path)`, `is_m02_protected_path(path)`.

No HTTP routes. No tables.

## Module architecture

### Middleware order on `/api/*`

1. Reset every identity contextvar (defensive — ASGI may reuse the task).
2. **Public allowlist.** If the path is `/api/health` or starts with `/api/auth/`, set `route_security_resolved = "public"` and pass through.
3. **M02-protected paths** (`M02_PROTECTED_PREFIXES`). Require `X-Org-Slug` header — else 400.
4. Call the route. The route's `Depends(require(action))` (or `Depends(public_route)`) resolves the slug, loads the membership, checks the role, and sets the identity contextvars + `route_security_resolved`.
5. **Post-response guard.** On M02-protected paths only: if the response status is 2xx and `route_security_resolved` is still `None`, the route forgot its security declaration — substitute a 500 + log. 4xx/5xx responses pass through untouched (the route's dep raised an intended `HTTPException`).
6. Tag the active OTel span with `yaaos.org_id`, `yaaos.user_id`, `yaaos.actor_kind`.

The post-response guard catches the bug "I added a new endpoint and forgot the security dep" the moment a test hits it. Forgetting protection crashes, not leaks.

Implemented as a pure-ASGI middleware (not Starlette's `BaseHTTPMiddleware`) so contextvars set inside the route handler propagate back to the middleware on the way out — `BaseHTTPMiddleware` runs the downstream in a separate task and the mutations are invisible to the dispatch task.

### Enforcement scope

Only paths matching `M02_PROTECTED_PREFIXES` participate in the strict checks. Legacy `/api/*` routes that haven't been backfilled with security deps pass through unchanged. Phase 14 expands the set as the backfill completes.

### `Action` enum

Single grep-able catalogue of every distinct privilege check. Adding a new endpoint that needs role gating means:

1. Add the action to `Action` (e.g., `MEMBERS_INVITE = "members.invite"`).
2. Map it to its minimum `Role` in `domain/sessions/dependencies._REQUIRED_ROLE`.
3. Write `Depends(require(Action.MEMBERS_INVITE))` on the route.

A unit test asserts every `Action` member has a matching `_REQUIRED_ROLE` entry; forgetting the mapping breaks CI.

### Contextvars

Set by `require(...)` on success, by `org_context(...)` for background work, or by the middleware itself for the public allowlist (`route_security_resolved = "public"`). Consumed by:

- Logging — structlog injects them into every log line (Phase 9 wiring).
- OTel — middleware copies them onto the active span.
- Audit-log writes — `current_actor()` reads `user_id_var` to build an `Actor.user(...)`.

The vars reset to `None` at the start of every request so a leak from one request to the next is impossible.

### `org_context()`

Phase 1 ships the minimum: sets all four identity vars + `route_security_resolved = "background"` and resets them on exit. Phase 9 layers in the OTel span attrs + structlog `bind_contextvars` so background-job logs and traces carry the same fields HTTP requests do.

### Rate limiting (M02 Phase 13)

`slowapi` wraps the ASGI app in `prod`/`dev`; skipped in `test`. Limits are per-key with two keys:

- **Per-IP** on `/api/auth/*` — anonymous endpoints (`/login`, `/callback/*`, `/totp/challenge`, `/providers`). Default: 30/minute. Catches credential-stuffing + replay.
- **Per-user** on mutating `/api/*` — every `POST`/`PUT`/`PATCH`/`DELETE` keyed by the `yaaos_session` cookie value (falls back to per-IP for anonymous mutations). Default: 120/minute.

Limits live as `@limiter.limit("...")` decorators on handlers in M02+; the global default is empty so legacy routes don't suddenly throttle. Exceeded → 429 `Retry-After: <s>`.

### Secret hygiene (M02 Phase 13)

`create_app()` calls `_check_required_prod_secrets()` before mounting middleware. In `prod`, missing/stub values for `YAAOS_OAUTH_STATE_SECRET`, `YAAOS_INVITATION_TOKEN_SECRET`, `YAAOS_OAUTH_GITHUB_CLIENT_ID`, `YAAOS_OAUTH_GITHUB_CLIENT_SECRET`, or `YAAOS_TOTP_MASTER_KEY` raise `RuntimeError` at boot. Dev/test boot with the bundled defaults. See [`docs/runbooks/secret-rotation.md`](../../../docs/runbooks/secret-rotation.md) for rotation procedure.

## Data owned

None.

## How it's tested

`apps/backend/app/domain/sessions/test/test_middleware.py` exercises every middleware behavior via an ad-hoc FastAPI app driven by `httpx.AsyncClient` over an ASGI transport (asyncpg refuses to straddle event loops, so `fastapi.testclient.TestClient` is unsuitable).

Coverage:
- Missing `X-Org-Slug` → 400.
- Unknown slug → 404 (same shape as "no membership" — existence is not leaked).
- Wrong role → 403.
- Success → 200, with contextvars set.
- Route under an M02-protected prefix with no security dep → 500.
- Public allowlist (`/api/auth/login`, `/api/health`) bypasses the header check.
- Legacy `/api/*` paths not in the protected set pass through unchanged.
- `Action` enum coverage check — every member must map to a `Role`.
