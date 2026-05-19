# plugins/oauth_github

> GitHub OAuth `Provider` plugin. Owns the token-exchange + userinfo round-trips for the `github` provider id.

## Purpose

Bridges GitHub's OAuth 2.0 authorization-code flow to the `domain/identity.Provider` Protocol. Translates GitHub's `/user` + `/user/emails` payloads into a normalized `ProviderProfile`. Holds no session state, no DB ownership, no business logic about who is allowed to log in — the login orchestrator in [`domain/identity`](domain_identity.md) applies the matching / linking / hard-reject rules against the profile.

## Public interface

- `GitHubOAuthProvider` — concrete `Provider` implementation. `provider_id = "github"`.
- `bootstrap()` — registers the singleton in the in-process registry. Runs at import time from `__init__.py`. Skips registration when `yaaos_oauth_github_client_id` or `_client_secret` is unset — the LoginPage then surfaces "no providers configured" instead of redirecting to a GitHub 404 with `client_id=`.

The plugin exposes no HTTP routes of its own. `/api/auth/login?provider=github` and `/api/auth/callback/github` live in [`domain/auth`](domain_auth.md) and dispatch to whichever Provider matches the `provider` parameter.

## Module architecture

### Authorization URL

`authorization_url(state, redirect_uri)` returns `https://github.com/login/oauth/authorize?...` with `client_id`, `redirect_uri`, `scope=read:user user:email`, the caller's signed `state`, and `allow_signup=false`. The signed state binds the callback to a login attempt initiated by this backend; the orchestrator's caller (`domain/auth/web.py`) generates and verifies it via `itsdangerous.URLSafeTimedSerializer`.

### Code exchange

`exchange_code(code, redirect_uri)`:

1. `POST https://github.com/login/oauth/access_token` via `authlib.integrations.httpx_client.AsyncOAuth2Client`. `Accept: application/json` so the response is JSON, not form-encoded.
2. `GET /user` — stable `id` (becomes `external_subject`) + `name`/`login` (becomes `display_name`).
3. `GET /user/emails` — pick the row with `primary: true`; copy its `verified` bool into `email_verified`.
4. Lowercase the email before returning. Unverified is **not** rejected here; the callback handler enforces the `email_verified == true` invariant so the orchestrator never sees a tentative address.

Failure modes — `ProviderError`:
- token exchange returned non-2xx or no `access_token`,
- userinfo or emails endpoint returned non-200,
- no `primary: true` row in the emails list.

### Endpoint URLs are settings-driven

`yaaos_oauth_github_authorize_url`, `..._token_url`, `..._userinfo_url`, `..._emails_url` all live in `core/config`. Test stacks override them to point at a fake GitHub; production uses the GitHub defaults. The same indirection lets a future enterprise variant point at `https://github.enterprise.example/...`.

## Data owned

None. The plugin is stateless. Credentials (`yaaos_oauth_github_client_id`, `_client_secret`) load fresh from settings on every call so tests can `monkeypatch.setenv` between requests after calling `get_settings.cache_clear()`.

## How it's tested

`pytest-httpx` mocks the three GitHub endpoints; the real `AsyncOAuth2Client` + `httpx.AsyncClient` codepaths execute. Cases covered:

- Happy path — `external_subject`, `primary_email` (lowercased), `email_verified`, `display_name` all populated correctly.
- Unverified primary email — `email_verified=False` round-trips (callback handler is responsible for the 403).
- Userinfo 401 → `ProviderError`.
- Token endpoint 401 → `ProviderError`.

See `app/plugins/oauth_github/test/test_provider.py`.
