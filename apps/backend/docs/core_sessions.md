# core/sessions

> FastAPI dependency factories that wire [`core/auth`](core_auth.md) middleware into identity + org lookups — no `domain/*` imports.

## Scope

- Owns: `require(action)`, `public_route` dependency factories; `/api/auth/*` HTTP routes.
- Does NOT own: session rows (those are in [`core/identity`](core_identity.md)), the middleware/`Action` enum + per-action role map (those are in [`core/auth`](core_auth.md)), org/membership tables (those are in [`core/tenancy`](core_tenancy.md)), the SSO discover route (moved to [`domain/orgs`](domain_orgs.md) at `/api/sso/discover`), or broken-credential data (that's [`domain/integrations`](domain_integrations.md) at `GET /api/integrations/broken-summary`).
- Pure core — no `domain/*` import at any layer. All org/membership resolution goes through [`core/tenancy`](core_tenancy.md).

## Why / invariants

**`require(action)` resolves via `core/tenancy`** — calls `resolve_auth_org(session, user_id, slug)` which returns `AuthOrg` (role, SSO flags, session timeout override) in a single lookup. No `domain/*` import; no `sso_configs` join at request time — SSO gate reads the denormalized `orgs.sso_enabled` column via `AuthOrg`. The dep returns that same `AuthOrg` — it carries only what `require()` actually resolved (org_id, slug, role, SSO flags), so a handler that captures the return value never gets half-populated fields.

**Per-action role map** — the `Action → Role` source of truth is `core/auth/role_policy._REQUIRED_ROLE`, read here via `required_role_for(action)`. See [`core_auth.md`](core_auth.md). Current mappings: Builder for read/self-update actions; Admin for invite/remove/role-change; Owner for SSO + GitHub App link.

**Error shapes (security-relevant):**
- No session → 401 `unauthenticated`.
- Idle window exceeded → 401 `session_idle_expired` + audit row `user / logout / payload.kind=idle_timeout`.
- Org not found OR no membership → 404 `org_not_found`. Never leak org existence.
- Role insufficient → 403 `insufficient_role`.

**`AuthFailure` response** — every 401 from a dead/missing session emits two `Set-Cookie: <name>=; Max-Age=0` headers clearing `yaaos_session` + `yaaos_csrf`. Prevents the "401 → cookie still attached → 401" cascade. The SPA's `apps/web/src/core/api/auth-failure.ts` reads `{"error": "<reason>"}` to pick a banner and hard-navigates to `/login?reason=...&next=<path>`.

**OAuth callback flow** (security-relevant order):
1. Verify `state` signature + 10-minute TTL via `itsdangerous.URLSafeTimedSerializer` (salt `yaaos-oauth-state`). Expired → 400; tampered → 400.
2. Exchange code; `ProviderError` → 502.
3. Reject unverified email → 403 `email_not_verified`.
4. Run [`login_via_oauth`](core_identity.md#login-orchestrator). TOTP step-up when user has verified secret (signed `yaaos_totp_challenge` cookie).
5. On success: `sessions.create`, set `yaaos_session` (HttpOnly, SameSite=Lax) + `yaaos_csrf` (non-HttpOnly) cookies, 303 to signed `next`.
6. Open-redirect defeated by `_safe_next`: only same-origin absolute paths honored.

**State vs TOTP-challenge cookie** use different `itsdangerous` salts (`yaaos-oauth-state` vs `yaaos-totp-challenge`) so a login state can't be replayed at the step-up endpoint.

**`GET /api/auth/me` response shape** — `{ user, memberships }`. Each membership entry carries `org_id`, `slug`, `role`, `handle`, `display_name`. No `broken_integrations` field — that data is served by `GET /api/integrations/broken-summary` (see [`domain_integrations.md`](domain_integrations.md)), keeping integrations concerns out of the core auth surface.

