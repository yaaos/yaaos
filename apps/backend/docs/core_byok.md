# core/byok

> Encrypted at-rest storage for per-org provider API keys.

## Purpose

Owns the `byok_keys` table. Stores one row per `(org_id, provider)` with the API key encrypted via [core/secrets](core_secrets.md). Lets the Claude Code plugin (and future LLM-using plugins) read a customer-supplied key at request time without ever surfacing it to the UI in plaintext.

## Public interface

- `get(org_id, provider) -> str | None` — decrypts and returns the stored key, or `None` if no row exists. Raises `ByokDecryptError` if the row is corrupt.
- `set(org_id, provider, plaintext, *, actor)` — encrypts via `core/secrets` and upserts. Audit: `byok.set`.
- `clear(org_id, provider, *, actor) -> bool` — removes the row. Returns True if a row was removed. Audit: `byok.cleared` (only when something was actually removed).
- `validate(org_id, provider, validator, *, actor) -> bool` — decrypts and hands plaintext to a caller-supplied `Awaitable[bool]` callable. Provider-specific HTTP logic lives in the validator, not here. On success: stamps `last_validated_at`. Audit: `byok.validated` with `{provider, success}`.
- `list_keys_for_org(org_id, *, session) -> list[ByokKey]` — returns metadata (no plaintext) for all stored keys belonging to an org. Returns `ByokKey` value objects with `org_id`, `provider`, and timestamp fields.

All functions accept a `session` so callers can join an outer transaction.

`ByokKey` — Pydantic value object: `org_id`, `provider`, `last_validated_at`, `last_used_at`, `updated_at`, `created_at`. No plaintext field.

## Module architecture

- Single table, no nested entities.
- The validator-callable pattern keeps `core/byok` free of provider-specific HTTP code: `plugins/claude_code` (or a future provider plugin) supplies its own minimal API call.
- Plaintext crosses this module's boundary in only two directions — into `set()` and out of `get()`/`validate()`. Callers must not log the result.
- **Write-only public API.** No endpoint returns a stored key. The list endpoint emits `status` + timestamps only; clients display "Configured ✓ · last set <updated_at>" with Rotate/Clear actions rather than offering a reveal toggle.

## Data owned

- `byok_keys` — `(org_id, provider) PK`, `encrypted_value text`, `last_validated_at`, `last_used_at`, `created_at`, `updated_at`. Encryption: `core/secrets.encrypt` on write; `decrypt` on read.

## How it's tested

`test/test_service.py` covers: set/get round-trip, overwrite, clear (with the rowcount-based audit gating), validate (success + failure + missing-key), audit-row emission for each mutation, empty-input rejection, and `list_keys_for_org` org-isolation (two orgs seeded; asserts each org sees only its own keys).
