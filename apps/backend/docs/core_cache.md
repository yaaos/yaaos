# core/cache

> Provider-neutral records, presence, expiring claims, approximate rate limits, and JSON event delivery.

## Scope

- Receives caller-owned keys, typed record patches, membership timestamps, limits, and events; returns ordinary data and delivery outcomes through [the public interface](../app/core/cache/__init__.py).
- Owns the private provider contract, validation/error translation, scoped test substitution, and connection/subscription cleanup. Redis is the default provider; its SDK and configuration remain private.
- Does not own key/channel naming, authorization, rate-limit policy, event schemas, or the separately connected [task broker](core_tasks.md). [Agent gateway](core_agent_gateway.md) and [SSE](core_sse.md) own their application flows.

## Why / invariants

- Record patches use a Pydantic model's JSON-mode dump with unset fields excluded; only flat string fields/values reach storage. An empty patch performs no I/O, missing reads return an empty record, and unspecified fields survive updates.
- Membership refresh updates one member's last-seen timestamp without double-counting; stale pruning includes the cutoff. Timestamps must be finite and nonnegative; TTLs, limits, and windows are strictly positive integers, never booleans.
- Namespace prefixes are literal. The Redis implementation escapes pattern metacharacters, checks the literal prefix again, deduplicates scan results, and counts actual deletions.
- Redis preserves HASH records, timestamp-scored ZSET membership, and atomic expiring claims containing `1`, so independent writers share existing storage. Claim duplicates do not refresh expiry; rejected rate-limit hits are not stored and do not refresh expiry.
- Rate limiting remains approximate and non-atomic, using timestamp/count members. Concurrent callers can exceed the limit; stricter enforcement is not promised.
- Subscription readiness follows consumption of the subscribe acknowledgement, not merely sending the command. Nested generators use `aclosing`; setup failure, cancellation, and explicit iterator close release resources and balance local counts.
- Operational exceptions become sanitized `CacheError` without vendor exception chaining. Invalid input raises `CacheValidationError` before I/O; health alone returns false on provider failure. Logs do not include provider addresses, keys, or cached payloads.
- One process-owned default contains no connections; clients open lazily per event loop. The ContextVar stores only scoped overrides, using an immutable sentinel for the default. Shutdown registers with both process lifecycles, closes resources idempotently, and neither opens unused clients nor clears shared storage. Teardown tolerates transport-close errors after the owning loop has already closed; failures on live loops remain neutral errors.

## Gotchas

- Events are at-most-once with no replay. Malformed JSON, invalid text, and non-object payloads are skipped; `subscriber_count` is a process-local diagnostic, while publish returns the provider's delivery count.
- `subscribe` is a synchronous iterator factory; use `aclosing` when consuming only part of its async iterator so cleanup is awaited.
- `set_cache_for_tests` restores nested bindings even on errors, not persisted data. Its default is real Redis; memory is test-only; recording captures publications without forwarding them to listeners, while preserving the bound provider's other behavior. Close opened resources before leaving a provider scope.
- No client, provider accessor, startup binder, command passthrough, or compatibility interface is exported. Follow the [cardinal rule](patterns.md#cardinal-rule--never-export-the-instance).

## Vocabulary

- **Record** — caller-owned named string fields, updated partially.
- **Membership** — immutable member identity and last-seen timestamp.
- **Claim** — one winner for a named key until its original expiry.

## Entry points

- [Facade and scoped bindings](../app/core/cache/service.py), [value objects and private protocol](../app/core/cache/types.py), [shutdown](../app/core/cache/shutdown.py).
- [Redis provider and acknowledged subscriptions](../app/core/cache/pubsub.py); its private storage helpers preserve physical representations.
- [Memory provider](../app/core/cache/memory.py) and [recording decorator](../app/core/cache/recording.py).
- [Contract tests](../app/core/cache/test/test_contract.py), [event tests](../app/core/cache/test/test_pubsub.py), [failure injection](../app/core/cache/test/test_provider_failures.py), and [mixed-writer layouts](../app/core/cache/test/test_storage_layout.py). Real-provider coverage uses `redis_or_skip`; CI must inspect skips when Redis is required.
