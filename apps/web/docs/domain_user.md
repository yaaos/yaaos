# domain_user

> User-scoped settings: profile, per-org handles, GitHub link, 2FA, sessions, and OAuth connections.

## Surfaces

- `/org/$slug/user/details` — `DetailsPage`. Display name, per-org handles, verified emails, GitHub association, OAuth connections.
- `/org/$slug/user/security` — `SecurityPage`. TOTP enrollment + sign-out-all-sessions.
- `/org/$slug/user/notifications` — cross-org notifications (see [domain_notifications](domain_notifications.md)).

The `$slug` in the path is a frontend routing concern only. Backend routes (`/api/user/*`, `/api/auth/totp/*`) are `USER_SCOPED` and ignore `X-Yaaos-Org-Slug`.

## Key behavior

- `useUserMe` (`useSuspenseQuery`) → `GET /api/user/me` — source of truth; carries display name, emails, `github_username`, memberships + handles.
- Display name / handle edits use PATCH mutations; Save disabled until value differs from saved state.
- Per-org handle errors render inline per row; other rows reset cleanly.
- TOTP state is local to the page; reload re-derives from `/api/auth/me`. No shared cache.
- `useLogoutAll` lives in `domain_auth`; SecurityPage imports it.

### Connections section

`ConnectionsSection` (in `DetailsPage`) shows one `ConnectionCard` per registered OAuth app (from `GET /api/user/oauth/connections`). Hidden when the list is empty.

**Device-auth connect flow:**
1. "Connect" clicks `useStartDeviceAuth(providerId)` → opens `Dialog` with `verification_url` + `user_code`.
2. While the dialog is open, `usePollDeviceAuth` (`useQuery`) polls `POST /api/user/oauth/{id}/device-auth/poll` at the `poll_interval_seconds` cadence.
3. On `status == "connected"`, polling stops, dialog closes, `["user-oauth-connections"]` invalidated.
4. On `status == "denied"` or `"expired"`, polling stops (dialog stays open showing the outcome).

**Disconnect:** confirm dialog → `useDisconnectOAuth(providerId)` → `DELETE /api/user/oauth/{id}/connection` → `["user-oauth-connections"]` invalidated.

## Tests

- `test/details.test.tsx` — component/MSW: display name, handles, emails, GitHub username states; connections section renders not-connected card; renders connected card with Disconnect button.
- `test/security.test.tsx` — smoke: TOTP setup button and logout-all action render.

## Public interface

- `apps/web/src/domain/user/public/DetailsPage.tsx` — `DetailsPage`
- `apps/web/src/domain/user/public/SecurityPage.tsx` — `SecurityPage`
- `apps/web/src/domain/user/public/queries.ts` — `useUserMe`, `useClearGithubUsername`, `useUpdateDisplayName`, `useUpdateOrgHandle`, `useOAuthConnections`, `useStartDeviceAuth`, `usePollDeviceAuth`, `useDisconnectOAuth`, `OAuthConnectionView`, `DeviceAuthStart`, `DeviceAuthPoll`, `UserEmail`, `UserMe`, `UserMembership`
