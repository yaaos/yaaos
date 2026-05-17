# domain/coding_agent

> Vendor-neutral abstraction over coding-agent CLIs (Claude Code, future: Codex, Aider) — Protocol, registry, dispatch, shipped subagent prompt content.

## Purpose

The contract between yaaos and external agent CLIs. Owns the `CodingAgentPlugin` Protocol with four targeted task methods (`review`, `incremental_review`, `verify_fix`, `stale_check`) — not a generic `invoke`. Owns structured input contexts and vendor-neutral output types per mode, telemetry/status enums, the plugin registry, and the typed exception hierarchy. Owns **zero prompt assembly** and **zero output-format choice** — plugin concerns.

Also owns the **shipped reviewer subagent prompt content** under `reviewers/*.md`. The markdown is plugin-agnostic ("what to check"); each plugin's installer wraps it in its own native format (Claude Code frontmatter, etc.) and writes it to the right place.

Lives in `domain/` (not `core/`) because return types reference `vcs.Finding` and `memory.Lesson`. See [`modularity.md`](modularity.md).

## Public interface

Exported from `app/domain/coding_agent/__init__.py`:

- Shared types — `InvocationStatus`, `InvocationTelemetry`, `ValidationResult`, `HealthStatus`, `ActivityEvent`, `OnActivity`, `Severity`, `FindingAnchor`, `FindingDraft`.
- Per-mode contexts/results — `ReviewContext`/`ReviewResult`, `IncrementalReviewContext`/`IncrementalReviewResult`, `VerifyFixContext`/`VerifyFixResult`, `StaleCheckContext`/`StaleCheckResult`.
- Protocol — `CodingAgentPlugin`.
- Registry/dispatch — `register_coding_agent_plugin`, `get_plugin`, `registered_plugin_ids`, `review`, `incremental_review`, `verify_fix`, `stale_check`, `validate_config`, `health_check_all`, `_reset_plugins_for_tests`, `_PLUGINS`.
- Exceptions — `CodingAgentError`, `PluginNotFoundError`, `CodingAgentCacheMiss`.

No HTTP routes.

## Module architecture

### Types (`types.py`)

- `ReviewContext` — `pr`, `diff`, `lessons`, optional `language_hint`, `prior_yaaos_comment_bodies`, `agent_config`. No persona, no agent_name — the parent reviewer's prompt and subagent definitions are shipped by the plugin layer.
- `InvocationStatus` — `SUCCESS` / `PARSE_FAILURE` / `AGENT_ERROR` / `TIMEOUT`.
- `InvocationTelemetry` — `tokens_in`, `tokens_out`, `latency_ms`, `raw_output`, `raw_stderr`, `model` (resolved name reported by the CLI on completion). Cost is not tracked — CLI pricing data is not authoritative.
- `ReviewResult` — `status`, `findings` (already `vcs.Finding`s with `source_agent` populated by the parent's synthesis pass — consumers wrap them in a `vcs.Review` and call `vcs_plugin.post_review`), optional `state` / `summary_body`, `lesson_ids_consulted`, `telemetry`, optional `error_message`.
- `ValidationResult` — `valid`, `errors`.
- `ActivityEvent` — `{ts, kind, message, detail}` — one pre-rendered user-facing event from the coding-agent stream. `message` is rendered by the plugin so consumers (and the FE) don't interpret raw CLI shapes.
- `OnActivity` — `Callable[[ActivityEvent], Awaitable[None]]`. Optional callback on `review`; called once per parsed stream event.

### `CodingAgentPlugin` Protocol

Async task methods `review`, `incremental_review`, `verify_fix`, `stale_check`, plus `validate_config`, `health_check`, and `meta: PluginMeta`. Each task method takes a `Workspace`, a mode-specific Pydantic context, and an optional `OnActivity` callback. Signatures in `app/domain/coding_agent/types.py`.

- `review` — full base..head diff. Returns `vcs.Finding`s (consumer wraps them in `vcs.Review`).
- `incremental_review` — `prev_sha..head` only. Returns `FindingDraft`s; the reviewer aggregate applies schema/severity gating, caps, dedup, and persists.
- `verify_fix` — given an original finding + original code + current code at the resolved anchor, decides whether the issue is still present.
- `stale_check` — given an original finding + current code + a diff summary, decides whether the finding still applies.

All task methods MUST NOT raise on agent-level failures (timeout, non-zero exit, malformed JSON) — those become `status` + `error_message` so consumers branch on the same surface. Only infrastructure failures (e.g., `WorkspaceExecError`) are raised.

Prompts and structured-output schemas for each mode live in the **calling domain module** (today: `domain/reviewer/llm/prompts/`), not here — `coding_agent` owns the contract; the caller owns the words.

### Subagent prompt content (`reviewers/`)

Six markdown files describe what each shipped subagent reviews. Plugin-agnostic — they describe *what to check*, the JSON output schema, and discipline rules. No vendor-specific syntax in the body.

- `architecture.md` — module boundaries, patterns, abstractions, CLAUDE.md adherence (always-on).
- `security.md` — auth, injection, secrets, crypto misuse (always-on).
- `line-level.md` — per-line correctness, idioms, code-level patterns including "no mocks in tests" (always-on).
- `tests.md` — test presence and quality for new behavior (always-on).
- `docs.md` — documentation sync per CLAUDE.md rule (always-on).
- `skill.md` — Claude Code Skill file validation (conditional: only when the diff touches `**/SKILL.md` or `.claude/skills/**`).

Plugins (today: `plugins/claude_code`) read these at bootstrap, wrap them in their native subagent format, and install them where their CLI expects to find them.

### Registry + dispatch (`service.py`)

Process-global `_PLUGINS` keyed by `plugin.meta.id`. `register_coding_agent_plugin` rejects duplicates. `review` is a thin wrapper that resolves the plugin, forwards the call, and emits an `agent.reviewed` log line carrying telemetry. No retry, no fallback — caller policy. `health_check_all` converts any raised exception to an unhealthy `HealthStatus`. `_reset_plugins_for_tests()` clears the registry.

### Failure model

The status-not-exception contract means a malformed JSON response or a timeout becomes `ReviewResult(status=PARSE_FAILURE, …)`, not a raised exception. Consumers (`reviewer`) branch on `result.status` to decide whether to mark the job failed, retry, or surface partial output.

## Data owned

None. Registry is in-memory. Subagent prompt content is shipped markdown.

## How it's tested

`app/domain/coding_agent/test/test_registry.py` — register/get/duplicate-rejection, dispatcher logging, `validate_config` forwarding, `health_check_all` exception-to-unhealthy. Uses a fake plugin. Plugin-specific behaviour covered by each plugin's tests under `app/plugins/<plugin>/test/`.
