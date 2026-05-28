# domain_auth

> Login page + logout. Email-first SSO-discover flow.

## Surfaces

- `/login` — `LoginPage`. Email field → `useSsoDiscover` (`POST /api/auth/sso/discover`) → provider button. Falls back to multi-provider panel when discover returns no preferred provider.
- Logout — `useLogoutAll` mutation; fired from the sidebar User Card popover. Re-exported from `domain/auth/index.ts` for cross-domain callers.

## Key behavior

- No client cache — mounts before any user identity is known.
- GitHub button POSTs to `/api/sso/start` (carries CSRF) then redirects; TOTP challenge renders inline, not a separate route.
- `data-testid="login-test"` panel is the e2e contract for "login page is rendered."

## Code

`apps/web/src/domain/auth/LoginPage.tsx`, `apps/web/src/domain/auth/index.ts`.
