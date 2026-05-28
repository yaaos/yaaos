# plugins/oauth_test

> Test-only `Provider` stub. Refuses to load outside `YAAOS_ENV=test`.

## Purpose

Lets backend integration tests + Playwright specs drive the real `/api/auth/login` + `/api/auth/callback/*` codepath without HTTP-mocking GitHub. Tests stage the identity the next callback resolves; every byte of the login orchestrator (matching, hard-reject, link-challenge) runs unmodified.

**Never enable in production.** `service.py` asserts `yaaos_env == "test"` at import time — importing from a `dev` or `prod` process raises immediately. `app/web.py` only imports this module when `yaaos_env == "test"`. The Playwright "Sign in (test)" button is only rendered when the providers endpoint reports `test` — which only happens in test env.

## Public interface

- `TestOAuthProvider` — `provider_id = "test"`.
- `set_next_profile(profile)` — stage the `ProviderProfile` the next `exchange_code` returns. `None` clears.
- `bootstrap()` — registers the singleton.

## Module architecture

- `authorization_url(state, redirect_uri)` → `redirect_uri?code=test-code&state=<state>`. Short-circuits the GitHub round-trip; browser lands on `/api/auth/callback/test` directly.
- `exchange_code(code, redirect_uri)` → returns the staged profile. Raises if nothing staged.
- Module-global `_NEXT_PROFILE` slot. Tests call `set_next_profile(None)` for isolation when needed.

## How it's tested

`app/plugins/oauth_test/test/test_provider.py` — sanity coverage (registers, echoes state, profiles round-trip). Full callback flow coverage in `app/core/sessions/test/test_oauth_endpoints.py`.
