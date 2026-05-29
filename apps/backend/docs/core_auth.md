# core/auth

> Default-deny HTTP middleware + identity contextvars + the `Action` enum + the `Role` enum + the role-policy map + the route-security taxonomy. Pure infrastructure — no DB access, no domain knowledge.

## Scope

- Owns: `AuthMiddleware`, `RouteSecurity` enum, `Action` enum, `Role` enum, `_REQUIRED_ROLE` map, `required_role_for(action)`, identity contextvars, `org_context()`, `classify_route()`.
- Does NOT own: session-cookie resolution or membership lookup — those are in [`core/sessions`](core_sessions.md), which also owns `/api/auth/*` routes.
- Emits: sets `org_id_var`, `user_id_var`, `actor_kind_var`, `actor_id_var`, `route_security_resolved`. Consumed by structlog, OTel middleware, and audit-log writes.

## Role

`Role` (StrEnum) is the shared authorization primitive. Three tiers: `OWNER ≥ ADMIN ≥ BUILDER`. `role.covers(required)` is the only comparison — the integer rank table is private. All layers that need the role type import it from `core.auth`, not from `domain.orgs`.

`_REQUIRED_ROLE: dict[Action, Role]` maps every `Action` member to its minimum `Role`. `required_role_for(action)` is the public accessor. `core/sessions.dependencies` builds the `require(action)` dep factory on top of these. A CI test (`test_role_covers.test_every_action_has_a_required_role`) asserts full coverage.

## Why / invariants

**Route taxonomy** — every `/api/*` path is exactly one of three categories:

| `RouteSecurity` | Session? | `X-Org-Slug`? | Role check? |
|---|---|---|---|
| `PUBLIC` | no | no | no |
| `USER_SCOPED` | yes (route dep) | no | no |
| `ORG_SCOPED` | yes (route dep) | yes | yes (`require(action)`) |

`classify_route(path, method)` is the single source of truth. Method-exact > exact > prefix. Unclassified `/api/*` falls through as `PUBLIC`.

**Middleware order on `/api/*`:**
1. Reset all identity contextvars (ASGI may reuse the task).
2. Classify route. `ORG_SCOPED` without `X-Org-Slug` → 400 immediately. `USER_SCOPED` and `ORG_SCOPED` mutations → CSRF double-submit check.
3. Post-response guard: if response is 2xx and `route_security_resolved` is still `None`, substitute 500 + log. Forgetting a security dep crashes, not leaks.
4. Tag OTel span with `yaaos.org_id`, `yaaos.user_id`, `yaaos.actor_kind`.

**`POST /api/orgs` is `USER_SCOPED`, not `ORG_SCOPED`** — org-create must work before the SPA has selected an org. Lives in `USER_SCOPED_METHOD_EXACT`.

**`org_context(org_id, actor_kind, actor_id)`** — async context manager for background jobs. Sets the same four identity vars + `route_security_resolved = "background"` + OTel attrs + structlog `bind_contextvars`. Resets on exit.

**Pure-ASGI, not `BaseHTTPMiddleware`** — contextvars set inside the route handler propagate back to the middleware on the way out. `BaseHTTPMiddleware` runs downstream in a separate task, making those mutations invisible.

**`Action` enum** — single grep-able catalogue of every distinct privilege check. Adding an `ORG_SCOPED` endpoint requires: (1) add to `Action`, (2) map to its minimum `Role` in `core/auth/role_policy._REQUIRED_ROLE`, (3) `Depends(require(Action.X))` on the route. A CI test asserts every `Action` member has a `_REQUIRED_ROLE` entry.

## Gotchas

- **Rate limiting** — `slowapi` wraps the app in `prod`/`dev`; skipped in `test`. Per-IP on `/api/auth/*` (30/min); per-user on all mutating `/api/*` (120/min, keyed by session cookie, falls back to IP for anonymous). Exceeded → 429 `Retry-After`.
- **Prod secret check** — `create_app()` calls `_check_required_prod_secrets()`. Missing/stub `YAAOS_OAUTH_STATE_SECRET`, `YAAOS_INVITATION_TOKEN_SECRET`, `YAAOS_OAUTH_GITHUB_CLIENT_ID`, `YAAOS_OAUTH_GITHUB_CLIENT_SECRET`, or `YAAOS_TOTP_MASTER_KEY` in `prod` → `RuntimeError` at boot. See [`docs/runbooks/secret-rotation.md`](../../../docs/runbooks/secret-rotation.md).

