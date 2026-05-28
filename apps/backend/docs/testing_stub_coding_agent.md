# testing/stub_coding_agent

> Wrapper plugin that fakes any `CodingAgentPlugin` so tests run offline and deterministically.

## Purpose

Test-only `CodingAgentPlugin`. When `YAAOS_CODING_AGENT_STUB` is set, `app/web.py` calls `wrap_all_registered_plugins()`, which walks `domain/coding_agent`'s registry and replaces every entry with a `StubCodingAgentPlugin` wrapping the real one. Consumer side unchanged — `coding_agent.review("claude_code", ...)` returns the same `ReviewResult` shape; no CLI spawn, no Anthropic call. Lives in the `testing/` layer (above `plugins/`) so it can know plugin specifics that production code cannot. Excluded from production wheel builds.

## Public interface

- `StubCodingAgentPlugin`
- `wrap_all_registered_plugins`

No HTTP routes. No `bootstrap()` — testing layer is wired from `app/web.py` based on env var state, not import-time side effects.

## Module architecture

### `StubCodingAgentPlugin(wrapped)`

Thin wrapper around a real `CodingAgentPlugin`. Mirrors `meta` so the registry consumer can't tell the difference at `meta.id`. Wrapped plugin is held so `validate_config` can delegate (config-shape work is config-shape work).

- **`review`** — ignores workspace. Emits one synthetic `Finding` (`file=src/example.ts`, `line_start=1`, `severity="suggestion"`, `source_agent="yaaos-architecture"`) so UI flows that depend on a non-empty findings list have something to act against. Returns `state="COMMENT"` — decoration, not a real must-fix. When `on_activity` is supplied, emits a canned four-event sequence (`session_start`, `subagent_dispatched`, `tool_call_started`, `result`) so consumers exercise the persisted-activity-log + SSE path the same way the real CLI would.
- **`validate_config`** — passes through to the wrapped plugin. Same config-key restrictions apply in stub mode.
- **`health_check`** — `healthy=True, message="stub mode"`. Does not delegate; the wrapped plugin's real check might fail in the very environments where the stub is the point.

`_STUB_TELEMETRY` is a module-level constant: `tokens_in=1000`, `tokens_out=200`, `latency_ms=10`, `model="opus"`. Reused across all calls; visible in the audit log so specs can recognize stub-generated reviews.

### `wrap_all_registered_plugins()`

Reaches into `domain/coding_agent._PLUGINS` and swaps entries in place. The testing layer is the only thing permitted to do this (see `docs/modularity.md`). Idempotent. Returns the count; logs `stub_coding_agent.wrapped_all`. Adding a future coding-agent plugin (codex, aider) requires zero changes here.

### Why a wrapper, not a free-standing fake

Mirroring `meta` and delegating `validate_config` keeps config-schema checks against the real plugin's rules — tests stay honest about config shape while skipping the LLM round-trip. A future plugin author can't land schema changes that pass in tests but break in production.

### Companion: stub_workspace

`stub_coding_agent` short-circuits before any `workspace.run_coding_agent_cli` call, but the workspace plugin still runs `provision` (the e2e flow exercises it). `testing_stub_workspace.md` covers the matching workspace-side stub. The two activate together (`YAAOS_CODING_AGENT_STUB` + `YAAOS_WORKSPACE_STUB`).

## Data owned

None. Stub holds no DB state and never reads `claude_code_settings` — the wrapped plugin's settings are irrelevant.

## How it's tested

Unit tests in `app/testing/stub_coding_agent/test/`:

- `test_wrapper.py` — stub `review` returns the canned shape; `validate_config` delegates; `health_check` returns stub mode; `wrap_all_registered_plugins` is idempotent and replaces every registry entry with a stub mirroring its `meta`.

Exercised end-to-end by every Playwright spec in `apps/e2e/`, all of which run with `YAAOS_CODING_AGENT_STUB=1`.
