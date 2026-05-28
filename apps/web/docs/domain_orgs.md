# domain_orgs

> Surfaces tied to a specific org's identity layer: picker, members, audit, SSO config.

## Surfaces

- `/orgs` — `OrgPickerPage`. Lists every org the signed-in user is a member of (role badge per row), with a Create-org modal.
- `/orgs/$slug/members` — `MembersPage`. Roster + invite + role-change + remove.
- `/orgs/$slug/audit` — `AuditPage`. Org-scoped mutating-action log; Owner/Admin only (server enforces).
- SSO config — `SsoConfigPage`. Rendered inside `/orgs/$slug/settings/auth` (composed by `AuthSettingsPage` in `domain_org_settings`).

## Data flow

- Picker: `useMyOrgs` → `GET /api/orgs/mine` (USER_SCOPED — uses the session cookie, no `X-Org-Slug` header).
- Create org: `useCreateOrg` → `POST /api/orgs` (USER_SCOPED via `USER_SCOPED_METHOD_EXACT`; runs before the user has selected an org; slug regex validated client-side).
- Members: `useQuery(["memberships", slug])` against `/api/memberships`. Invite / change-role / remove mutations all invalidate that key on success.
- Audit: `useQuery(["audit", slug, filters])` against `/api/audit?actor_kind&action`.
- SSO: `useQuery(["sso","config"])` against `/api/sso/config`; PUT upserts.

## State / contract

- Picker sorts orgs alphabetically by slug. `last_used_at` is deferred per Open Question 3 in requirements.md.
- Create-org form maps 409 (slug-taken) and 422 (slug format) to inline errors.
- Members remove uses native `window.confirm`; a ConfirmModal upgrade is a polish item.

## Where the code lives

- `apps/web/src/domain/orgs/{OrgPickerPage,MembersPage,AuditPage,SsoConfigPage}.tsx`
- Vitest smoke tests in `apps/web/src/domain/orgs/test/`.
