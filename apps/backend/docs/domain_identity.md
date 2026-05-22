# domain/identity

> Users, emails, OAuth identities, TOTP secrets, opaque sessions, and the GitHub-installation-to-org binding.

## Purpose

Owns who the human (or workspace principal) is, what identities they've linked, the opaque server-side session backing their browser cookie, the `Provider` Protocol that OAuth plugins implement, and the login orchestrator that applies the matching / linking / hard-reject ladder to a normalized profile.

## Public interface

Exported from `app/domain/identity/__init__.py`:

- Types — `User`, `UserEmail`, `OAuthIdentity`, `Session`, `LoginResult`.
- Rows — `UserRow`, `UserEmailRow`, `OAuthIdentityRow`, `UserTotpSecretRow`, `SessionRow`, `GithubInstallationRow`.
- Exceptions — `UserNotFoundError`, `EmailAlreadyLinkedError`, `LinkChallengeRequiredError`, `HardRejectError`, `SessionNotFoundError`, `TotpError`.
- Login orchestrator — `login_via_oauth(db, provider_id, profile)`, `complete_oauth_link(db, user_id, provider_id, external_subject)`.
- Provider Protocol — `providers.Provider`, `providers.ProviderProfile`, `providers.ProviderError`, `providers.register_provider`, `providers.get_provider`, `providers.list_providers`.
- Sessions namespace — `sessions.create`, `sessions.lookup`, `sessions.touch`, `sessions.revoke`, `sessions.revoke_all_for_user`, `sessions.rotate`, `sessions.mark_sso_satisfied`, `sessions.is_sso_satisfied`, `sessions.cleanup_expired`, `sessions.CreatedSession`, `sessions.SSO_TTL`.

HTTP routes for login + callback + logout live in [`domain/sessions`](domain_sessions.md) (`/api/auth/*`). Account-management routes live under `/api/account/*` in `account_web.py`: emails (M02), plus `GET/PATCH /api/account/me` for the user profile + `GET /api/account/github/verify[/callback]` for the verify-only GitHub OAuth flow. The Phase 3 skeleton wires an `on_startup` hook that spawns the periodic cleanup loop.

### Verify-only GitHub flow

A user already authenticated to yaaos can prove ownership of a GitHub account *without* creating an `oauth_identities` row or issuing a session. `GET /api/account/github/verify` mints a `(user_id)`-bound signed state and 303s to GitHub's authorization URL via the same `Provider` interface as login. The callback at `GET /api/account/github/verify/callback` verifies the signature, cross-checks state-user against session-user, calls `Provider.exchange_code()`, and writes `profile.provider_login` into `users.github_username` via `repository.set_user_github_username`. No row is added to `oauth_identities`; no new session cookie is set. The signed-state salt is `yaaos-github-verify` (10-minute TTL), separate from the login-flow salt so a login state can't be replayed at the verify callback.

`ProviderProfile.provider_login` is the field providers populate when they have a username/handle to surface (the GitHub plugin sets it from the `/user` response's `login`). Generic OIDC providers may leave it `None`.

## Module architecture

### Entities

- **User** — UUID PK. Never keyed by email. Soft-deleted via `deactivated_at`. Carries `github_username` (verified GitHub login): written by the OAuth-github callback on every successful login and by the verify-only flow described below.
- **UserEmail** — N per user. `is_primary` marks the canonical address; `verified_at` distinguishes provider-confirmed addresses from claimed-but-unverified ones. Sign-in matches any verified email.
- **OAuthIdentity** — N per user. `(provider, external_subject)` is globally unique. Account-linking creates additional rows for the same `user_id`.
- **Session** — one per active browser. PK is the sha256 hex of the raw token; raw tokens never live in the DB. Carries `user_id` xor `workspace_id`, the per-session CSRF token, and optional `sso_satisfied_for_org_id` + timestamp for the 8-hour SSO TTL.
- **UserTotpSecret** — at most one per user. Base32 seed encrypted via [core/secrets](core_secrets.md); `verified_at` set only after the user proves possession.
- **GithubInstallation** — links a GitHub App installation id to the org that owns it. Inserted by the install callback (Phase 10).

### Key value objects

- `Session.csrf_token` — plaintext; the SPA echoes it in `X-CSRF-Token` on mutations. Double-submit pattern; pairs with the `HttpOnly` session cookie.
- `OAuthIdentity.external_subject` — the provider's stable user id (numeric for GitHub, `sub` claim for OIDC).

### Login orchestrator

`login_via_oauth(db, provider_id, profile)` is the only place the matching / linking / hard-reject rules live. Provider plugins produce a normalized `ProviderProfile`; the orchestrator decides what happens next, in this order:

1. `(provider, external_subject)` resolves to an existing `OAuthIdentity` → return the existing user.
2. `primary_email` resolves to an existing verified `UserEmail` but `(provider, external_subject)` does not → raise `LinkChallengeRequiredError`. The HTTP callback handler in `domain/sessions` sets a signed `yaaos_link_pending` cookie and returns 409; the user signs in via an already-linked provider, and that second callback attaches the new identity via `complete_oauth_link`.
3. No identity, no email match, but a not-yet-accepted not-yet-expired invitation exists for the email → create the user + verified email + oauth identity, accept the invitation, insert the membership. Phase 6's invite/accept service supersedes the minimal accept path used here.
4. Otherwise → raise `HardRejectError`. The callback handler returns 403 `ask_for_invite`.

Unverified emails never reach the orchestrator — the callback handler enforces `email_verified == true` before calling it.

### Session lifecycle

1. Login calls `sessions.create(user_id=…)` after the orchestrator returns. The returned `CreatedSession` carries the raw token (set on the `yaaos_session` HttpOnly cookie) and the per-session CSRF token (set on the `yaaos_csrf` non-HttpOnly cookie).
2. Subsequent requests come in with the session cookie. `domain/sessions.require()` resolves it to a user via `sessions.lookup`, then loads the membership for the `X-Org-Slug` header.
3. Mutating requests must include the matching CSRF token in the `X-CSRF-Token` header — the middleware enforces the double-submit check before any handler runs.
4. Role change, invite-accept, or SSO satisfaction triggers `sessions.rotate(old_raw)` — the old row is deleted and a new one minted atomically.
5. "Sign out everywhere" calls `sessions.revoke_all_for_user(user_id)`. Role revocation does the same automatically (Phase 6).

### Provider registry

`providers.register_provider(p)` keys by `p.provider_id` and overwrites on re-register (plugin bootstraps may run multiple times in tests). The HTTP layer reads by id via `get_provider` and enumerates via `list_providers` for the "which providers can I sign in with" endpoint. Plugins: [`plugins/github`](plugins_github.md) (M04 collapsed the M02 `plugins/oauth_github` here), [`plugins/oauth_test`](plugins_oauth_test.md).

### Periodic cleanup

`scheduler.run_cleanup_loop()` is spawned in the FastAPI lifespan via the module's `on_startup` hook. Every `YAAOS_AUTH_CLEANUP_INTERVAL_SECONDS` (default 1h) it purges:
- expired sessions (`expires_at < now`),
- expired un-accepted invitations,
- unverified TOTP secrets older than 24h.

## Data owned

- `users`, `user_emails`, `oauth_identities`, `user_totp_secrets`, `sessions`, `github_installations`.
- Partial unique index `uq_user_emails_email_active` on `lower(email) WHERE verified_at IS NOT NULL` — verified emails are globally unique; deactivation frees them lazily.

## How it's tested

- `test/test_repository.py` — repository helpers against real Postgres via the transactional-rollback fixture.
- `test/test_sessions.py` — lifecycle: create, rotate, revoke, revoke-all, expired-lookup, mark-sso-satisfied, TTL.
- `test/test_login_orchestrator.py` — every branch of `login_via_oauth`: existing identity, link-challenge, hard-reject, pending-invitation-creates-user, expired-invitation-still-rejects, `complete_oauth_link`.
- Endpoint coverage lives in [`domain/sessions`](domain_sessions.md): `test/test_oauth_endpoints.py`.
