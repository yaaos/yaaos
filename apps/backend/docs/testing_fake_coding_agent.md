# testing/fake_coding_agent

> Standalone `CodingAgentPlugin` fake for tests that need a registered plugin without wrapping a real one.

## Purpose

`stub_coding_agent` wraps an already-registered real plugin (used when `YAAOS_CODING_AGENT_STUB=1` in the e2e stack). `fake_coding_agent` is the opposite: a self-contained `CodingAgentPlugin` impl that tests register on the fly under any `plugin_id`. Used by service tests that drive a workflow through the reviewer Workspace commands (`CodeReview`, `IncrementalReview`, `VerifyFix`, `StaleCheck`, `AnswerQuestion`) when no real plugin is bootstrapped.

## Public interface

- `FakeCodingAgentPlugin(plugin_id="claude_code")` — instantiate directly when you want to drive specific return values.
- `register_fake_coding_agent(plugin_id="claude_code")` — context manager. Registers a `FakeCodingAgentPlugin` under `plugin_id` in `domain/coding_agent._PLUGINS`, yields the instance for setup + assertions, restores prior registration on exit.

## Module architecture

Each agent method (`review`, `incremental_review`, `verify_fix`, `stale_check`, `answer_question`) returns a deterministic, schema-valid result. Tests mutate public attributes on the registered instance (`review_findings`, `verify_fix_still_present`, `stale_still_applies`, `answer_text`, …) to drive specific outcomes. Each call captures its context in a `last_*_context` attribute so tests can assert what the command body actually sent.

Telemetry is a module-level zero (`tokens_in=0, tokens_out=0, latency_ms=0`) — tests that care about telemetry use real plugins or `stub_coding_agent`.

## Why it exists separately

`stub_coding_agent` is *production-shaped*: it preserves the real plugin's `meta` + `validate_config` so e2e flows exercise the same config validation production runs. The fake is *test-shaped*: zero coupling to a real plugin, lets a unit test register a `claude_code` plugin into a registry that would otherwise be empty (no bootstrap). The two never both register the same id.

## Data owned

None. The fake holds in-memory return-value attributes per instance; restored on context-manager exit.

## How it's tested

Indirectly: exercised by the reviewer service tests (`app/domain/reviewer/test/test_pr_review_v1_e2e_service.py`) that walk a workflow through the Workspace command bodies.
