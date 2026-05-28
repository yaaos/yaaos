# core/identity

> Users, emails, OAuth identities, TOTP secrets, opaque sessions.

## Scope

- Owns: `users`, `user_emails`, `oauth_identities`, `user_totp_secrets`, `sessions` tables + all read/write ops; login orchestrator; Provider registry; periodic cleanup scheduler.
- Does NOT own: `/api/auth/*` HTTP routes (those are in [`core/sessions`](core_sessions.md)) or `/api/user/*` (those are in `user_web.py`, `USER_SCOPED`).
- Emits: `CreatedSession` (raw token + CSRF token) to the callback handler.

## Why / invariants

**Login orchestrator** (`login_via_oauth`) — only place identity-binding rules live. Resolution order:
1. `(provider, external_subject)` matches existing `OAuthIdentity` → load user; refresh `github_username` on GitHub.
2. `primary_email` matches a verified `UserEmail` but identity doesn't exist → **auto-link**: insert identity row on existing user.
3. No match → **create** user + email + identity. Pending invitation for the email is accepted in the same operation.

Unverified emails never reach the orchestrator — the callback handler enforces `email_verified == true` before calling it.

**`users.github_username`** — denorm for VCS attribution; written on every successful GitHub sign-in. Load-bearing; never drop.

**Session security invariants:**
- PK is the sha256 hex of the raw token; raw tokens never stored in DB.
- Per-session CSRF token in `yaaos_csrf` (non-HttpOnly) echoed in `X-CSRF-Token` on mutations — double-submit pattern.
- `sessions.rotate(old_raw)` on role change / invite-accept / SSO satisfaction: deletes old row and mints new one atomically.
- "Sign out everywhere" calls `sessions.revoke_all_for_user(user_id)`. Role revocation triggers this automatically.
- `sso_satisfied_for_org_id` + timestamp encode 8-hour SSO TTL per org.

**TOTP secret** — at most one per user. Base32 seed encrypted via [`core/secrets`](core_secrets.md); `verified_at` set only after the user proves possession.

**Periodic cleanup** — `scheduler.run_cleanup_loop()` spawned in FastAPI lifespan every `YAAOS_AUTH_CLEANUP_INTERVAL_SECONDS` (default 1h): purges expired sessions, expired uninvited invitations, unverified TOTP secrets older than 24h, and audit entries older than `AUDIT_LOG_RETENTION` (15d).

**Provider registry** — `register_provider(p)` overwrites on re-register (plugins may import multiple times in tests). Plugins: [`plugins/github`](plugins_github.md), [`plugins/oauth_test`](plugins_oauth_test.md).

## Gotchas

- `_*_for_tests` helpers are production exports — cross-module test callers need them. Not used by production code.
- Partial unique index `uq_user_emails_email_active` on `lower(email) WHERE verified_at IS NOT NULL` — verified emails are globally unique; deactivation frees them lazily.

