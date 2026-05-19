# domain/identity

> Users, emails, OAuth identities, TOTP secrets, opaque sessions, and the GitHub-installation-to-org binding.

## Purpose

Owns who the human (or workspace principal) is, what identities they've linked, and the opaque server-side session backing their browser cookie. The login flow itself (provider handshake, hard-reject, account-linking challenge) ships in Phase 4; this Phase 1 skeleton lands the tables and the repository layer that later phases build on.

## Public interface

Exported from `app/domain/identity/__init__.py`:

- Types — `User`, `UserEmail`, `OAuthIdentity`, `Session`.
- Rows — `UserRow`, `UserEmailRow`, `OAuthIdentityRow`, `UserTotpSecretRow`, `SessionRow`, `GithubInstallationRow`.
- Exceptions — `UserNotFoundError`, `EmailAlreadyLinkedError`, `LinkChallengeRequiredError`, `HardRejectError`, `SessionNotFoundError`, `TotpError`.
- Sessions namespace — `sessions.create`, `sessions.lookup`, `sessions.touch`, `sessions.revoke`, `sessions.revoke_all_for_user`, `sessions.rotate`, `sessions.mark_sso_satisfied`, `sessions.is_sso_satisfied`, `sessions.cleanup_expired`, `sessions.CreatedSession`, `sessions.SSO_TTL`.

HTTP routes (`/api/auth/*`, `/api/account/*`) land in Phase 4-7; the Phase 3 skeleton wires an `on_startup` hook that spawns the periodic cleanup loop.

## Module architecture

### Entities

- **User** — UUID PK. Never keyed by email. Soft-deleted via `deactivated_at`.
- **UserEmail** — N per user. `is_primary` marks the canonical address; `verified_at` distinguishes provider-confirmed addresses from claimed-but-unverified ones. Sign-in matches any verified email.
- **OAuthIdentity** — N per user. `(provider, external_subject)` is globally unique. Account-linking creates additional rows for the same `user_id`.
- **Session** — one per active browser. PK is the sha256 hex of the raw token; raw tokens never live in the DB. Carries `user_id` xor `workspace_id`, the per-session CSRF token, and optional `sso_satisfied_for_org_id` + timestamp for the 8-hour SSO TTL.
- **UserTotpSecret** — at most one per user. Fernet-encrypted base32 seed; `verified_at` set only after the user proves possession.
- **GithubInstallation** — links a GitHub App installation id to the org that owns it. Inserted by the install callback (Phase 10).

### Key value objects

- `Session.csrf_token` — plaintext; the SPA echoes it in `X-CSRF-Token` on mutations. Double-submit pattern; pairs with the `HttpOnly` session cookie.
- `OAuthIdentity.external_subject` — the provider's stable user id (numeric for GitHub, `sub` claim for OIDC).

### Core user flows

Login, link-confirm, 2FA flows ship in Phases 4/11 with the provider plugins. The session lifecycle from Phase 3 onwards:

1. Login (Phase 4) calls `sessions.create(user_id=…)` after the provider callback verifies the identity. The returned `CreatedSession` carries the raw token (set on the `yaaos_session` HttpOnly cookie) and the per-session CSRF token (set on the `yaaos_csrf` non-HttpOnly cookie).
2. Subsequent requests come in with the session cookie. `domain/auth.require()` resolves it to a user via `sessions.lookup`, then loads the membership for the `X-Org-Slug` header.
3. Mutating requests must include the matching CSRF token in the `X-CSRF-Token` header — the middleware enforces the double-submit check before any handler runs.
4. Role change, invite-accept, or SSO satisfaction triggers `sessions.rotate(old_raw)` — the old row is deleted and a new one minted atomically.
5. "Sign out everywhere" calls `sessions.revoke_all_for_user(user_id)`. Role revocation does the same automatically (Phase 6).

### Periodic cleanup

`scheduler.run_cleanup_loop()` is spawned in the FastAPI lifespan via the module's `on_startup` hook. Every `YAAOS_AUTH_CLEANUP_INTERVAL_SECONDS` (default 1h) it purges:
- expired sessions (`expires_at < now`),
- expired un-accepted invitations,
- unverified TOTP secrets older than 24h.

## Data owned

- `users`, `user_emails`, `oauth_identities`, `user_totp_secrets`, `sessions`, `github_installations`.
- Partial unique index `uq_user_emails_email_active` on `lower(email) WHERE verified_at IS NOT NULL` — verified emails are globally unique; deactivation frees them lazily.

## How it's tested

- `test/test_repository.py` — repository helpers against real Postgres via the transactional-rollback fixture; covers user + email + oauth-identity insert, case-insensitive email lookup, verified-email gate, session insert/lookup, TOTP upsert reset semantics, installation upsert + lookup.
- Login flow and middleware integration tests ship with their phases (`core/auth`, Phase 4).
