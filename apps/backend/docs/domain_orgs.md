# domain/orgs

> Org feature aggregate — invitations, SSO config, VCS binding, coding agents, onboarding.

## Scope

`domain/orgs` is a feature aggregate over [`core/tenancy`](core_tenancy.md). All org and membership state (read, write, CRUD) is delegated to `core/tenancy` service primitives — `domain/orgs` never queries `orgs` or `memberships` rows directly. Feature work (invitations, SSO config, VCS binding, coding agents, onboarding) lives here; org/membership IAM is tenancy's concern.

Invitations are the sole access gate for new members — no self-signup. SAML SSO config and the onboarding-status aggregator (`register_onboarding_contributor` / `get_onboarding_status`) live here. Every non-user row is `org_id`-scoped.

## Entities

- **Org** — UUID PK + immutable unique `slug` (used in `X-Yaaos-Org-Slug` header). Soft-deleted via `archived_at`. Row owned by [`core/tenancy`](core_tenancy.md).
- **Membership** — composite PK `(user_id, org_id)`. Per-membership `@handle` (a user can have different handles per org); one of three roles. Removal deletes the row (presence = active). Row owned by [`core/tenancy`](core_tenancy.md).
- **Invitation** — stores `sha256(raw_token)`, never the raw value. Single-use: `accepted_at` clamps the row.
- **SsoConfig** — at most one per org. Holds IdP metadata XML, JIT toggle, exempt-Owner pointer, SP private key (encrypted via [core/secrets](core_secrets.md)).

## Role hierarchy

`OWNER ≥ ADMIN ≥ BUILDER`. `Role` and `role.covers(required)` live in [`core/auth`](core_auth.md) — import from there. Per-action minimums are declared in `core/auth/role_policy._REQUIRED_ROLE`.

- **Owner** — full control incl. org deletion, billing, SSO config, GitHub App linking. ≥1 Owner required per org.
- **Admin** — Owner powers minus deleting the org or removing other Owners.
- **Builder** — read findings, post replies, trigger reviews, manage own acks.

## Invitation lifecycle

1. `invite(...)` — signs `{org_id, email}` via `itsdangerous.URLSafeTimedSerializer` (salt `yaaos-invitation`, 7-day TTL), inserts row with `sha256(raw_token)`, sends SMTP email, audits `invitation/invited`. Returns `(Invitation, raw_token)` — raw token only ever surfaced in the email.
2. `accept_invitation(raw_token, user_id, actor)` — verifies signature + TTL, looks up by token hash, refuses on `accepted_at` (`InvitationUsedError`) or expiry (`InvitationExpiredError`) or mismatch (`InvitationInvalidError`). On success: inserts membership, stamps `accepted_at`, audits `membership/joined`. Re-acceptance with existing membership is a no-op (still marks token used).
3. Handle defaults to email local-part (lower-cased, ≤64 chars).
4. **Expired-invitation sweep** — `invitation_sweeper.run_invitation_sweep_loop()` spawned via `web.py`'s `RouteSpec.on_startup`. Runs on the same `YAAOS_AUTH_CLEANUP_INTERVAL_SECONDS` cadence. `domain/orgs` owns this sweep; `core/identity` does not touch invitations.

`/api/memberships/accept` is `RouteSecurity.PUBLIC` — the signed token is the authorization, not a membership.

## Membership mutations

- `change_role` — updates row + calls `sessions.revoke_all_for_user`. User must re-authenticate.
- `remove_member` — deletes row + revokes all sessions. No-op if row already gone.

Both audit with `from_role` + `to_role` payload.

## VCS + coding agents

- One VCS plugin per org. State on the `orgs` row (`vcs_plugin_id` + `vcs_settings`). GitHub install handshake is via `POST /api/github/install/start` (separate endpoint so `X-Yaaos-Org-Slug` + CSRF are available); `set_vcs` records the choice on first-bind. Switching is two-step: clear then set.
- `clear_vcs` calls every hook registered via `register_vcs_clear_hook` (see `vcs.py`) before clearing the org row. VCS plugins (e.g. `plugins/github`) register a hook at boot to delete their per-org install rows — no direct model import needed in `domain/orgs`.
- Many coding-agent plugins per org via `org_coding_agents(org_id, plugin_id)` with `settings jsonb`. All mutations audit.

## BYOK routes

HTTP surface for [`core/byok`](core_byok.md) lives in `byok_routes.py` here (BYOK keys are per-org; routes need `core/sessions` deps). `GET` returns `configured` / `not_set` only — plaintext never leaves. Provider list sourced from `core/byok`'s validator registry.

## Session-timeout override

`orgs.session_timeout_override` (nullable integer, minutes) tightens the idle-session window per org. Checked in [`core/sessions`](core_sessions.md) `require()` dep on every org-scoped request. Null = global default. Non-positive values rejected with 422.

## Data owned

Tables: `invitations`, `sso_configs`, `org_coding_agents`. `orgs` and `memberships` are owned by [`core/tenancy`](core_tenancy.md) — `domain/orgs` delegates all reads and writes on those tables through `core/tenancy` service functions (`create_org`, `create_membership`, `get_org_full`, `list_memberships_for_org`, `update_org_fields`, etc.). `domain/orgs/repository.py` exposes compatibility shims over those service functions for callers in this module and tests. See `models.py` + [core_database.md](core_database.md) for columns.

Notable constraints:
- `UNIQUE(org_id, handle)` on `memberships` — keeps `@mentions` unambiguous.
- Partial unique `uq_invitations_pending_org_email` on `(org_id, lower(email)) WHERE accepted_at IS NULL` — blocks duplicate pending invites.
- `orgs.registered_iam_arn` partial UNIQUE (`WHERE NOT NULL`), stored lowercased. Paired with `orgs.aws_region` via check constraint `ck_orgs_arn_region_paired` (both-or-neither). ARN must match `arn:aws:iam::<12-digit>:role/<name>` with no path slashes — paths are stripped by AWS's assumed-role form, so different-path roles could collide on the same canonical. `PATCH /api/orgs` runs an app-layer cross-org collision check before the DB write, returning 422 `arn_already_registered` instead of a DB constraint 500. When the ARN changes or is cleared, `PATCH /api/orgs` calls `revoke_all_for_arn(old_arn)` before writing — agents holding old-ARN bearers 401 on their next call.

## SSO discover

`GET /api/sso/discover?email=<address>` — public; returns `{provider: "github" | "saml", saml_org_slug?}` by scanning `sso_configs.email_domains` (JSONB array). Owned here because it queries `sso_configs` which is a `domain/orgs` table. Route prefix `/api/sso/` is already classified PUBLIC by the auth middleware. See `sso_web.py`.

## Import-cycle note

`domain.orgs.web` imports `core.sessions.dependencies`. The side-effect import of `orgs.web` lives in `app/web.py` after both modules finish loading — `domain.orgs.__init__` does NOT trigger it. `Role` is no longer imported from `domain.orgs`; callers import it from `core.auth` directly. `core.sessions.dependencies` no longer imports `domain.orgs`.

## HTTP routes

See `web.py` for the full route list (`/api/memberships`, `/api/vcs`, `/api/coding-agents`, `/api/orgs`, `/api/api-keys`). See `sso_web.py` for `/api/sso/*` including `/api/sso/discover`.

## How it's tested

- `test/test_repository.py` — repository helpers (invitation + shim calls to tenancy) against real Postgres.
- `test/test_invitations.py` — invite, accept, used-token, expired-token, garbage-token, remove revokes sessions, role change revokes sessions.
- `test/test_membership_endpoints.py` — ASGI-driven: invite + email sent, role enforcement, accept happy path, accept-expired → 410, accept-used → 410, remove/change_role session revocation.
- `test/test_inbox_binding.py` — ContextVar isolation for the email inbox: `send_plain` writes to the bound inbox; fresh bind hides prior messages; fail-fast `RuntimeError` before bind.
- `test/test_tenancy_delegation.py` — service tests verifying `create_org` + `create_membership` delegate through `core/tenancy`, and SSO authz flags are written via `set_sso_authz_for_org`.

Email inbox isolation between tests is provided by the `email_inbox_isolation` autouse fixture in `app/testing/isolation`. Tests read sent emails via `app.testing.seed.read_email_inbox()`.
