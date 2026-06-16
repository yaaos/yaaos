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
- `build_invocation` — returns a minimal stub exec block (`argv=["stub"]`, empty env) without running the real plugin.
- `parse_result` — returns a canned `RunResult`; no CLI spawn, no Anthropic call.
- `validate_settings` — always passes through `dict(settings)` unchanged. The stub deliberately skips validation so tests that exercise the full pipeline don't need valid settings; endpoint-level validation tests that need real rejection must bind `ClaudeCodePlugin` directly.

`plugin_id` mirrors the wrapped plugin's.

### `wrap_all_registered_plugins()`

Reads the current `CodingAgentRegistry` via `current_coding_agent_registry()`, builds a fresh `CodingAgentRegistry` with each entry wrapped, and binds it via `bind_coding_agent_registry()`. Idempotent — already-wrapped entries are kept as-is. Future coding-agent plugins require zero changes here.

### Why a wrapper, not a free-standing fake

Delegating `build_invocation` keeps the exec-spec shape honest — tests exercise real argv/env/stdin assembly while skipping the LLM round-trip. See [testing_fake_coding_agent.md](testing_fake_coding_agent.md) for the counterpart that needs no real plugin.

### Companion: stub_workspace

`stub_coding_agent` short-circuits the terminal-event stub path. Both stubs activate together (`YAAOS_CODING_AGENT_STUB` + `YAAOS_WORKSPACE_STUB`). See [testing_stub_workspace.md](testing_stub_workspace.md).

## Data owned

None. Never reads `claude_code_settings`.

## How it's tested

`app/testing/stub_coding_agent/test/test_wrapper.py` — canned `parse_result` shape, `build_invocation` delegation, `wrap_all_registered_plugins` idempotency.

Exercised end-to-end by every Playwright spec (`YAAOS_CODING_AGENT_STUB=1`).
