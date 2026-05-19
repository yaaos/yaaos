# M02 phase ledger

> Source of truth for "what's done." Every box must become `[x]` before M02 is complete. Tick boxes as you go. See [START_HERE.md](START_HERE.md) for the ritual.

## Phase 0 — scaffolding

- [x] `authlib`, `python3-saml`, `itsdangerous`, `pyotp`, `slowapi` added to `apps/backend/pyproject.toml`
- [x] `docker-compose.dev.yml` installs `libxmlsec1-dev` + `xmlsec1` in the backend image (or its base Dockerfile does)
- [x] `docker-compose.dev.yml` adds `mailpit` service (`axllent/mailpit`, ports `1025:1025` SMTP and `8025:8025` UI)
- [x] `apps/web/vite.config.ts` proxy table includes `/webhooks` alongside `/api` and `/openapi.json`
- [x] `docs/setup.md` mentions Mailpit UI at `http://localhost:8025` + GitHub OAuth note (creds in `.env`)
- [x] `docs/system-architecture.md` has a stub "Identity & access" section (filled later)
- [x] `apps/backend/bin/ci` exits 0
- [x] Phase committed on branch `m02-auth`

## Phase 1 — data model

- [x] `domain/identity` module skeleton (`__init__.py`, `service.py`, `repository.py`, `models.py`, `types.py`) + `apps/backend/docs/domain_identity.md` skeleton
- [x] `domain/orgs` module skeleton + `apps/backend/docs/domain_orgs.md` skeleton
- [x] `core/primitives.ActorKind` extended additively with `USER`, `WORKSPACE`, `SSO` (existing values preserved)
- [x] `apps/backend/docs/core_primitives.md` updated for the new ActorKind values
- [x] `apps/backend/docs/core_audit_log.md` updated to note M02 actor kinds
- [x] New tables added: `users`, `user_emails`, `oauth_identities`, `user_totp_secrets`, `orgs`, `memberships`, `invitations`, `sso_configs`, `sessions`, `github_installations`
- [x] `memberships` has `UNIQUE(org_id, handle)` constraint
- [x] Named migration `010_create_all_m02` registered in `core/database/service.py:_MIGRATIONS` (see DECISIONS.md — `002` collided with an existing M01 migration)
- [x] Repository tests against real Postgres for each new table (TDD: tests written first)
- [x] `apps/backend/bin/sync_modules` run; `tach.toml` updated by the script
- [x] `apps/backend/bin/ci` exits 0
- [x] Phase committed

## Phase 2 — core/auth + middleware

- [x] `core/auth` module created with `context.py`, `middleware.py`, `module.py`, `__init__.py`, `types.py` (the dependency factories live in `domain/auth` — see DECISIONS, layering forbids `core → domain`)
- [x] Contextvars defined: `org_id`, `user_id`, `actor_kind`, `actor_id`, `route_security_resolved`
- [x] `require(action)` dependency factory implemented in `domain/auth`; resolves `X-Org-Slug` → membership, checks role, sets contextvars
- [x] `public_route` dependency implemented in `domain/auth`; sets `route_security_resolved` without auth
- [x] Middleware rejects `/api/*` requests missing `X-Org-Slug` on M02-protected prefixes (allowlist: `/api/auth/*`, `/api/health`) with 400; legacy prefixes pass through (Phase 14 expands the protected set)
- [x] Middleware post-response guard: 500 + log if `route_security_resolved` unset on a 2xx response (4xx/5xx pass through unchanged to avoid masking dep-raised 401/403/404 with a misleading 500)
- [x] OTel span attributes `yaaos.org_id`, `yaaos.user_id`, `yaaos.actor_kind` set in middleware
- [x] structlog contextvars processor configured for the same fields (Phase 9 will extend `org_context()` with `bind_contextvars`)
- [x] Error-shape helper: 401 unauthenticated, 403 wrong role, 404 unknown-or-forbidden org slug
- [x] Integration tests cover: missing header → 400, unknown slug → 404, wrong role → 403, success → 200, missing dependency → 500
- [x] `apps/backend/docs/core_auth.md` written (+ `apps/backend/docs/domain_auth.md`)
- [x] `apps/backend/bin/ci` exits 0
- [x] Phase committed

## Phase 3 — sessions

- [x] `domain/identity/sessions.py` implements `create`, `lookup`, `touch`, `revoke`, `revoke_all_for_user`, `rotate`
- [x] Session token: 32 bytes via `secrets.token_urlsafe`; stored as `hashlib.sha256` hex
- [x] Cookie config: `HttpOnly; SameSite=Lax; Secure` — `Secure` env-gated off when `yaaos_env == "dev"`
- [x] Double-submit CSRF token issued at session creation, validated on `POST/PUT/PATCH/DELETE` to M02-protected `/api/*` paths
- [x] `sessions.rotate(old_token)` returns new token + deletes old row in same transaction
- [x] Periodic cleanup task spawned in FastAPI lifespan purges expired sessions, expired invitations, unverified TOTP secrets older than 24h
- [x] Tests: create + lookup, rotate (old invalidated), revoke, revoke-all, CSRF mismatch returns 403, expired session returns None
- [x] `apps/backend/bin/ci` exits 0
- [x] Phase committed

## Phase 4 — GitHub OAuth

- [x] `Provider` Protocol defined in `domain/identity/providers.py`
- [x] `plugins/oauth_github` implements Provider via Authlib; reads creds from settings
- [x] `plugins/oauth_test` implements Provider as a test-only stub; `assert settings.yaaos_env == "test"` at module import
- [x] `GET /api/auth/login?provider=<id>` redirects to provider's authorization URL
- [x] `GET /api/auth/callback/<provider>` exchanges code, verifies `email_verified`, applies account-linking + hard-reject rules
- [x] Hard-reject path: un-invited login → 403 with "ask for invite" message
- [x] Account-linking challenge: same-browser inline flow (sign in via existing provider to confirm link)
- [x] Backend integration tests for `oauth_github` use `pytest-httpx` to mock GitHub's token + `/user` endpoints
- [x] Backend integration tests via `oauth_test` cover: existing-identity, link-confirm, hard-reject
- [x] `apps/backend/docs/plugins_oauth_github.md` + `plugins_oauth_test.md` written
- [x] `apps/backend/bin/ci` exits 0
- [x] Phase committed

## Phase 5 — bootstrap script

- [x] `apps/backend/bin/bootstrap` is executable, interactive
- [x] Prompts for: email, GitHub username, display name, org name, org slug
- [x] Creates `users` row + `user_emails` row (verified) + `oauth_identities` row for GitHub + `orgs` row + `memberships` row with role=Owner
- [x] Idempotent: running twice with the same inputs does not error or duplicate
- [x] `docs/setup.md` documents the bootstrap step
- [x] Test: invokable via subprocess in pytest with stdin-piped inputs; produces expected rows
- [x] `apps/backend/bin/ci` exits 0
- [x] Phase committed

## Phase 6 — invitations + membership

- [x] `domain/orgs` service: `invite`, `accept_invitation`, `remove_member`, `change_role`; each emits an audit-log entry
- [x] Endpoints: `POST /api/memberships/invite`, `POST /api/memberships/accept`, `DELETE /api/memberships/{id}`, `PATCH /api/memberships/{id}`
- [x] Invitation tokens: signed via `itsdangerous`, 7-day expiry, single-use (mark `accepted_at`)
- [x] Email sent via SMTP (configured to Mailpit in dev); invitation contains signed link
- [x] Role-change auto-rotates affected user's sessions
- [x] Removed member's sessions are all revoked
- [x] Tests: invite happy path, accept token, accept expired token → 410, accept used token → 410, remove member revokes sessions, change role rotates sessions
- [x] Frontend `apps/web/src/domain/orgs` has Members page with invite form + role picker + remove button
- [x] `apps/backend/docs/domain_orgs.md` populated
- [x] `apps/backend/bin/ci` + `apps/web/bin/ci` exit 0
- [x] Phase committed

## Phase 7 — frontend integration

- [x] `GET /api/auth/me` endpoint returns `{user, orgs, current_org_slug}`
- [x] `apps/web/src/domain/auth` exists with Login page (provider buttons) + post-login org picker + `useCurrentUser` hook
- [x] TanStack Router refactored: org-scoped routes nested under `/orgs/$slug/...`; user-global account page at `/account` (not org-scoped)
- [x] Existing pages (dashboard, tickets, memory, settings) accessible via `/orgs/$slug/<page>`
- [x] `/account` page with email list, TOTP setup entry point, and "Sign out of all sessions" button
- [x] Root `/` redirects to `/orgs/<default-slug>/dashboard` if logged in, `/login` if not
- [x] API-client wrapper auto-injects `X-Org-Slug` header from current route param
- [x] `<RequireMembership role="...">` component wraps role-gated UI
- [x] "Sign out of all sessions" button lives on `/account`; calls `POST /api/auth/logout-all`
- [x] Playwright E2E test: login via `oauth_test` → land on dashboard → switch org → invite member → accept invite → change role → logout-all
- [x] `apps/web/docs/` per-page docs updated for new URL shape
- [x] `apps/web/bin/ci` + `apps/e2e/bin/ci` exit 0
- [x] Phase committed

## Phase 8 — audit log wiring

- [x] All identity service calls (login, logout, link, unlink) emit audit entries via `core/audit_log.write`
- [x] All orgs service calls (invite/accept/remove/change-role/sso-config-change) emit audit entries
- [x] `AUDIT_LOG_RETENTION = timedelta(days=30)` defined in a single constants module (create `apps/backend/app/core/constants.py` if absent)
- [x] Cleanup task purges `audit_entries` older than `AUDIT_LOG_RETENTION`; runs daily in the existing scheduler
- [x] Read-only `GET /api/audit` endpoint with org-scoped filtering by `actor_kind`, `action`, date range
- [x] Frontend Audit page in `apps/web/src/domain/orgs` for Owners/Admins
- [x] Tests: each emitter writes the expected row; cleanup purges old rows; endpoint paginates and filters
- [x] `apps/backend/docs/core_audit_log.md` updated with new actions list
- [x] `apps/backend/bin/ci` + `apps/web/bin/ci` exit 0
- [x] Phase committed

## Phase 9 — background-job context

- [x] `core/auth/context.py` exports `org_context(org_id, actor_kind, actor_id=None)` async context manager
- [x] `org_context` sets the same contextvars + OTel span attrs + structlog vars as HTTP middleware
- [x] GitHub poller wraps each org's poll in `org_context(...)`
- [x] Reviewer worker wraps each review in `org_context(...)` with appropriate `actor_kind`
- [x] Scheduler cleanup jobs wrap their work in `org_context(..., actor_kind=system)`
- [x] Audit entries written from background jobs have correct `actor_kind` (`workspace` for reviewer, `system` for scheduler)
- [x] `apps/backend/docs/patterns.md` documents the rule: any function reading from an org-scoped table must either assert `org_id` contextvar is set or take `org_id` as an explicit parameter
- [x] Tests: contextvar propagation through `asyncio.create_task`; missing context raises in assertion-mode functions
- [x] `apps/backend/bin/ci` exits 0
- [x] Phase committed

## Phase 10 — GitHub App org binding

- [x] `GET /api/github/install` endpoint signs `state=<org_id>` via `itsdangerous` and redirects to GitHub App install URL
- [x] `/webhooks/github/install_callback` (or post-install redirect) verifies state, writes `github_installations(org_id, installation_id)` row
- [x] Existing GitHub webhook handler looks up `installation_id → org_id` and wraps handling in `org_context(...)`
- [x] Frontend Settings page has "Connect GitHub" button visible to Owners
- [x] Tests: state signature verified, mismatched state rejected, webhook handler resolves org correctly
- [x] `apps/backend/docs/plugins_github.md` updated for install binding
- [x] `apps/backend/bin/ci` + `apps/web/bin/ci` exit 0
- [x] Phase committed

## Phase 11 — 2FA

- [x] `domain/identity` exposes IdP MFA detection: parses `amr`/`acr` from OIDC tokens when present
- [x] GitHub OAuth path documented as MFA-trusted (no API check); comment in `plugins/oauth_github` references this
- [x] `POST /api/auth/totp/enroll` returns QR seed; `POST /api/auth/totp/verify` confirms a code and writes `verified_at`
- [x] TOTP secret encrypted at rest using env-var master key + `cryptography` library
- [x] Login flow: if user has verified TOTP and IdP didn't satisfy MFA, present TOTP challenge before issuing session
- [x] SSO-exempt Owner flag cannot be set unless that Owner has a verified TOTP secret (enforced at API + UI)
- [x] Frontend account-settings page has "Set up 2FA" flow with QR display + verify input
- [x] Tests: enroll → verify happy path, login step-up triggers when needed, exempt-flag-without-TOTP rejected
- [x] `apps/backend/bin/ci` + `apps/web/bin/ci` exit 0
- [x] Phase committed

## Phase 12 — SAML SSO

- [x] `plugins/saml` implements Provider via `python3-saml`, SP-initiated only
- [x] `plugins/saml_test` stub IdP, env-gated to `test`, issues signed assertions for seeded users
- [x] Per-org SSO config: upload IdP metadata XML, generate SP metadata for download, JIT-toggle, exempt-Owner picker
- [x] Endpoints: `GET /api/sso/{slug}/login`, `POST /api/sso/{slug}/acs`, `GET /api/sso/{slug}/metadata`
- [x] On successful assertion: match by verified email, JIT-create membership if enabled, mark session `sso_satisfied_for_org_id` with 8-hour TTL
- [x] Middleware enforces SSO satisfaction when org has SSO on; exempt Owners bypass via OAuth + TOTP
- [x] Break-glass exempt-Owner login emits audit entry with `actor_kind=user` + metadata `{"break_glass": true}`
- [x] Frontend Settings SSO page: upload metadata, download SP metadata, toggle JIT, pick exempt Owner
- [x] Playwright E2E via `saml_test`: enable SSO → login fails without SSO → SSO satisfies → JIT creates membership when enabled
- [x] SAML SP private key per org encrypted at rest using same master key as TOTP
- [x] `apps/backend/docs/plugins_saml.md` written
- [x] `apps/backend/bin/ci` + `apps/web/bin/ci` + `apps/e2e/bin/ci` exit 0
- [x] Phase committed

## Phase 13 — rate limiting + secret hygiene

- [x] `slowapi` rate limiter on `/api/auth/*` (per-IP) and all mutating `/api/*` endpoints (per-user)
- [x] Limits documented in `apps/backend/docs/core_auth.md`
- [x] Settings (`apps/backend/app/core/config/service.py`) declares all new secret env vars: session cookie secret, invitation token secret, TOTP master key, OAuth GitHub client id + secret
- [x] Missing-secret behavior: backend refuses to start if a required secret is unset in non-dev env; dev has stub defaults
- [x] `docs/setup.md` lists every new env var with a one-line description
- [x] Secret-rotation runbook stub at `docs/runbooks/secret-rotation.md` (one paragraph per secret is fine)
- [x] Tests: rate limit returns 429 when exceeded; missing secret in prod env causes startup failure
- [x] `apps/backend/bin/ci` exits 0
- [x] Phase committed

## Phase 14 — docs + cleanup + final verification

- [x] Per-module docs filled and reviewed: `core_auth.md`, `domain_identity.md`, `domain_orgs.md`, plugin docs (`plugins_oauth_github.md`, `plugins_oauth_test.md`, `plugins_saml.md`, `plugins_saml_test.md`)
- [x] `apps/backend/docs/core_audit_log.md` + `core_primitives.md` reflect ActorKind extension
- [x] `docs/system-architecture.md` "Identity & access" section complete: login flow ASCII, session lifecycle, contextvar propagation
- [x] `apps/backend/docs/patterns.md` includes "every route declares security" + "every background job opens org_context"
- [x] `apps/web/docs/patterns.md` includes "API client auto-injects X-Org-Slug" + "use RequireMembership for role gates"
- [x] `docs/glossary.md` adds: user, org, membership, role, session, invitation, provider, SSO, break-glass Owner
- [x] `grep -rn "TBD\|TODO\|coming soon" plan/milestones/M02-auth apps/*/docs` returns no hits introduced by M02
- [x] `grep -rn "<old-renamed-thing>" apps/*/docs docs` clean for any symbols renamed during M02
- [x] `apps/backend/bin/sync_modules` produces no diff (tach is up to date)
- [x] Full CI: `apps/backend/bin/ci` + `apps/web/bin/ci` + `apps/e2e/bin/ci` all exit 0
- [x] Security scan run (semgrep via backend CI covers it)
- [x] `plan/notes/users_orgs_auth.md` deleted (promoted into this milestone)
- [x] `plan/ROADMAP.md` updated: M02 status moved from `[planned]` to `[done]`
- [x] `grep -n '\[ \]' plan/milestones/M02-auth/PHASES.md` returns zero matches
- [x] Final assistant message summarizes work done + appends `DECISIONS.md` contents
- [x] Phase committed

## Completion check (run before declaring milestone done)

- [x] `grep -n '\[ \]' plan/milestones/M02-auth/PHASES.md` → no output
- [x] `apps/backend/bin/ci` → exit 0
- [x] `apps/web/bin/ci` → exit 0
- [x] `apps/e2e/bin/ci` → exit 0
- [x] `git status` on branch `m02-auth` → clean
- [x] `git log main..m02-auth --oneline` shows commits for every phase
