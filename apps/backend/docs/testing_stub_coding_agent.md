# testing/stub_coding_agent

> Wrapper plugin that fakes any `CodingAgentPlugin` so tests run offline and deterministically.

## Purpose

When `YAAOS_CODING_AGENT_STUB` is set, `app/web.py` calls `wrap_all_registered_plugins()` after `bootstrap()`, replacing every registry entry with a `StubCodingAgentPlugin` wrapping the real one. Consumer side unchanged — no CLI spawn, no Anthropic call. Excluded from production wheel builds.

## Public interface

- `StubCodingAgentPlugin`
- `wrap_all_registered_plugins`

No HTTP routes. No `bootstrap()` — wired from `app/web.py` via env var, not import-time side effects.

## Module architecture

### `StubCodingAgentPlugin(wrapped)`

Wraps the real plugin. Implements the full `CodingAgentPlugin` Protocol surface:
- `build_invocation` — returns a hardcoded minimal exec block (`argv=["stub"]`, `env={}`, `stdin=None`). Does **not** delegate to the wrapped plugin's `build_invocation`. Purpose: prevent any real Claude CLI launch or Anthropic IO from tests.
- `parse_result` — passes `terminal_event_payload["stdout"]` through unchanged into `RunResult.output`. Emits zero findings on its own — any downstream finding assertion requires the caller (test code or the e2e stub Go agent) to supply schema-valid stream-json in the payload. Attaches a canned `ActivityLog` (four typed `ActivityEvent` instances with real `datetime` timestamps and canonical `kind` values) and fixed stub usage counters.
- `validate_settings` — always passes through `dict(settings)` unchanged. The stub deliberately skips validation so tests that exercise the full pipeline don't need valid settings; endpoint-level validation tests that need real rejection must bind `ClaudeCodePlugin` directly.

`plugin_id` mirrors the wrapped plugin's.

### `wrap_all_registered_plugins()`

Reads the current `CodingAgentRegistry` via `current_coding_agent_registry()`, builds a fresh `CodingAgentRegistry` with each entry wrapped, and binds it via `bind_coding_agent_registry()`. Idempotent — already-wrapped entries are kept as-is. Future coding-agent plugins require zero changes here.

### Companion: stub_workspace

`stub_coding_agent` short-circuits the terminal-event stub path. Both stubs activate together (`YAAOS_CODING_AGENT_STUB` + `YAAOS_WORKSPACE_STUB`). See [testing_stub_workspace.md](testing_stub_workspace.md).

## Data owned

None. Never reads `claude_code_settings`.

## How it's tested

`app/testing/stub_coding_agent/test/test_wrapper.py` — `parse_result` stdout pass-through shape, `build_invocation` stub exec block, `wrap_all_registered_plugins` idempotency.

Exercised end-to-end by every Playwright spec (`YAAOS_CODING_AGENT_STUB=1`).
