# plugins/saml_test

> Test-only stub SAML IdP. Refuses to load outside `YAAOS_ENV=test`.

See [plugins_saml.md](plugins_saml.md) for the full SAML SP design — `plugins/saml_test` and `plugins/saml` register into the same assertion-verifier registry in [`domain/orgs.sso`](domain_orgs.md) and the orgs ACS handler dispatches to whichever returns a non-None payload first.

## Purpose

Issues `itsdangerous`-signed dicts standing in for real SAML Response XML so backend integration tests + the Phase 12 Playwright spec can exercise `/api/sso/{slug}/acs` without spinning up `libxmlsec1` and a live IdP. The orchestration code in `domain/orgs.sso_web` reads the verified payload identically regardless of the source — the registry hides the shape difference.

## Public interface

- `sign_assertion(payload)` — encode the next stub assertion (`{"email", "name_id", ...}`).
- `verify_assertion(token)` — verify + return the payload (or `None` on bad signature).
- Module asserts `yaaos_env == "test"` at import time.

## Module architecture

`bootstrap()` registers the stub verifier in `domain/orgs.sso` at import time. The signed-token TTL is 10 minutes. Salt: `yaaos-saml-test-assertion`. Keying: shares `YAAOS_OAUTH_STATE_SECRET` (tests don't need a separate key).

## Data owned

None.

## How it's tested

`app/domain/orgs/test/test_sso.py` drives the stub end-to-end through the ACS handler. The `/api/testing/saml/sign` helper exposes `sign_assertion` to the Playwright spec via HTTP.
