# domain/identity

> Users, emails, OAuth identities, TOTP secrets, opaque sessions, and the GitHub-installation-to-org binding.

## Purpose

Owns who the human (or workspace principal) is, what identities they've linked, and the opaque server-side session backing their browser cookie. The login flow itself (provider handshake, hard-reject, account-linking challenge) ships in Phase 4; this Phase 1 skeleton lands the tables and the repository layer that later phases build on.

## Public interface

Exported from `app/domain/identity/__init__.py`:

- Types — `User`, `UserEmail`, `OAuthIdentity`, `Session`.
- Rows — `UserRow`, `UserEmailRow`, `OAuthIdentityRow`, `UserTotpSecretRow`, `SessionRow`, `GithubInstallationRow`.
- Exceptions — `UserNotFoundError`, `EmailAlreadyLinkedError`, `LinkChallengeRequiredError`, `HardRejectError`, `SessionNotFoundError`, `TotpError`.

HTTP routes (`/api/auth/*`, `/api/account/*`) land in Phase 4-7; not in scope for the Phase 1 skeleton.

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

Phase 1 ships only the data layer; login, session, and 2FA flows ship in subsequent phases. See `core/auth` and the phase-specific docs.

## Data owned

- `users`, `user_emails`, `oauth_identities`, `user_totp_secrets`, `sessions`, `github_installations`.
- Partial unique index `uq_user_emails_email_active` on `lower(email) WHERE verified_at IS NOT NULL` — verified emails are globally unique; deactivation frees them lazily.

## How it's tested

- `test/test_repository.py` — repository helpers against real Postgres via the transactional-rollback fixture; covers user + email + oauth-identity insert, case-insensitive email lookup, verified-email gate, session insert/lookup, TOTP upsert reset semantics, installation upsert + lookup.
- Login flow and middleware integration tests ship with their phases (`core/auth`, Phase 4).
