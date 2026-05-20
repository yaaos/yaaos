# core/byok

> Encrypted at-rest storage for per-org provider API keys.

## Purpose

Owns the `byok_keys` table. Stores one row per `(org_id, provider)` with the API key encrypted via [core/secrets](core_secrets.md). Lets the Claude Code plugin (and future LLM-using plugins) read a customer-supplied key at request time without ever surfacing it to the UI in plaintext.

Skeleton only at Phase 0 — table + module shell exist; the `get`/`set`/`clear`/`validate` service surface lands in Phase 2.

## Public interface

Planned (Phase 2):

- `get(org_id, provider) -> str | None` — decrypts and returns the stored key, or `None`.
- `set(org_id, provider, plaintext) -> None` — encrypts via `core/secrets` and upserts.
- `clear(org_id, provider) -> None` — removes the row.
- `validate(org_id, provider, validator: Callable[[str], Awaitable[bool]]) -> bool` — runs a provider-supplied callable against the decrypted key; stamps `last_validated_at` on success.

Provider plugins (Anthropic via `plugins/claude_code` for M03) supply their own validator callable so this module stays free of provider-specific HTTP logic.

## Module architecture

Single table, no nested entities. All audit-log entries (`byok.set`, `byok.cleared`, `byok.validated`) are emitted at the service layer.

## Data owned

- `byok_keys` — `(org_id, provider) PK`, `encrypted_value text`, `last_validated_at`, `last_used_at`, `created_at`, `updated_at`. Encryption: `core/secrets.encrypt` on write; `decrypt` on read.

## How it's tested

Tests land alongside the Phase 2 implementation: round-trip set/get, clear, validate-callable invocation, audit-row writes.
