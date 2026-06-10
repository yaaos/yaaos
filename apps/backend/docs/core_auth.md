# core/auth

> Default-deny HTTP middleware + identity contextvars + the `Action` enum + the `Role` enum + the role-policy map + the route-security taxonomy. Pure infrastructure — no DB access, no domain knowledge.

## Scope

- Owns: `CloudflareIngressMiddleware`, `AuthMiddleware`, `RouteSecurity` enum, `Action` enum, `Role` enum, `_REQUIRED_ROLE` map, `required_role_for(action)`, identity contextvars, `org_context()`, `classify_route()`.
- Does NOT own: session-cookie resolution or membership lookup — those are in [`core/sessions`](core_sessions.md), which also owns `/api/auth/*` routes.
- Emits: sets `org_id_var`, `user_id_var`, `actor_kind_var`, `actor_id_var`, `workflow_execution_id_var`, `command_id_var`, `route_security_resolved`. Consumed by structlog, OTel span processor, log filter, and audit-log writes.

## Role

`Role` (StrEnum) is the shared authorization primitive. Three tiers: `OWNER ≥ ADMIN ≥ BUILDER`. `role.covers(required)` is the only comparison — the integer rank table is private. All layers that need the role type import it from `core.auth`, not from `domain.orgs`.

`_REQUIRED_ROLE: dict[Action, Role]` maps every `Action` member to its minimum `Role`. `required_role_for(action)` is the public accessor. `core/sessions.dependencies` builds the `require(action)` dep factory on top of these. A CI test (`test_role_covers.test_every_action_has_a_required_role`) asserts full coverage.

## Cloudflare ingress gate

`CloudflareIngressMiddleware` is the **outermost security gate** in the ASGI chain. It runs before `AuthMiddleware`, rate-limiting, and all route handlers. (`CSPMiddleware` is registered strictly outermost so its header lands on Cloudflare's 403s, but it's a header injector, not a gate — see `core/webserver`.)

- **Header:** `X-Yaaos-cf-Ingress` — Cloudflare injects this via a Transform Rule using the shared secret set in `YAAOS_CLOUDFLARE_INGRESS_SECRET` (Fly secret). Direct `.fly.dev` hits and Fly IP hits do not carry it → 403. The `CF-*` prefix is reserved by Cloudflare for its own managed headers; the `X-Yaaos-cf-*` form follows the `X-Yaaos-*` convention used by `X-Yaaos-Org-Slug` and `X-Yaaos-Audience` while the `cf` segment marks it as Cloudflare-injected.
- **Exempt path:** `/api/health` passes unconditionally — Fly's internal machine checker bypasses Cloudflare and must still reach the health endpoint.
- **No-op when empty:** when `YAAOS_CLOUDFLARE_INGRESS_SECRET` is unset or empty (dev/test/e2e), the middleware is a transparent pass-through. Local stacks and Playwright suites are unaffected.
- **Constant-time compare:** `hmac.compare_digest` guards against timing attacks.
- **Registration:** last `app.add_middleware(...)` call in `app_factory._install_middleware` — FastAPI reverses registration order, so last-registered = outermost.

## Why / invariants

**Route taxonomy** — every `/api/*` path is exactly one of three categories:

| `RouteSecurity` | Session? | `X-Yaaos-Org-Slug`? | Role check? |
|---|---|---|---|
| `PUBLIC` | no | no | no |
| `USER_SCOPED` | yes (route dep) | no | no |
| `ORG_SCOPED` | yes (route dep) | yes¹ | yes (`require(action)`) |

¹ `/api/sse/*` routes accept the org slug in the `?org=` query param instead, because the browser `EventSource` API cannot set headers (`org_slug_in_query_allowed`). The slug runs through the same membership check, so it is not a bypass.

`classify_route(path, method)` is the single source of truth. Method-exact > exact > prefix. Unclassified `/api/*` falls through as `PUBLIC`.

**Middleware order on `/api/*`:**
0. `CSPMiddleware` (outermost) — injects `Content-Security-Policy` or `…-Report-Only` on every response. Owned by `core/webserver`, not `core/auth`. Outside the security gates so its header lands on Cloudflare's 403s too.
1. `CloudflareIngressMiddleware` — 403 unless `X-Yaaos-cf-Ingress` matches; exempt `/api/health`; no-op when secret unset.
2. Reset all identity contextvars (ASGI may reuse the task).
3. Classify route. `ORG_SCOPED` without `X-Yaaos-Org-Slug` (nor `?org=` on `/api/sse/*`) → 400 immediately. `USER_SCOPED` and `ORG_SCOPED` mutations → CSRF double-submit check.
4. Post-response guard: if response is 2xx and `route_security_resolved` is still `None`, substitute 500 + log. Forgetting a security dep crashes, not leaks.
5. OTel spans created during the request carry `yaaos.org_id`, `yaaos.user_id`, `yaaos.actor_kind` via `YaaosDimensionsSpanProcessor` (stamped on every span at creation, not inline after the request).

**`POST /api/orgs` is `USER_SCOPED`, not `ORG_SCOPED`** — org-create must work before the SPA has selected an org. Lives in `USER_SCOPED_METHOD_EXACT`.

**`org_context(org_id, actor_kind, actor_id)`** — async context manager for background jobs. Sets the four identity vars + `route_security_resolved = "background"` + OTel attrs on the current span + structlog `bind_contextvars`. Resets on exit. Does NOT set `workflow_execution_id_var` or `command_id_var` — those are set by workflow task bodies.

**`workflow_execution_id_var` + `command_id_var`** — workflow-scope contextvars. None outside an active workflow task body. Set/reset by `core/workflow` task bodies (`start_step`, `handle_agent_event`, `route_workflow`) so every span and log record in scope carries `yaaos.workflow_id` and `yaaos.command_id` via the span processor and `_YaaosLogDimsFilter`. Not set in background work or HTTP requests.

**Standard dims on every span** — `YaaosDimensionsSpanProcessor` (in `core/observability`) reads all six identity/workflow contextvars on `on_start` and stamps `yaaos.org_id`, `yaaos.user_id`, `yaaos.actor_kind`, `yaaos.workflow_id`, `yaaos.command_id` on every new span. Dims are only stamped when the var is set — background spans carry org+actor but no `user_id`; non-workflow spans carry no `workflow_id`/`command_id`. The middleware's previous inline `set_attribute` calls are removed; the processor makes dims universal without per-span code.

**Pure-ASGI, not `BaseHTTPMiddleware`** — contextvars set inside the route handler propagate back to the middleware on the way out. `BaseHTTPMiddleware` runs downstream in a separate task, making those mutations invisible.

**`Action` enum** — single grep-able catalogue of every distinct privilege check. Adding an `ORG_SCOPED` endpoint requires: (1) add to `Action`, (2) map to its minimum `Role` in `core/auth/role_policy._REQUIRED_ROLE`, (3) `Depends(require(Action.X))` on the route. A CI test asserts every `Action` member has a `_REQUIRED_ROLE` entry.

## Gotchas

- **Rate limiting** — `slowapi` wraps the app in `prod`/`dev`; skipped in `test`. Per-IP on `/api/auth/*` (30/min); per-user on all mutating `/api/*` (120/min, keyed by session cookie, falls back to IP for anonymous). Exceeded → 429 `Retry-After`.
- **Prod secret check** — `create_app()` calls `_check_required_prod_secrets()`. Missing/stub `YAAOS_OAUTH_STATE_SECRET`, `YAAOS_INVITATION_TOKEN_SECRET`, `YAAOS_OAUTH_GITHUB_CLIENT_ID`, `YAAOS_OAUTH_GITHUB_CLIENT_SECRET`, `YAAOS_TOTP_MASTER_KEY`, or `YAAOS_CLOUDFLARE_INGRESS_SECRET` in `prod` → `RuntimeError` at boot. Empty `YAAOS_CLOUDFLARE_INGRESS_SECRET` would otherwise turn `CloudflareIngressMiddleware` into a transparent pass-through and silently disable the outermost defense layer — fail-secure boot prevents that. See [`docs/runbooks/secret-rotation.md`](../../../docs/runbooks/secret-rotation.md).

