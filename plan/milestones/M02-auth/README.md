# M02 — Users, orgs, auth

> Identity, tenancy, and access for yaaos. Adds real users, orgs, OAuth + SAML SSO, sessions, roles, audit log.

## Status

`[planned]` — depends on M01 shipping.

## Reading order

1. [requirements.md](requirements.md) — locked spec: data model, roles, flows, explicit cuts.
2. [architecture.md](architecture.md) — module layout, middleware design, contextvar propagation, URL/header conventions.
3. [implementation-plan.md](implementation-plan.md) — phased build order, dependencies, risks.

## Scope at a glance

- Users with UUID PK, multiple verified emails, OAuth-only login.
- Multi-org from day one; three roles (Owner/Admin/Member); per-org memberships.
- GitHub OAuth + SAML SSO. TOTP fallback for break-glass.
- Opaque server-side sessions, double-submit CSRF, same-origin SPA + API.
- Polymorphic audit log (user / workspace / system / sso actors).
- Bootstrap script for first org/user; no self-signup.
- Security middleware: default-deny `/api/*`, contextvar guard catches missing role checks.
- `org_id` + `user_id` on every log, trace, span.

## Out of scope (deferred)

- API tokens (`yaaos_pat_…`).
- SCIM auto-deprovisioning.
- Custom roles beyond the three-enum.
- Multiple SSO providers per org; cross-org SSO.
- Personal / single-user orgs.
- Per-finding visibility from GitHub repo permissions.

## Source

Matured from [plan/notes/users_orgs_auth.md](../../notes/users_orgs_auth.md) — note kept for now; delete after milestone ships.
