# domain/orgs

> Orgs, memberships, roles, invitations, SSO config, onboarding aggregator.

## Scope

Owns the tenancy boundary. Every non-user yaaos row is `org_id`-scoped; this module owns the `orgs` table and the membership rows that decide who's in it and at what role. Invitations are the sole access gate — no self-signup. SAML SSO config and the onboarding-status aggregator (`register_onboarding_contributor` / `get_onboarding_status`) also live here.

## Entities

- **Org** — UUID PK + immutable unique `slug` (used in `X-Org-Slug` header). Soft-deleted via `archived_at`.
- **Membership** — composite PK `(user_id, org_id)`. Per-membership `@handle` (a user can have different handles per org); one of three roles. Removal deletes the row (presence = active).
- **Invitation** — stores `sha256(raw_token)`, never the raw value. Single-use: `accepted_at` clamps the row.
- **SsoConfig** — at most one per org. Holds IdP metadata XML, JIT toggle, exempt-Owner pointer, SP private key (encrypted via [core/secrets](core_secrets.md)).

## Role hierarchy

`OWNER ≥ ADMIN ≥ BUILDER`. `role.covers(required)` is the only comparison; per-action minimums declared at the call site.

- **Owner** — full control incl. org deletion, billing, SSO config, GitHub App linking. ≥1 Owner required per org.
- **Admin** — Owner powers minus deleting the org or removing other Owners.
- **Builder** — read findings, post replies, trigger reviews, manage own acks.

## Invitation lifecycle

1. `invite(...)` — signs `{org_id, email}` via `itsdangerous.URLSafeTimedSerializer` (salt `yaaos-invitation`, 7-day TTL), inserts row with `sha256(raw_token)`, sends SMTP email, audits `invitation/invited`. Returns `(Invitation, raw_token)` — raw token only ever surfaced in the email.
2. `accept_invitation(raw_token, user_id, actor)` — verifies signature + TTL, looks up by token hash, refuses on `accepted_at` (`InvitationUsedError`) or expiry (`InvitationExpiredError`) or mismatch (`InvitationInvalidError`). On success: inserts membership, stamps `accepted_at`, audits `membership/joined`. Re-acceptance with existing membership is a no-op (still marks token used).
3. Handle defaults to email local-part (lower-cased, ≤64 chars).

`/api/memberships/accept` is `RouteSecurity.PUBLIC` — the signed token is the authorization, not a membership.

## Membership mutations

- `change_role` — updates row + calls `sessions.revoke_all_for_user`. User must re-authenticate.
- `remove_member` — deletes row + revokes all sessions. No-op if row already gone.

Both audit with `from_role` + `to_role` payload.

## VCS + coding agents

- One VCS plugin per org. State on the `orgs` row (`vcs_plugin_id` + `vcs_settings`). GitHub install handshake is via `POST /api/github/install/start` (separate endpoint so `X-Org-Slug` + CSRF are available); `set_vcs` records the choice on first-bind. Switching is two-step: clear then set.
- Many coding-agent plugins per org via `org_coding_agents(org_id, plugin_id)` with `settings jsonb`. All mutations audit.

## BYOK routes

HTTP surface for [`core/byok`](core_byok.md) lives in `byok_routes.py` here (BYOK keys are per-org; routes need `core/sessions` deps). `GET` returns `configured` / `not_set` only — plaintext never leaves. Provider list sourced from `core/byok`'s validator registry.

## Session-timeout override

`orgs.session_timeout_override` (nullable integer, minutes) tightens the idle-session window per org. Checked in [`core/sessions`](core_sessions.md) `require()` dep on every org-scoped request. Null = global default. Non-positive values rejected with 422.

## Data owned

Tables: `orgs`, `memberships`, `invitations`, `sso_configs`. See `models.py` + [core_database.md](core_database.md) for columns.

Notable constraints:
- `UNIQUE(org_id, handle)` on `memberships` — keeps `@mentions` unambiguous.
- Partial unique `uq_invitations_pending_org_email` on `(org_id, lower(email)) WHERE accepted_at IS NULL` — blocks duplicate pending invites.
- `orgs.registered_iam_arn` partial UNIQUE (`WHERE NOT NULL`), stored lowercased. Paired with `orgs.aws_region` via check constraint `ck_orgs_arn_region_paired` (both-or-neither). ARN must match `arn:aws:iam::<12-digit>:role/<name>` with no path slashes — paths are stripped by AWS's assumed-role form, so different-path roles could collide on the same canonical.

## Import-cycle note

`domain.orgs.web` imports `core.sessions.dependencies`; `core.sessions.dependencies` imports `domain.orgs`. The side-effect import of `orgs.web` lives in `app/web.py` after both modules finish loading — `domain.orgs.__init__` does NOT trigger it.

## HTTP routes

See `web.py` for the full route list (`/api/memberships`, `/api/vcs`, `/api/coding-agents`, `/api/orgs`, `/api/api-keys`).

## How it's tested

- `test/test_repository.py` — repository helpers against real Postgres.
- `test/test_invitations.py` — invite, accept, used-token, expired-token, garbage-token, remove revokes sessions, role change revokes sessions.
- `test/test_membership_endpoints.py` — ASGI-driven: invite + email sent, role enforcement, accept happy path, accept-expired → 410, accept-used → 410, remove/change_role session revocation.
