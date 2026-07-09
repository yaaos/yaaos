# plugins/rwx

> RWX API key validator — registers `validate_rwx_token` with `core/api_keys` at import time.

## Scope

Owns: `validate_rwx_token` (validator callable), `bootstrap` (registration side effect).

Does NOT own: API key storage (that's `core/api_keys`), credential delivery to agents (that's `core/coding_agent` + `core/agent_gateway`). Does NOT expose HTTP routes — the generic `/api/api-keys/rwx/validate` endpoint dispatched by `core/api_keys` is sufficient.

## Why / invariants

- **Validator-only plugin.** RWX credentials ride the existing `core/api_keys` encrypted-at-rest store under `provider="rwx"`. The validator probe (`validate_rwx_token`) mirrors the anthropic validator's shape: one authenticated HTTP call to the RWX API, returns `True` on 2xx, `False` on any failure. The module exists only to provide the validator — no web routes, no settings schema.
- **Bootstrap at import.** `__init__.py` calls `bootstrap()` at import time so `web.py`'s step-6 plugin import triggers validator registration. Worker does not import `plugins/rwx` (validators serve the web validate endpoint; key delivery via ConfigUpdate is validator-independent).
- **Key delivery is forward-all.** Once the org sets an `rwx` API key, `core/coding_agent.build_api_key_secrets_for_org` picks it up on every ConfigUpdate because it forwards all stored org keys. The agent injects `RWX_ACCESS_TOKEN` into claude subprocesses via `apiKeyProviderEnvVars`.

## Entry points

- `apps/backend/app/plugins/rwx/__init__.py` — `bootstrap()` call at import.
- `apps/backend/app/plugins/rwx/service.py` — `validate_rwx_token`, `bootstrap`.

## How it's tested

Service tests in `app/plugins/rwx/test/test_rwx_service.py`:
- `test_list_providers_surfaces_rwx` — `GET /api/api-keys` includes `rwx` after bootstrap.
- `test_validate_rwx_dispatches_to_registered_validator` — `POST /api/api-keys/rwx/validate` calls the registered validator callable. Outbound HTTP probe is stubbed via `register_validator` DI (no network calls).
