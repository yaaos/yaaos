# testing/stub_coding_agent

> Wrapper plugin that fakes any `CodingAgentPlugin` so tests run offline and deterministically.

## Purpose

When `YAAOS_CODING_AGENT_STUB` is set, `app/web.py` calls `wrap_all_registered_plugins()` after `bootstrap()`, replacing every registry entry with a `StubCodingAgentPlugin` wrapping the real one. Consumer side unchanged — `coding_agent.review("claude_code", ...)` returns the same `ReviewResult` shape; no CLI spawn, no Anthropic call. Excluded from production wheel builds.

## Public interface

- `StubCodingAgentPlugin`
- `wrap_all_registered_plugins`

No HTTP routes. No `bootstrap()` — wired from `app/web.py` via env var, not import-time side effects.

## Module architecture

### `StubCodingAgentPlugin(wrapped)`

Mirrors `meta` from the real plugin. Holds the wrapped instance for `validate_config` delegation.

- **`review`** — emits one synthetic `Finding` (`file=src/example.ts`, `line_start=1`, `severity="suggestion"`, `source_agent="yaaos-architecture"`, `state="COMMENT"`). When `on_activity` is supplied, emits a canned four-event sequence (`session_start`, `subagent_dispatched`, `tool_call_started`, `result`) so consumers exercise the activity-log + SSE path.
- **`validate_config`** — delegates to wrapped plugin. Config-key restrictions apply in stub mode.
- **`health_check`** — `healthy=True, message="stub mode"`. Does not delegate; the real check might fail in exactly the environments where the stub is the point.

`_STUB_TELEMETRY`: `tokens_in=1000`, `tokens_out=200`, `latency_ms=10`, `model="opus"`. Constant; visible in audit log so specs can recognize stub-generated reviews.

### `wrap_all_registered_plugins()`

Reads the current `CodingAgentRegistry` via `current_coding_agent_registry()`, builds a fresh `CodingAgentRegistry` with each entry wrapped, and binds it via `bind_coding_agent_registry()`. Idempotent — already-wrapped entries are kept as-is. Future coding-agent plugins require zero changes here.

### Why a wrapper, not a free-standing fake

Delegating `validate_config` keeps config-schema checks against real rules — tests stay honest about config shape while skipping the LLM round-trip. See [testing_fake_coding_agent.md](testing_fake_coding_agent.md) for the counterpart that needs no real plugin.

### Companion: stub_workspace

`stub_coding_agent` short-circuits before `workspace.run_coding_agent_cli`, but the workspace plugin still runs `provision`. Both stubs activate together (`YAAOS_CODING_AGENT_STUB` + `YAAOS_WORKSPACE_STUB`). See [testing_stub_workspace.md](testing_stub_workspace.md).

## Data owned

None. Never reads `claude_code_settings`.

## How it's tested

`app/testing/stub_coding_agent/test/test_wrapper.py` — canned review shape, `validate_config` delegation, stub `health_check`, `wrap_all_registered_plugins` idempotency.

Exercised end-to-end by every Playwright spec (`YAAOS_CODING_AGENT_STUB=1`).
