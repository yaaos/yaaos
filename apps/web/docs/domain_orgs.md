# domain_orgs

> Org identity surfaces: picker, members, audit log, SSO config.

## Surfaces

- `/orgs` — `OrgPickerPage`. Lists member orgs (role badge), Create-org modal. Org list renders under `<ErrorBoundary>` + `<Suspense>` via `useMyOrgs` (`useSuspenseQuery`).
- `/orgs/$slug/members` — `MembersPage`. Roster, invite, role-change, remove. Roster renders under `<ErrorBoundary>` + `<Suspense>` via `useMembers` (`useSuspenseQuery`).
- `/orgs/$slug/audit` — `AuditPage`. Mutating-action log; Owner/Admin only (server enforces). Table renders under `<ErrorBoundary>` + `<Suspense>` via `useAudit` (`useSuspenseQuery`).
- SSO config — `SsoConfigPage` composed inside `domain_org_settings` `AuthSettingsPage`. Config form renders under `<ErrorBoundary>` + `<Suspense>` via `useSsoConfig` (`useSuspenseQuery`).

## Key behavior

- Picker: `useMyOrgs` → `GET /api/orgs/mine` (USER_SCOPED — no `X-Org-Slug`). Sorted alphabetically by slug.
- Create org: `POST /api/orgs` (USER_SCOPED); slug regex validated client-side. 409 → slug-taken error; 422 → slug format error.
- Members mutations (invite / role-change / remove) all invalidate `["memberships", slug]`. Remove uses `window.confirm` (ConfirmModal is a polish item).
- SSO: `GET /api/sso/config` read; PUT upserts.

## Tests

`test/org-picker.test.tsx` — component/MSW: empty state, org rows with role badges, create-org modal.

## Code

`apps/web/src/domain/orgs/{OrgPickerPage,MembersPage,AuditPage,SsoConfigPage}.tsx`.
