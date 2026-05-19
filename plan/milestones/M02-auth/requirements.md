# M02 requirements

> Locked spec. Changes require explicit milestone amendment.

## Users

- UUID PK. Never keyed by email.
- Multiple verified emails per user; one marked `primary`. Sign-in matches any verified email.
- `display_name` on `users` (freeform, global). `handle` lives on `memberships` (per-org, used in @mentions).
- Linked external identities are additive rows in `oauth_identities`.
- Soft-delete only (`deactivated_at`). Emails on deactivated rows become reusable **lazily** — freed when next invite needs them, no background sweep.
- A user with zero memberships still exists as a row (re-invitable).
- Removing the last verified email is blocked.

## Orgs

- UUID PK + immutable unique `slug` used in UI URLs.
- All non-user data is `org_id`-scoped.
- Soft-delete only; archived orgs restorable within retention.
- Multi-org from day one. Users may belong to many; roles per-membership.
- No personal/single-user orgs.

## Roles

Three-enum, fixed for POC:

- **Owner** — full control incl. org deletion, billing, SSO config, GitHub App linking. ≥1 always.
- **Admin** — all Owner powers except deleting the org or removing other Owners.
- **Member** — read findings, post replies, trigger reviews, manage own acks. No settings or member-management.

Per-action role minimums declared in code (single action enum); evaluated at every state-changing endpoint and every read endpoint surfacing org-scoped data.

## Auth

- Provider-agnostic OAuth/OIDC framework. GitHub first; SAML SSO per-org.
- **Real GitHub OAuth App used in dev.** Dev OAuth App is already registered; credentials live in `.env`. Callback URL terminates on the single dev origin where SPA + API are both served. Dev + prod each get their own OAuth App.
- Email matching requires `email_verified=true` from provider.
- **Account-linking rule**: first-time login at provider B whose email exists under provider A → block and require the user to sign in via A in the same browser session to confirm link. Inline flow, no email round-trip.
- Hard-reject for un-invited OAuth logins. No self-signup.
- yaaos owns no passwords.
- No dev-stub OAuth provider — real GitHub OAuth used in dev.
- **Test-stub providers** exist for E2E: `plugins/oauth_test` and `plugins/saml_test`, env-gated to `yaaos_env == "test"`. Implement the `Provider` Protocol; Playwright drives login through them without real IdP traffic. Backend unit/integration tests for `oauth_github` itself use `pytest-httpx` to mock GitHub's token/userinfo endpoints.

### 2FA

- Required globally for human users.
- IdP MFA (`amr`/`acr`) accepted as satisfaction.
- **GitHub OAuth treated as MFA-satisfied by trust, not API check.** We do not call GitHub's `/user/2fa` API per-login; we rely on the user/org enforcing GitHub 2FA themselves. Documented assumption.
- TOTP fallback for providers that don't enforce MFA. Encrypted secrets at rest.

### SSO (SAML 2.0)

- Per-org, Owner-configurable. One IdP per org.
- SP-initiated only.
- **Dev-stub IdP** (`plugins/saml_dev`): mirrors `plugins/oauth_dev` pattern. Issues signed assertions for seeded users without an external IdP. Only active in `yaaos_env == "dev"`.
- JIT provisioning opt-in per org; off by default; invitations remain the access gate.
- **Break-glass Owner**: one Owner per org SSO-exempt; must have TOTP enrolled before flag can be set. Bypass = OAuth + TOTP. Every use audit-logged.
- SSO satisfaction tracked per-session per-org; **8-hour TTL** then re-required (independent of session expiry).

### Invitations

- Owner/Admin enters email → signed-token link (`itsdangerous`, 7-day expiry) → recipient signs in via any configured provider → membership created with chosen role.
- Sole access gate.

### Bootstrap (replaces self-signup)

- `apps/backend/bin/bootstrap` interactive script: creates first user + org + Owner membership + OAuth identity. Idempotent.

## Sessions

- Opaque, server-side, revocable. No JWTs.
- 32 random bytes; sha256-hashed in DB.
- Row carries `user_id|workspace_id`, `created_at`, `last_seen_at`, `expires_at`, `ip`, `user_agent`, optional `sso_satisfied_for_org_id`.
- Browser: `HttpOnly; Secure; SameSite=Lax` cookie + double-submit CSRF token on mutating endpoints. `Secure` flag is env-gated off when `yaaos_env == "dev"` so `http://localhost` works.
- Workspace: opaque token issued post AWS-IAM bootstrap; refreshed by re-bootstrap.
- Revoke = row delete. Rotated on login, SSO satisfaction, role change.

## URL & header conventions

- **UI**: `/orgs/{slug}/...` — slug in browser URL bar for shareability + switching.
- **API**: flat `/api/...` (`/api/auth/login`, `/api/findings`, `/api/memberships`, …).
- **Webhooks**: `/webhooks/...` (`/webhooks/github`).
- API requests carry `X-Org-Slug: <slug>` header (except `/api/auth/*` and `/api/health`). Middleware resolves slug → org_id and verifies membership.
- Session is org-agnostic; the header is per-request org context.
- Same-origin SPA + API; web reverse-proxies `/api/*` and `/webhooks/*` to backend.

## Security middleware contract

- **Default-deny on `/api/*`.** Every route must consume `Depends(require(action))` or `Depends(public_route)`.
- Post-response contextvar guard: route that resolved neither → 500 in dev, alarm in prod. Forgetting protection crashes, not leaks.
- Public-route allowlist is code, not config.
- Error shape: 401 unauthenticated, 403 wrong role, 404 unknown-or-forbidden org slug (don't leak existence).
- Session rotation enforced via service interface on login, SSO satisfaction, role change.
- Logout-everywhere = delete-by-user_id; triggered automatically on role revocation / removal, and user-initiated via a "Sign out of all sessions" button in account settings.

## Observability contract

- Every log entry, OTel span, and metric carries `yaaos.org_id` + `yaaos.user_id` (or `yaaos.actor_kind` + `yaaos.actor_id` for non-user actors).
- HTTP middleware sets these from session/membership.
- Background jobs run inside an `org_context(org_id, actor_kind, actor_id=None)` context manager that sets the same fields.

## Audit log

**Extends the existing `core/audit_log` module.** `ActorKind` is broadened **additively**: keep `github_user|agent|system`; add `user|workspace|sso`. Existing `audit_entries` table extended via the M02 migration (no new table). Single helper `core.audit_log.write(...)`; domain services never write directly.

Events captured:

- Login (provider, MFA path), logout (explicit / expiry / forced).
- Provider link / unlink. Account-linking challenge issued / completed.
- SSO config change, exempt-Owner flag toggle, exempt-Owner login.
- Member invited / accepted / removed / role-changed.
- Role-elevated action by Owners/Admins.
- GitHub App installation linked to org.

Retention: 30 days, single constant `AUDIT_LOG_RETENTION` in `core/constants.py`. Daily cleanup job.

## Cross-cutting test requirements

- Every protected endpoint: triplet — unauthenticated 401, wrong-org 404, insufficient-role 403, success 200.
- Pytest fixture auto-generates negative trio from route registry.
- E2E (Playwright): login → org switch → invite → accept → role change → logout-everywhere.

## Library choices (all in scope)

- `authlib` — OAuth/OIDC.
- `python3-saml` — SAML (requires `libxmlsec1` system package; Docker only).
- `itsdangerous` — signed invitation tokens.
- `pyotp` — TOTP.
- `slowapi` — rate limiting on `/api/auth/*` and mutating endpoints.
- Stdlib for session-token hashing (`hashlib.sha256`) and random bytes (`secrets.token_bytes(32)`).

## Secrets inventory

`SESSION_COOKIE_SECRET`, `INVITATION_TOKEN_SECRET`, `TOTP_MASTER_KEY`, `OAUTH_GITHUB_CLIENT_ID`/`SECRET`, per-org `SAML_SP_PRIVATE_KEY` (stored encrypted in DB).

## Explicit cuts (POC)

- API tokens / `yaaos_pat_…`.
- SCIM.
- Custom roles.
- Email magic-link auth.
- Multiple SSO providers per org; cross-org SSO.
- Per-finding visibility via GitHub repo permissions.
- Personal orgs.
