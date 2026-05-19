# M02 architecture

> Module layout, middleware design, contextvar propagation, URL conventions. Read [requirements.md](requirements.md) first.

## Modules

### New backend domains

- `domain/identity` — users, emails, oauth_identities, TOTP, sessions, login flow. Owns the `Provider` Protocol.
- `domain/orgs` — orgs, memberships, roles, invitations, SSO configs.

### Extended backend core

- `core/audit_log` (exists) — broaden `ActorKind` enum additively (`+ user|workspace|sso`), no new module.
- `core/primitives.ActorKind` (exists) — same additive change.
- `core/auth` (new) — middleware, dependencies (`require(action)`, `public_route`), contextvars (`org_id`, `user_id`, `actor_kind`, `route_security_resolved`), `org_context()` for background jobs, OTel/structlog wiring.

### New backend plugins

- `plugins/oauth_github` — Authlib OAuth client + callback handler. Used in dev *and* prod against real GitHub OAuth Apps (one per environment). Dev credentials already provisioned in `.env`. SPA + API share an origin so the session cookie set by the callback is reachable to the SPA.
- `plugins/oauth_test` — test-only stub provider; `assert settings.yaaos_env == "test"` at registration. Mirrors `Provider` Protocol. Used by Playwright E2E and by backend integration tests that need to drive the full callback path without HTTP mocking.
- `plugins/saml` — `python3-saml` adapter; SP-initiated only.
- `plugins/saml_test` — test-only stub IdP for SAML; same env-gating as `oauth_test`. No external IdP needed for SAML E2E.

### New frontend domains

- `apps/web/src/domain/auth` — login page, provider buttons, post-login org picker, `useCurrentUser` hook, API-client wrapper that injects `X-Org-Slug`.
- `apps/web/src/domain/orgs` — members page, invites, role picker, SSO setup, GitHub App linking, audit log view.

### Touched

- Every existing web page moves under `/orgs/{slug}/...`.
- Every existing API endpoint gains `Depends(require(action))` or `Depends(public_route)`.
- GitHub poller and reviewer worker wrap work in `org_context(...)`.
- `core/database` adds FKs to `orgs.id` on existing `org_id` columns.

## Data model

Tables (single named migration `002_create_all_m02` following the project pattern in `core/database/service.py:_apply_create_all` — `Base.metadata.create_all` for new tables plus explicit `ALTER`s for additive enum/column changes):

- `users` — uuid PK, display_name, deactivated_at.
- `user_emails` — user_id, email, is_primary, verified_at. Unique among non-deactivated.
- `oauth_identities` — user_id, provider, external_subject, verified_at. Unique (provider, external_subject).
- `user_totp_secrets` — user_id PK, encrypted_secret, verified_at, last_used_at.
- `orgs` — uuid PK, slug unique immutable, archived_at.
- `memberships` — (user_id, org_id) PK, `handle` text, role enum, created_at. `UNIQUE(org_id, handle)`. Handle is per-membership: a user can be `@jack` in one org and `@jkora` in another.
- `invitations` — org_id, email, role, token_hash, expires_at, accepted_at, invited_by.
- `sso_configs` — org_id PK, idp_metadata, jit_enabled, exempt_owner_user_id, sp_private_key_encrypted.
- `sessions` — token_hash PK, nullable user_id, nullable workspace_id, sso_satisfied_for_org_id (nullable), ip, ua, created_at, last_seen_at, expires_at.
- `audit_entries` — **existing table in `core/audit_log`.** M02 broadens `actor_kind` enum additively (`+ user|workspace|sso`); no new table.
- `github_installations` — org_id, installation_id, created_at.

## Login flow

```
SPA → GET /api/auth/login?provider=github
      → 302 to GitHub
GitHub → GET /api/auth/callback/github?code=...
      → exchange code, verify email_verified
      → lookup oauth_identities
         ├─ identity exists → issue session, redirect to /orgs/{last_or_only_slug}
         ├─ email matches user, provider not linked → block, send link-confirm challenge
         └─ no match
            ├─ pending invitation exists → create user, accept invite, issue session
            └─ otherwise → 403 "ask for invite"
```

SSO flow mirrors but starts at `/api/sso/{slug}/login`, lands at `/api/sso/{slug}/acs`, and sets `sessions.sso_satisfied_for_org_id`.

## Security middleware

Order on `/api/*` request:

1. Resolve session cookie → `current_session` dep sets `user_id` contextvar.
2. If path not in public allowlist (`/api/auth/*`, `/api/health`), require `X-Org-Slug` header — else 400.
3. Route handler's `Depends(require(action))` resolves slug → org_id, loads membership, checks role, sets `org_id` + `actor_kind=user` contextvars + `route_security_resolved=membership`.
4. Public routes use `Depends(public_route)` which sets `route_security_resolved=public`.
5. Post-response guard: if contextvar unset → 500 + alarm. Forgetting protection crashes loudly.
6. Throughout: OTel span + structlog contextvars carry `yaaos.org_id`, `yaaos.user_id`, `yaaos.actor_kind`, `yaaos.actor_id`.

## Background-job context

`with org_context(org_id, actor_kind, actor_id=None)` sets the same contextvars as the HTTP middleware. Every entrypoint that runs outside HTTP wraps its unit of work:

- GitHub poller → wraps each org's poll.
- Reviewer worker → wraps each review with `actor_kind=workspace, actor_id=workspace_id` (or `system` for in-process).
- Scheduler cleanup → `actor_kind=system`.

Discipline rule (in `apps/backend/docs/patterns.md`): any function reading from an org-scoped table must either assert `org_id` contextvar is set, or take `org_id` as an explicit parameter.

## URL & header conventions

- UI: `/orgs/{slug}/tickets`, `/orgs/{slug}/settings`, etc. SPA reads slug from route param.
- API: flat `/api/auth/*`, `/api/findings`, `/api/memberships`, `/api/sso/{slug}/*`. SPA's API-client wrapper auto-injects `X-Org-Slug` from current route.
- Webhooks: `/webhooks/github`, etc. No `X-Org-Slug`; org derived from payload (`installation.id` → `github_installations.org_id`).
- Same-origin: web reverse-proxies `/api/*` and `/webhooks/*` to backend. `vite.config.ts` proxy table needs `/webhooks` added alongside the existing `/api` + `/openapi.json` entries.

## GitHub App ↔ org binding

Pre-pick: Owner navigates to `/orgs/{slug}/settings/github`, clicks "Connect GitHub App" → backend signs `state=<org_id>` via `itsdangerous` → 302 to GitHub install URL → install callback verifies state → `github_installations(org_id, installation_id)` row written.

## Session rotation

Triggered on:

- Successful login (replaces any pre-auth session).
- SSO satisfaction (new `sso_satisfied_for_org_id` value).
- Role change affecting the current user.

Implementation: `sessions.rotate(old_token)` returns new token + deletes old row in same transaction.

## Audit log

Uses the existing `core/audit_log` module's `write(...)` helper. M02 broadens `ActorKind` additively. No new module.

Distinct from per-review event log (lives inside `domain/reviewer` already). Different queries, retention, audience — kept separate by design.

## Forward-compatibility notes

- `sessions.principal` shape (nullable user_id + workspace_id) extends to `api_token_id` without schema change when M03+ adds tokens.
- `audit_log.actor_kind` enum extends with `api_token` later.
- `Provider` Protocol lets Google/Microsoft/etc. land as new `plugins/oauth_*` rows with no `domain/identity` changes.
- `sso_configs` is one row per org now; future multi-IdP becomes `sso_idps` table with `(org_id, idp_id)` PK.

## Diagrams

Single ASCII flow for login (above) since it crosses 4 modules. Everything else is prose — modules and tables are linear enough not to need pictures.
