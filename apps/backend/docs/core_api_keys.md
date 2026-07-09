# core/api_keys

> Encrypted at-rest storage for per-org provider API keys.

## Scope

- Owns: `org_api_keys` table; `get`, `set`, `clear`, `validate`, `list_keys_for_org`, `register_on_change`.
- Plaintext crosses the boundary in only two directions: into `set()` and out of `get()`/`validate()`.

## Why / invariants

**Write-only public API** — no endpoint returns a stored key. `list_keys_for_org` returns metadata (`ApiKey`: `org_id`, `provider`, timestamps) only. UI displays "Configured ✓ · last set \<updated_at\>" with Rotate/Clear actions.

**`validate(...)` pattern** — caller supplies an `Awaitable[bool]` validator; provider-specific HTTP stays out of this module. On success: stamps `last_validated_at`. Audit: `api_key.validated {provider, success}`.

**`clear()` audits conditionally** — emits `api_key.cleared` only when a row was actually removed (rowcount check).

**`register_on_change(cb)` fan-out** — any caller may register an async `(org_id, *, session) -> None` callback invoked after every successful `set()` or `clear()`. Used by `core/coding_agent` to trigger `enqueue_config_update_for_all_org_agents` so agents receive fresh `api_keys` on key rotation without polling. Callbacks run in the caller's transaction — they must only enqueue work (e.g. outbox rows), never block.

**Callers must not log the result of `get()`.**
