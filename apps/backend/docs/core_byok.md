# core/byok

> Encrypted at-rest storage for per-org provider API keys.

## Scope

- Owns: `byok_keys` table; `get`, `set`, `clear`, `validate`, `list_keys_for_org`.
- Plaintext crosses the boundary in only two directions: into `set()` and out of `get()`/`validate()`.

## Why / invariants

**Write-only public API** — no endpoint returns a stored key. `list_keys_for_org` returns metadata (`ByokKey`: `org_id`, `provider`, timestamps) only. UI displays "Configured ✓ · last set \<updated_at\>" with Rotate/Clear actions.

**`validate(...)` pattern** — caller supplies an `Awaitable[bool]` validator; provider-specific HTTP stays out of this module. On success: stamps `last_validated_at`. Audit: `byok.validated {provider, success}`.

**`clear()` audits conditionally** — emits `byok.cleared` only when a row was actually removed (rowcount check).

**Callers must not log the result of `get()`.**

