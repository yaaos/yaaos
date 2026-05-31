# testing/fake_coding_agent

> Standalone `CodingAgentPlugin` fake for tests that need a registered plugin without wrapping a real one.

## Purpose

`stub_coding_agent` wraps an already-registered real plugin (e2e stack). `fake_coding_agent` is the opposite: a self-contained `CodingAgentPlugin` impl that tests register on the fly under any `plugin_id`. Used by service tests driving workflows through reviewer Workspace commands (`CodeReview`, `IncrementalReview`, `VerifyFix`, `StaleCheck`, `AnswerQuestion`) when no real plugin is bootstrapped.

## Public interface

- `FakeCodingAgentPlugin(plugin_id="claude_code")` — instantiate directly; set return-value attributes to drive outcomes.
- `register_fake_coding_agent(plugin_id="claude_code")` — context manager. Binds a fresh `CodingAgentRegistry` copy with the fake substituted, yields the fake for setup/assertions, restores the prior registry binding on exit.

## Module architecture

Each agent method returns a deterministic, schema-valid result. Tests mutate public attributes (`review_findings`, `verify_fix_still_present`, `stale_still_applies`, `answer_text`, …). Each call captures its context in `last_*_context` for assertions.

Telemetry is a zero constant — tests that need real telemetry use `stub_coding_agent`.

## Why it exists separately from `stub_coding_agent`

`stub_coding_agent` is production-shaped: preserves the real plugin's `meta` + `validate_config` so e2e flows exercise real config validation. `fake_coding_agent` is test-shaped: zero coupling to a real plugin; lets a unit test register a `claude_code` plugin into an otherwise empty registry. The two never both register the same id.

## Data owned

None. In-memory return-value attributes per instance; restored on context-manager exit.

## How it's tested

Exercised indirectly by reviewer service tests (`app/domain/reviewer/test/test_pr_review_v1_e2e_service.py`).
