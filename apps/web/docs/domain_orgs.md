# domain_orgs

> Org picker — the `/orgs` route where users select a member org.

## Surfaces

- `/orgs` — `OrgPickerPage`. Lists member orgs (role badge), Create-org modal. Org list renders under `<ErrorBoundary>` + `<Suspense>` via `useMyOrgs` (`useSuspenseQuery`).

`MembersPage`, `AuditPage`, and `SsoConfigPage` are private to `domain/org_settings` — see [domain_org_settings](domain_org_settings.md).

## Key behavior

- Picker: `useMyOrgs` → `GET /api/orgs/mine` (USER_SCOPED — no `X-Yaaos-Org-Slug`). Sorted alphabetically by slug.
- Create org: `POST /api/orgs` (USER_SCOPED); slug regex validated client-side. 409 → slug-taken error; 422 → slug format error.

## Tests

`test/org-picker.test.tsx` — component/MSW: empty state, org rows with role badges, create-org modal.

## Public interface

- `apps/web/src/domain/orgs/public/OrgPickerPage.tsx` — `OrgPickerPage`
