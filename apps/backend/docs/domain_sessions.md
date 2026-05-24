# domain/sessions

> FastAPI dependency factories that wire the [`core/auth`](core_auth.md) middleware into identity + orgs lookups.

## Purpose

`core/auth` ships the pure middleware, contextvars, and the `Action` enum. The actual session-cookie → user lookup and slug → org → membership → role check happen here because they need both `domain/identity` and `domain/orgs` — dependencies that `core/auth` can't take (core can't depend on domain). The module also owns the `/api/auth/*` HTTP surface — login redirect, callback, logout, providers list.

## Public interface

Exported from `app/domain/sessions/__init__.py`:

- `require(action)` — dependency factory. Resolves `X-Org-Slug` → org → membership → role check. Sets the identity contextvars + `route_security_resolved = "membership"`. Returns the `Membership` so handlers that want it can `Depends(require(...))` directly.
- `public_route` — dependency for routes that intentionally have no auth requirement. Sets `route_security_resolved = "public"`. Using this where a role check should live is the bug the post-response guard catches.
- `current_actor()` — reads `user_id_var` and returns an `Actor.user(user_id=…)` for audit-log writes. Raises if no session resolved.
- `required_role_for(action)` — lookup the minimum role for an action.

HTTP routes (registered side-effect via `web.py`):

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/auth/login?provider=<id>&next=<path>` | 302 to the provider's authorization URL; signs `next` into the state. |
| GET | `/api/auth/callback/{provider}?code=...&state=...` | Verify state, exchange code, run [`login_via_oauth`](domain_identity.md#login-orchestrator), issue session, 303 to `next`. |
| POST | `/api/auth/logout` | Revoke the current session; clear cookies. |
| GET | `/api/auth/providers` | Enumerate registered provider ids (test stub appears only under `YAAOS_ENV=test`). |

## Module architecture

### Session resolution

`_current_session_user_id` reads the `yaaos_session` cookie, sha256-hashes it, looks up the row, validates expiry, and sets `user_id_var`. None is returned for missing/expired/unknown sessions; the caller (`require`) raises 401.

### Error shape

- No session → 401 `unauthenticated`.
- No `X-Org-Slug` → middleware already 400'd; this dep won't reach the check.
- Org doesn't exist OR caller has no membership in it → 404 `org_not_found`. Mask existence — never leak "the org is real but you can't see it."
- Role insufficient → 403 `insufficient_role`.

### `_REQUIRED_ROLE` registry

Single source of truth mapping `Action → Role`. Per-endpoint overrides are explicit: write `Depends(require(Action.X))` whose row in this map is the role you want enforced.

| Action | Required role |
|---|---|
| `IDENTITY_READ_SELF` | Builder |
| `ORG_READ` | Builder |
| `MEMBERS_READ` | Builder |
| `AUDIT_READ` | Admin |
| `ACCOUNT_UPDATE_SELF` | Builder |
| `MEMBERS_INVITE` | Admin |
| `MEMBERS_REMOVE` | Admin |
| `MEMBERS_CHANGE_ROLE` | Admin |
| `SSO_CONFIGURE` | Owner |
| `GITHUB_APP_LINK` | Owner |
| `REVIEW_TRIGGER` | Builder |
| `TICKETS_READ` | Builder |
| `LESSONS_READ` | Builder |
| `LESSONS_WRITE` | Builder |
| `REVIEWER_READ` | Builder |
| `REVIEWER_WRITE` | Builder |

A coverage test asserts every `Action` member has a row here.

### OAuth callback flow

The callback handler is the only place that translates a provider's normalized profile into a session. Order:

1. Look up the provider by path parameter; unknown → 404 `unknown_provider`.
2. Verify the `state` signature + 10-minute TTL via `itsdangerous.URLSafeTimedSerializer` (salt `yaaos-oauth-state`). Expired → 400 `state_expired`; tampered → 400 `state_invalid`.
3. `provider.exchange_code(...)` — `ProviderError` → 502 `provider_error`.
4. Reject unverified email → 403 `email_not_verified`.
5. Run [`login_via_oauth`](domain_identity.md#login-orchestrator) — auto-link or self-signup as needed. The orchestrator never raises rejections; the only deferral is TOTP step-up when the user has a verified TOTP secret and the provider didn't satisfy MFA (signed `yaaos_totp_challenge` cookie, JSON `{step_up: "totp_required"}`).
6. On success — `sessions.create(user_id=…)`, set `yaaos_session` (HttpOnly, SameSite=Lax) + `yaaos_csrf` (non-HttpOnly) cookies, 303-redirect to the signed `next` path. Open-redirect defeated by `_safe_next`: only same-origin absolute paths are honored.

### State signing

`yaaos-oauth-state` uses `URLSafeTimedSerializer` with `yaaos_oauth_state_secret` and a per-flow salt. TTL is 10 minutes. The TOTP-challenge cookie uses the salt `yaaos-totp-challenge` so a login state can't be replayed at the step-up endpoint.

## Data owned

None — reads `sessions`, `orgs`, `memberships` via the identity/orgs repositories.

## How it's tested

- `test/test_middleware.py` — middleware header check, dep resolution, role check, contextvar propagation. See [`core/auth`](core_auth.md) for the test inventory.
- `test/test_oauth_endpoints.py` — ASGI-driven coverage of `/api/auth/login` and `/api/auth/callback/test` through the `oauth_test` stub: login redirect, unknown-provider 404, existing-identity issues session, unknown-email auto-creates a user, email-match auto-links a new identity, invitation accept creates user, unverified email 403, invalid state 400, logout clears cookies.
