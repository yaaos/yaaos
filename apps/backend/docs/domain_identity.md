# domain/identity

> Users, emails, OAuth identities, TOTP secrets, opaque sessions.

## Purpose

Owns who the human (or workspace principal) is, what identities they've linked, the opaque server-side session backing their browser cookie, the `Provider` Protocol that OAuth plugins implement, and the login orchestrator that turns a normalized profile into an existing-or-fresh `User`.

## Public interface

Exported from `app/domain/identity/__init__.py`:

- Types — `User`, `UserEmail`, `OAuthIdentity`, `Session`, `LoginResult`.
- Rows — `UserRow`, `UserEmailRow`, `OAuthIdentityRow`, `UserTotpSecretRow`, `SessionRow`.
- Exceptions — `UserNotFoundError`, `EmailAlreadyLinkedError`, `SessionNotFoundError`, `TotpError`.
- Login orchestrator — `login_via_oauth(db, provider_id, profile)`.
- Provider Protocol — `providers.Provider`, `providers.ProviderProfile`, `providers.ProviderError`, `providers.register_provider`, `providers.get_provider`, `providers.list_providers`.
- Sessions namespace — `sessions.create`, `sessions.lookup`, `sessions.touch`, `sessions.revoke`, `sessions.revoke_all_for_user`, `sessions.rotate`, `sessions.mark_sso_satisfied`, `sessions.is_sso_satisfied`, `sessions.cleanup_expired`, `sessions.CreatedSession`, `sessions.SSO_TTL`.

HTTP routes for login + callback + logout live in [`domain/sessions`](domain_sessions.md) (`/api/auth/*`). User-management routes live under `/api/user/*` in `user_web.py`: emails, plus `GET/PATCH /api/user/me` for the user profile. All `/api/user/*` routes are `RouteSecurity.USER_SCOPED` — session via `_require_user()` (which delegates to `domain/sessions.require_session`), no `X-Org-Slug` required. An `on_startup` hook spawns the periodic cleanup loop.

`users.github_username` is a denorm written by the login orchestrator on every GitHub sign-in (see Login orchestrator below). The User > Details page only displays it and offers a Clear button (`clear_github_username: true` on `PATCH /api/user/me`). Re-binding to a different GitHub account is "sign in with GitHub again."

`ProviderProfile.provider_login` is the field providers populate when they have a username/handle to surface (the GitHub plugin sets it from the `/user` response's `login`). Generic OIDC providers may leave it `None`.

## Module architecture

### Entities

- **User** — UUID PK. Never keyed by email. Soft-deleted via `deactivated_at`. Carries `github_username` (verified GitHub login) — denorm for VCS attribution; written by the login orchestrator on every successful GitHub sign-in.
- **UserEmail** — N per user. `is_primary` marks the canonical address; `verified_at` distinguishes provider-confirmed addresses from claimed-but-unverified ones. Sign-in matches any verified email.
- **OAuthIdentity** — N per user. `(provider, external_subject)` is globally unique. Auto-link on first sign-in attaches a second identity to the same `user_id`.
- **Session** — one per active browser. PK is the sha256 hex of the raw token; raw tokens never live in the DB. Carries `user_id` xor `workspace_id`, the per-session CSRF token, and optional `sso_satisfied_for_org_id` + timestamp for the 8-hour SSO TTL.
- **UserTotpSecret** — at most one per user. Base32 seed encrypted via [core/secrets](core_secrets.md); `verified_at` set only after the user proves possession.

### Key value objects

- `Session.csrf_token` — plaintext; the SPA echoes it in `X-CSRF-Token` on mutations. Double-submit pattern; pairs with the `HttpOnly` session cookie.
- `OAuthIdentity.external_subject` — the provider's stable user id (numeric for GitHub, `sub` claim for OIDC).

### Login orchestrator

`login_via_oauth(db, provider_id, profile)` is the only place identity-binding rules live. Provider plugins produce a normalized `ProviderProfile`; the orchestrator decides what happens next, in this order:

1. `(provider, external_subject)` resolves to an existing `OAuthIdentity` → load that user, refresh `users.github_username` from `profile.provider_login` if the provider is `github`.
2. `primary_email` resolves to an existing verified `UserEmail` but `(provider, external_subject)` does not → **auto-link**: insert an `oauth_identities` row attaching the new provider identity to the matched user. Refresh `github_username` on the GitHub branch.
3. No identity, no email match → **create a new user** with the verified email + identity row. If a not-yet-accepted, not-yet-expired invitation exists for the email, accept it as part of creation and insert the membership.

Unverified emails never reach the orchestrator — the callback handler enforces `email_verified == true` before calling it.

### Session lifecycle

1. Login calls `sessions.create(user_id=…)` after the orchestrator returns. The returned `CreatedSession` carries the raw token (set on the `yaaos_session` HttpOnly cookie) and the per-session CSRF token (set on the `yaaos_csrf` non-HttpOnly cookie).
2. Subsequent requests come in with the session cookie. `domain/sessions.require()` resolves it to a user via `sessions.lookup`, then loads the membership for the `X-Org-Slug` header.
3. Mutating requests must include the matching CSRF token in the `X-CSRF-Token` header — the middleware enforces the double-submit check before any handler runs.
4. Role change, invite-accept, or SSO satisfaction triggers `sessions.rotate(old_raw)` — the old row is deleted and a new one minted atomically.
5. "Sign out everywhere" calls `sessions.revoke_all_for_user(user_id)`. Role revocation does the same automatically.

### Provider registry

`providers.register_provider(p)` keys by `p.provider_id` and overwrites on re-register (plugin bootstraps may run multiple times in tests). The HTTP layer reads by id via `get_provider` and enumerates via `list_providers` for the "which providers can I sign in with" endpoint. Plugins: [`plugins/github`](plugins_github.md), [`plugins/oauth_test`](plugins_oauth_test.md).

### Periodic cleanup

`scheduler.run_cleanup_loop()` is spawned in the FastAPI lifespan via the module's `on_startup` hook. Every `YAAOS_AUTH_CLEANUP_INTERVAL_SECONDS` (default 1h) it purges:
- expired sessions (`expires_at < now`),
- expired un-accepted invitations,
- unverified TOTP secrets older than 24h.

## Data owned

- `users`, `user_emails`, `oauth_identities`, `user_totp_secrets`, `sessions`.
- Partial unique index `uq_user_emails_email_active` on `lower(email) WHERE verified_at IS NOT NULL` — verified emails are globally unique; deactivation frees them lazily.

## How it's tested

- `test/test_repository.py` — repository helpers against real Postgres via the transactional-rollback fixture.
- `test/test_sessions.py` — lifecycle: create, rotate, revoke, revoke-all, expired-lookup, mark-sso-satisfied, TTL.
- `test/test_login_orchestrator.py` — the three orchestrator branches: existing-identity, auto-link-by-email, fresh-signup-creates-user (with and without invitation).
- Endpoint coverage lives in [`domain/sessions`](domain_sessions.md): `test/test_oauth_endpoints.py`.
