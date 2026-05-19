# M02 implementation plan

> Phased build order. Read [requirements.md](requirements.md) and [architecture.md](architecture.md) first.

## Phase 0 — scaffolding

- Add deps: `authlib`, `python3-saml`, `itsdangerous`, `pyotp`, `slowapi`. (`structlog`, `cryptography`, `pyjwt` already present.)
- `docker-compose.dev.yml` + backend Dockerfile install `libxmlsec1-dev`, `xmlsec1`. Mac-host install not supported; everything runs in Docker.
- Add **Mailpit** to `docker-compose.dev.yml` (`axllent/mailpit` image, ports 1025 SMTP / 8025 UI). Backend `SMTP_HOST`/`SMTP_PORT` env vars point to it in dev.
- Add `/webhooks` to `apps/web/vite.config.ts` proxy table (alongside `/api`, `/openapi.json`).
- `docs/setup.md` updated for new env vars, Docker requirement, Mailpit UI URL (`http://localhost:8025`), and a note that the dev GitHub OAuth App is already provisioned with credentials in `.env` (prod will need its own).
- Stub "Identity & access" section in `docs/system-architecture.md`.

## Phase 1 — data model

- Create `domain/identity`, `domain/orgs` skeletons + per-module doc skeletons. **No `domain/audit`** — use existing `core/audit_log`.
- Broaden `core/primitives.ActorKind` additively (`+ user|workspace|sso`); update `core_primitives.md` and `core_audit_log.md`.
- Single named migration `002_create_all_m02` following the existing pattern in `core/database/service.py`: register `("002_create_all_m02", "create_all")` in `_MIGRATIONS` and add any necessary `ALTER` statements for the `actor_kind` enum extension.
- Repository tests against real Postgres.
- Run `apps/backend/bin/sync_modules`.

## Phase 2 — `core/auth` + middleware

- Contextvars module.
- `current_session`, `require(action)`, `public_route` dependencies.
- Middleware: `X-Org-Slug` enforcement, post-response guard, OTel span + structlog wiring.
- Error-shape helper (401/403/404 contract).
- Integration tests for every middleware behavior.

## Phase 3 — sessions

- `domain/identity/sessions.py`: create, lookup-by-hash, touch, revoke, revoke-all-for-user, rotate.
- Cookie config + double-submit CSRF.
- Periodic cleanup (sessions, invitations, unverified TOTP secrets) — single job in existing scheduler.

## Phase 4 — GitHub OAuth

- `Provider` Protocol in `domain/identity`.
- `plugins/oauth_github`: Authlib OAuth client + callback handler. Real GitHub OAuth App in both dev and prod; dev credentials already in `.env`.
- `plugins/oauth_test`: test-only stub provider, env-gated to `yaaos_env == "test"`. Same `Provider` Protocol; canned identity payload; Playwright drives it via a "Sign in (test)" button visible only in test env.
- Login + callback endpoints with hard-reject and inline account-linking-challenge flow.
- Tests:
  - Backend integration: `pytest-httpx` mocks GitHub's token + `/user` endpoints to exercise the real `oauth_github` adapter end-to-end without network.
  - Backend integration via `oauth_test`: existing-identity login, link-confirm flow, hard-reject path. Tests the auth pipeline, not the GitHub HTTP shape.
  - E2E (Phase 7): Playwright clicks "Sign in (test)" → asserts session cookie + landing page.

## Phase 5 — bootstrap script

- `apps/backend/bin/bootstrap`: interactive; creates first user + org + Owner membership + OAuth identity; idempotent.
- `docs/setup.md` updated.

## Phase 6 — invitations + member management

- `domain/orgs` service: invite, accept, remove, change-role. All audit-logged.
- Endpoints: `POST /api/memberships/invite`, `POST /api/memberships/accept`, `DELETE /api/memberships/{id}`, `PATCH /api/memberships/{id}`.
- Invitation email via signed `itsdangerous` token, 7-day expiry.
- Frontend `domain/orgs` members page.

## Phase 7 — frontend integration

- `domain/auth` login page + org picker + `useCurrentUser`.
- `GET /api/auth/me` returns `{user, orgs, current_org_slug}`.
- Router: all existing pages move under `/orgs/{slug}/...`.
- API-client wrapper auto-injects `X-Org-Slug`.
- `<RequireMembership role="...">` wrapper for role-gated UI.
- **Biggest single diff in the milestone — its own PR.**

## Phase 8 — audit log wiring

- Use existing `core/audit_log.write(...)` from new auth/membership/SSO call sites — no new module.
- `AUDIT_LOG_RETENTION = timedelta(days=30)` in `core/constants.py`; cleanup task purges older rows.
- Read-only admin UI: list + filter (against existing `audit_entries` table).

## Phase 9 — background-job context

- `org_context(...)` context manager.
- Wrap every non-HTTP entrypoint: GitHub poller, reviewer worker, scheduler jobs.
- Patterns rule landed in `apps/backend/docs/patterns.md`.

## Phase 10 — GitHub App org binding

- Owner-pre-picks org → signed `state` → install → callback writes `github_installations(org_id, installation_id)`.
- Webhook handlers look up installation_id → org_id and `org_context()`-wrap.

## Phase 11 — 2FA

- IdP MFA detection via `amr`/`acr` (GitHub treated as MFA-satisfied; documented).
- TOTP enroll + verify endpoints. Encrypted at rest via env-var master key.
- Login-time step-up if user has TOTP and IdP didn't satisfy MFA.
- Mandatory TOTP before SSO-exempt-Owner flag can be set.

## Phase 12 — SAML SSO

- `plugins/saml` with `python3-saml`. SP-initiated only.
- `plugins/saml_dev` stub IdP, env-gated, for end-to-end testing without a real IdP. Issues signed assertions for seeded users.
- Per-org config UI: IdP metadata upload, SP metadata download, JIT toggle, exempt-Owner picker.
- Endpoints: `GET /api/sso/{slug}/login`, `POST /api/sso/{slug}/acs`, `GET /api/sso/{slug}/metadata`.
- Middleware enforces SSO satisfaction when org has SSO on (except exempt Owner). 8-hour TTL on `sso_satisfied_for_org_id`.
- Break-glass flow audit-logged on every use.

## Phase 13 — rate limiting + secret hygiene

- `slowapi` on `/api/auth/*` (per-IP) and mutating endpoints (per-user).
- Document env-var inventory.
- Secret-rotation runbook stub.

## Phase 14 — docs + cleanup

- Fill `domain_identity.md`, `domain_orgs.md`, `core_auth.md`, plugin docs. Update existing `core_audit_log.md` + `core_primitives.md` for the additive `ActorKind` change.
- Complete `docs/system-architecture.md` Identity & access section.
- `apps/backend/docs/patterns.md` + `apps/web/docs/patterns.md` rules: "every route declares security", "every background job opens `org_context`".
- `apps/backend/bin/sync_modules`. Full CI: backend, web, e2e, security scan.
- Delete `plan/notes/users_orgs_auth.md` (promoted into this milestone).

## Dependency order

```
0 → 1 → 2 → 3 ┬─→ 4 → 5 → 6 → 7
              └─→ 8 → 9 → 10 → 11 → 12 → 13 → 14
```

## Cross-cutting through every phase

- TDD: failing test first.
- Triplet tests on protected endpoints (401 / 404 / 403 / 200) via auto-fixture from route registry.
- Each phase updates relevant per-module doc in the same commit.
- E2E (Playwright) test added in Phase 7 and extended in 6, 10, 11, 12.

## Risks

- **SAML install path** — `xmlsec1` system dep. Caught in Phase 0.
- **contextvar leak across asyncio tasks** — propagates through `asyncio.create_task` but not threadpools. Tests assert behavior.
- **Session rotation forgotten on role change** — assert via audit-log inspection in tests.
- **Phase 7 routing migration touches every existing page** — own PR, own review.
- **Empty `org_id` columns today** — additive migration adds FKs; needs ordering with bootstrap script seeding the first org.
