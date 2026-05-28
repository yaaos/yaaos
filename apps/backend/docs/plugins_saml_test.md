# plugins/saml_test

> Test-only stub SAML IdP. Refuses to load outside `YAAOS_ENV=test`.

See [core_saml.md](core_saml.md) for the full SAML SP design. `plugins/saml_test` registers a stub verifier into the assertion-verifier registry in [`domain/orgs.sso`](domain_orgs.md); the ACS handler dispatches to whichever returns a non-None payload first.

## Purpose

Issues `itsdangerous`-signed dicts standing in for real SAML Response XML so backend integration tests + Playwright SSO specs can exercise `/api/sso/{slug}/acs` without `libxmlsec1` or a live IdP.

**Never enable in production.** Module asserts `yaaos_env == "test"` at import time.

## Public interface

- `sign_assertion(payload)` — encode a stub assertion (`{"email", "name_id", ...}`).
- `verify_assertion(token)` — verify + return payload (or `None` on bad signature).

## Module architecture

`bootstrap()` registers the stub verifier at import time. Signed-token TTL: 10 minutes. Salt: `yaaos-saml-test-assertion`. Key: shared `YAAOS_OAUTH_STATE_SECRET`.

## Data owned

None.

## How it's tested

`app/domain/orgs/test/test_sso.py` drives the stub through the ACS handler. `/api/testing/saml/sign` exposes `sign_assertion` to Playwright specs via HTTP.
