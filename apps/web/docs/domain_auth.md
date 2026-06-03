# domain_auth

> Login page + logout. Email-first SSO-discover flow.

## Surfaces

- `/login` — `LoginPage`. Email-first SSO discovery form (`react-hook-form` + Zod; schema: `email: z.string().email()`). Submit calls `useSsoDiscover`; on a SAML hit, renders the SAML button. Falls back to multi-provider panel when discover returns no preferred provider. Provider buttons load under `<ErrorBoundary>` + `<Suspense>` via `useProviders` (`useSuspenseQuery`).
- `RequireMembership` — renders `children` only when the authenticated user has at least `role` in `orgSlug`. Uses `useCurrentUser` (`useSuspenseQuery`); suspends while the auth check is in flight. Server `require()` is the authority — this is UI hinting only.
- Logout — `useLogoutAll` mutation; fired from the sidebar User Card popover. Re-exported from `domain/auth/index.ts` for cross-domain callers.

## Key behavior

- GitHub button POSTs to `/api/sso/start` (carries CSRF) then redirects; TOTP challenge renders inline, not a separate route.
- `data-testid="login-test"` panel is the e2e contract for "login page is rendered."
- `useCurrentUser` and `useProviders` both use `useSuspenseQuery`; callers must render under `<Suspense>`.

## Tests

`domain/auth/test/login.test.tsx` — component/MSW: GitHub button renders, SSO discovery flow, no-providers fallback.

## Code

`apps/web/src/domain/auth/LoginPage.tsx`, `apps/web/src/domain/auth/RequireMembership.tsx`, `apps/web/src/domain/auth/index.ts`.
