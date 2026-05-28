# core/sessions

> FastAPI dependency factories that wire the [`core/auth`](core_auth.md) middleware into identity + orgs lookups.

## Scope

- Owns: `require(action)`, `require_session`, `public_route` dependency factories; `/api/auth/*` HTTP routes; `_REQUIRED_ROLE` registry.
- Does NOT own: session rows (those are in [`core/identity`](core_identity.md)) or the middleware/`Action` enum (those are in [`core/auth`](core_auth.md)).
- Why separate from `core/auth`: the dep factories need both `core/identity` and `domain/orgs`; `core/auth` stays free of domain reads.

## Why / invariants

**`_REQUIRED_ROLE` registry** — single source of truth mapping `Action → Role`. A CI test asserts every `Action` member has an entry. See `app/core/sessions/dependencies.py`. Current mappings: Builder for read/self-update actions; Admin for invite/remove/role-change; Owner for SSO + GitHub App link.

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

