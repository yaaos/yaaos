# domain/orgs

> Orgs, memberships, roles, invitations, and per-org SSO config.

## Purpose

Owns the tenancy boundary. Every non-user yaaos data row is `org_id`-scoped; this module owns the table that defines an org and the membership rows that decide who's in it and what they can do. Invitations are the sole access gate (no self-signup). SAML SSO config lives here too — the IdP metadata + per-org SP private key + JIT toggle + break-glass exempt-Owner pointer. Phase 1 ships the data model + repository; concrete invite/accept/role flows ship in Phase 6, SSO in Phase 12.

## Public interface

Exported from `app/domain/orgs/__init__.py`:

- Types — `Org`, `Membership`, `Invitation`, `SsoConfig`, `Role`.
- Rows — `OrgRow`, `MembershipRow`, `InvitationRow`, `SsoConfigRow`.
- Exceptions — `OrgNotFoundError`, `MembershipNotFoundError`, `InsufficientRoleError`, `InvitationError`.

HTTP routes (`/api/memberships/*`, `/api/sso/*`) ship in Phases 6 + 12.

## Module architecture

### Entities

- **Org** — UUID PK + immutable unique `slug` used in `/orgs/{slug}/...` and the `X-Org-Slug` header. Soft-deleted via `archived_at`.
- **Membership** — composite PK `(user_id, org_id)`. Carries a per-membership `@handle` (a user can be `@jack` here and `@jkora` there) and one of three roles.
- **Invitation** — pending offer. Stores the sha256 hex of the signed invitation token, never the raw value. Single-use: `accepted_at` clamps the row.
- **SsoConfig** — at most one per org. Holds the IdP metadata XML, JIT toggle, exempt-Owner pointer, and the Fernet-encrypted SP private key used to sign SAML AuthnRequests.

### Key value objects

- **`Role`** — `OWNER ≥ ADMIN ≥ MEMBER`. `role.covers(required)` is the only comparison anywhere in the codebase; per-action minimums declared at the call site.
  - Owner — full control incl. org deletion, billing, SSO config, GitHub App linking. ≥1 Owner required per org.
  - Admin — Owner powers minus deleting the org or removing other Owners.
  - Member — read findings, post replies, trigger reviews, manage own acks.

### Core user flows

Phase 1 ships only the data layer. Invitation lifecycle and SSO config flows ship with their respective phases.

## Data owned

- `orgs`, `memberships`, `invitations`, `sso_configs`.
- `UNIQUE(org_id, handle)` on `memberships` keeps `@mentions` unambiguous inside an org.
- Partial unique `uq_invitations_pending_org_email` on `(org_id, lower(email)) WHERE accepted_at IS NULL` blocks duplicate pending invites for the same address.

## How it's tested

- `test/test_repository.py` — repository helpers against real Postgres; covers org + Owner membership insert, the unique-handle-per-org constraint, role-ordering semantics, role updates, invitation persistence with hashed token, and slug lookup excluding archived orgs.
- Service-level invite/accept/role-change flows are exercised by Phase 6 integration tests; SAML flows by Phase 12.
