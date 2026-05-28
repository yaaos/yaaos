# domain_auth

> Login page + logout. Email-first SSO-discover flow.

## Surfaces

- `/login` — `LoginPage`. Email field, on Continue the SPA calls `useSsoDiscover` (`POST /api/auth/sso/discover`) and renders the matching provider button. Falls back to a multi-provider panel (preserves `data-testid="login-test"` for e2e) when discover returns no preferred provider.
- Logout — `useLogoutAll` mutation, fired from the User Card popover in the sidebar.

## Data flow

- `useSsoDiscover` — `POST /api/auth/sso/discover` with `{email}`. Returns `{provider: "github"}` for any well-formed email.
- GitHub button POSTs to `/api/sso/start` (carries CSRF) and redirects.
- TOTP step (when SSO returns a 2FA challenge) renders inline; not a separate route.

## State / contract

- No client cache — Login mounts before any user identity is known.
- Provider button presence drives the `data-testid="login-test"` panel; that testid is the e2e contract for "is the login page rendered?"

## Where the code lives

- `apps/web/src/domain/auth/LoginPage.tsx`
- `apps/web/src/domain/auth/index.ts` re-exports `useLogoutAll` for cross-domain callers (Account → Security).
