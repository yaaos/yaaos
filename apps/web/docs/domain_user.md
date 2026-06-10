# domain_user

> User-scoped settings: profile, per-org handles, GitHub link, 2FA, sessions.

## Surfaces

- `/orgs/$slug/user/details` — `DetailsPage`. Display name, per-org handles, verified emails, GitHub association.
- `/orgs/$slug/user/security` — `SecurityPage`. TOTP enrollment + sign-out-all-sessions.
- `/orgs/$slug/user/notifications` — cross-org notifications (see [domain_notifications](domain_notifications.md)).

The `$slug` in the path is a frontend routing concern only. Backend routes (`/api/user/*`, `/api/auth/totp/*`) are `USER_SCOPED` and ignore `X-Yaaos-Org-Slug`.

## Key behavior

- `useUserMe` (`useSuspenseQuery`) → `GET /api/user/me` — source of truth; carries display name, emails, `github_username`, memberships + handles. `DetailsPage` renders under `<ErrorBoundary>` + `<Suspense>`.
- Display name / handle edits use PATCH mutations; Save disabled until value differs from saved state.
- Per-org handle errors render inline per row; other rows reset cleanly.
- TOTP state is local to the page; reload re-derives from `/api/auth/me`. No shared cache.
- `useLogoutAll` lives in `domain_auth`; SecurityPage imports it.

## Tests

- `test/details.test.tsx` — component/MSW: display name, handles, emails, GitHub username states.
- `test/security.test.tsx` — smoke: TOTP setup button and logout-all action render.

## Public interface

- `apps/web/src/domain/user/public/DetailsPage.tsx` — `DetailsPage`
- `apps/web/src/domain/user/public/SecurityPage.tsx` — `SecurityPage`
- `apps/web/src/domain/user/public/queries.ts` — `useUserMe`, `useClearGithubUsername`, `useUpdateDisplayName`, `useUpdateOrgHandle`, `UserEmail`, `UserMe`, `UserMembership`
