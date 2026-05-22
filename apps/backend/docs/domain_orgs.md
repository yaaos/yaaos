# domain/orgs

> Orgs, memberships, roles, invitations, SSO config, onboarding aggregator.

## Purpose

Owns the tenancy boundary. Every non-user yaaos data row is `org_id`-scoped; this module owns the table that defines an org and the membership rows that decide who's in it and what they can do. Invitations are the sole access gate (no self-signup). SAML SSO config lives here too — the IdP metadata + per-org SP private key + JIT toggle + break-glass exempt-Owner pointer. SSO config flows ship in Phase 12. M04 Phase 6b absorbed the legacy `domain/settings` onboarding-status aggregator: `register_onboarding_contributor(name, check)` lets plugins push readiness callbacks into a per-org registry, and `get_onboarding_status(org_id)` fans them out; `onboarding_web.py` re-exposes the legacy `/api/settings/onboarding` + `/api/settings/plugins` endpoints unchanged for the M01 settings page that consumes them.

## Public interface

Exported from `app/domain/orgs/__init__.py`:

- Types — `Org`, `Membership`, `Invitation`, `SsoConfig`, `Role`, `VcsState`, `CodingAgentInstall`.
- Rows — `OrgRow`, `MembershipRow`, `InvitationRow`, `SsoConfigRow`, `OrgCodingAgentRow`.
- Lifecycle — `invite`, `accept_invitation`, `change_role`, `remove_member`.
- VCS — `get_vcs`, `set_vcs`, `clear_vcs`. One VCS per org; state lives on the `orgs` row.
- Coding agents — `list_coding_agents`, `install_coding_agent`, `update_coding_agent_settings`, `uninstall_coding_agent`. Many per org via `org_coding_agents`.
- Exceptions — `OrgNotFoundError`, `MembershipNotFoundError`, `InsufficientRoleError`, `InvitationError`, `InvitationExpiredError`, `InvitationUsedError`, `InvitationInvalidError`, `CodingAgentAlreadyInstalledError`, `CodingAgentNotInstalledError`.

HTTP routes (registered side-effect via `web.py`, mounted from `main.py` to break the `domain.orgs ↔ domain.sessions` import cycle):

| Method | Path | Action |
|---|---|---|
| GET    | `/api/memberships`              | `MEMBERS_READ` — list members of the current org. |
| POST   | `/api/memberships/invite`       | `MEMBERS_INVITE` — invite by email; sends an SMTP message and writes audit. |
| POST   | `/api/memberships/accept`       | public allowlist; session cookie identifies the acceptor. |
| PATCH  | `/api/memberships/{user_id}`    | `MEMBERS_CHANGE_ROLE` — update role; revokes the target's existing sessions. |
| DELETE | `/api/memberships/{user_id}`    | `MEMBERS_REMOVE` — drop the row + revoke every session for the user. |
| GET    | `/api/vcs`                      | `VCS_READ` — current VCS install (plugin_id + settings). |
| POST   | `/api/vcs`                      | `VCS_WRITE` — set chosen plugin; returns either the new state OR a redirect URL when the plugin needs an out-of-band install. |
| DELETE | `/api/vcs`                      | `VCS_WRITE` — clear the org's VCS choice. |
| GET    | `/api/coding-agents`            | `CODING_AGENT_READ` — list installed coding-agent plugins. |
| POST   | `/api/coding-agents`            | `CODING_AGENT_WRITE` — install a plugin. |
| PATCH  | `/api/coding-agents/{plugin_id}`| `CODING_AGENT_WRITE` — replace settings. |
| DELETE | `/api/coding-agents/{plugin_id}`| `CODING_AGENT_WRITE` — uninstall. |
| PATCH  | `/api/orgs`                     | `ORG_SETTINGS_WRITE` — update top-level org settings (today: `session_timeout_override`). |
| GET    | `/api/byok`                     | `BYOK_READ` — list providers with status (`configured` / `not_set`) + timestamps. |
| POST   | `/api/byok/{provider}`          | `BYOK_WRITE` — set/update the encrypted key for a provider. |
| POST   | `/api/byok/{provider}/validate` | `BYOK_WRITE` — call the provider plugin's validator with the stored key. |
| DELETE | `/api/byok/{provider}`          | `BYOK_WRITE` — remove the row. |

SSO endpoints land in Phase 12.

### BYOK routes

The HTTP surface for [`core/byok`](core_byok.md) lives in `byok_routes.py` here because BYOK keys are per-org and the routes need `domain/sessions` deps; `core/byok` stays free of HTTP. Plaintext crosses the boundary only inbound on `POST {provider}` — `GET` returns `configured` / `not_set` only. The provider list is sourced from `core/byok`'s validator registry: a plugin registering its validator auto-surfaces here.

### Session-timeout override

`orgs.session_timeout_override` (nullable integer, minutes) lets an org tighten the idle-session window without redeploy. The check lives in [`domain/sessions`](domain_sessions.md)'s `require()` dep: every org-scoped request looks up the org's override and rejects with `401 session_idle_expired` when `last_seen_at + override` (or [`SESSION_IDLE_TIMEOUT`](core_database.md) when null) is in the past. Null = use the global default. Owner+Admin can change the value via `PATCH /api/orgs`; non-positive values are rejected with 422. Unknown body keys are ignored so future top-level settings can be added without breaking existing clients.

### VCS

One VCS plugin per org. State lives on the `orgs` row (`vcs_plugin_id` + `vcs_settings`). Switching is two-step: clear, then set. The github plugin's `install_url(org_id)` returns `/api/github/install`; the picker UI navigates the user there, the M02 handshake completes the install, and the install callback calls `set_vcs(...)` to durably record the org's choice. All three mutations audit (`vcs.installed`, `vcs.cleared`).

### Coding agents

Many coding-agent plugins per org. Each install is an `org_coding_agents` row keyed by `(org_id, plugin_id)` with a `settings jsonb`. Mutations audit (`coding_agent.installed`, `coding_agent.settings_updated`, `coding_agent.uninstalled`). Plugin-specific settings shape lives in the plugin itself (Phase 10 ships the Claude Code Pydantic model + bespoke UI).

## Module architecture

### Entities

- **Org** — UUID PK + immutable unique `slug` used in `/orgs/{slug}/...` and the `X-Org-Slug` header. Soft-deleted via `archived_at`.
- **Membership** — composite PK `(user_id, org_id)`. Carries a per-membership `@handle` (a user can be `@jack` here and `@jkora` there) and one of three roles.
- **Invitation** — pending offer. Stores the sha256 hex of the signed invitation token, never the raw value. Single-use: `accepted_at` clamps the row.
- **SsoConfig** — at most one per org. Holds the IdP metadata XML, JIT toggle, exempt-Owner pointer, and the SP private key (encrypted via [core/secrets](core_secrets.md)) used to sign SAML AuthnRequests.

### Key value objects

- **`Role`** — `OWNER ≥ ADMIN ≥ MEMBER`. `role.covers(required)` is the only comparison anywhere in the codebase; per-action minimums declared at the call site.
  - Owner — full control incl. org deletion, billing, SSO config, GitHub App linking. ≥1 Owner required per org.
  - Admin — Owner powers minus deleting the org or removing other Owners.
  - Member — read findings, post replies, trigger reviews, manage own acks.

### Invitation lifecycle

1. `invite(org_id, email, role, invited_by_user_id, actor)` — signs `{org_id, email}` via `itsdangerous.URLSafeTimedSerializer` (salt `yaaos-invitation`, 7-day TTL), inserts the invitation row with `sha256(raw_token)`, sends an SMTP plain-text email containing the accept URL, writes an `invitation/invited` audit entry. Returns `(Invitation, raw_token)` — the raw token is only ever surfaced inside the email (test callers also read it from the return).
2. `accept_invitation(raw_token, user_id, actor)` — verifies the signature + TTL, looks up the row by token hash, refuses on `accepted_at` set (`InvitationUsedError`) or expiry (`InvitationExpiredError`), refuses on payload/row mismatch (`InvitationInvalidError`). On success: insert the membership with `Role(row.role)`, stamp `accepted_at`, write an `membership/joined` audit entry. Idempotent against existing membership — re-acceptance is a no-op that still marks the row used.
3. Membership creation = always one row per `(user_id, org_id)`. Handle defaults to the email local-part (lower-cased, ≤64 chars).

### Membership mutations

- `change_role(org_id, user_id, new_role, actor)` updates the row and calls `sessions.revoke_all_for_user(user_id)` — the affected user must re-authenticate. Phase 12 replaces the blunt rotation with a targeted session-row patch.
- `remove_member(org_id, user_id, actor)` deletes the row and revokes every session for the user. No-op if the membership is already gone.

Both write `membership/role_changed` or `membership/removed` audit entries with the `from_role` + `to_role` payload.

### Email transport

`email.send_plain` wraps blocking `smtplib` in `asyncio.to_thread`. Dev points at Mailpit (`smtp://localhost:1025`); prod points wherever the operator configured (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS`, `SMTP_FROM`). In `YAAOS_ENV=test` the call short-circuits and appends to `get_test_inbox()` — tests assert against the list.

### Public-allowlist exception for `/accept`

`/api/memberships/accept` is on `PUBLIC_PATH_EXACT` because it must work for users who have a session but not yet a membership in the org. The signed token is the authorization, not the membership.

### Import-cycle break

`domain.orgs.web` imports `domain.sessions.dependencies` (for `require`, `public_route`, `current_actor`). `domain.sessions.dependencies` imports `domain.orgs` (repository, service.Membership, types.Role). To avoid a partial-init `ImportError`, `domain.orgs.__init__` does NOT trigger `orgs.web`; the side-effect import lives in `app/main.py` after both modules have finished loading.

## Data owned

- `orgs`, `memberships`, `invitations`, `sso_configs`.
- `UNIQUE(org_id, handle)` on `memberships` keeps `@mentions` unambiguous inside an org.
- Partial unique `uq_invitations_pending_org_email` on `(org_id, lower(email)) WHERE accepted_at IS NULL` blocks duplicate pending invites for the same address.

## How it's tested

- `test/test_repository.py` — repository helpers against real Postgres.
- `test/test_invitations.py` — service-layer coverage: invite (verifies inbox), accept happy path, used-token error, expired-token error, garbage-token error, remove revokes sessions, role change revokes sessions.
- `test/test_membership_endpoints.py` — ASGI-driven endpoint coverage: invite + email sent, member role rejected for invite, accept happy path, accept-expired → 410, accept-used → 410, remove revokes sessions, change_role rotates sessions, list-members returns roster.
- SAML flows ship with Phase 12.
