# plugins/oauth_test

> Test-only `Provider` stub. Refuses to load outside `YAAOS_ENV=test`.

## Purpose

Lets backend integration tests + the Playwright E2E suite drive the real `/api/auth/login` + `/api/auth/callback/*` codepath without HTTP-mocking GitHub. Tests stage the identity the next callback will resolve, then drive the actual route — every byte of the login orchestrator (matching, hard-reject, link-challenge) runs unmodified.

## Public interface

- `TestOAuthProvider` — `provider_id = "test"`.
- `set_next_profile(profile)` — stage the `ProviderProfile` the next `exchange_code` call will return. Pass `None` to clear.
- `bootstrap()` — registers the singleton.

## Module architecture

### Env-gate at import time

The `service.py` module asserts `get_settings().yaaos_env == "test"` at the top of the file. Importing this module from a `dev` or `prod` process raises `AssertionError` immediately — defense-in-depth so the stub can never accidentally accept real users.

`app/web.py` only imports `plugins.oauth_test` when `yaaos_env == "test"`. The conftest sets `YAAOS_ENV=test` before any app import so the suite picks it up. The Playwright "Sign in (test)" button is only rendered when the providers endpoint reports `test` in its list — which only happens in the test env.

### Authorization URL

`authorization_url(state, redirect_uri)` short-circuits the GitHub round-trip: it returns `redirect_uri?code=test-code&state=<state>`. The browser follows the redirect, lands on `/api/auth/callback/test`, and the callback handler proceeds as normal.

### Exchange

`exchange_code(code, redirect_uri)` returns the profile previously staged via `set_next_profile`. Calling without staged data raises — tests must opt into a profile each time.

## Data owned

A single module-global `_NEXT_PROFILE` slot. Tests reset it via `set_next_profile(None)` when they care about isolation (the orchestrator typically reads it once per test so leakage is rare in practice).

## How it's tested

`app/plugins/oauth_test/test/test_provider.py` — sanity coverage that the stub registers, that `authorization_url` echoes the state, and that staged profiles round-trip. End-to-end coverage of the full callback flow (linking, hard-reject, invitation acceptance) lives in `app/domain/sessions/test/test_oauth_endpoints.py` and drives the stub indirectly.
