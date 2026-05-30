# domain/coding_agent

> Vendor-neutral abstraction over coding-agent CLIs — Protocol, registry, dispatch, and shipped subagent prompt content.

## Scope

Owns: `CodingAgentPlugin` Protocol, per-mode context/result types, telemetry enums, plugin registry, typed exception hierarchy, shipped `reviewers/*.md` prompt content.

Does NOT own: prompt assembly, output-format choice (plugin concerns), workspace mechanics.

Lives in `domain/` (not `core/`) because return types reference `vcs.Finding` and `lessons.Lesson`.

## Why / invariants

- **Status-not-exception contract:** task methods MUST NOT raise on agent-level failures (timeout, bad JSON, non-zero exit) — those become `status` + `error_message`. Only infrastructure errors (`WorkspaceExecError`, etc.) are raised. Consumers (`reviewer`) branch on `result.status`.
- **Five named task methods, not a generic `invoke`:** `review`, `incremental_review`, `verify_fix`, `stale_check`, `answer_question`. Adding a mode requires a Protocol change.
- **Prompts live in the caller, not here.** `domain/reviewer/llm/prompts/` owns the words; `coding_agent` owns the contract.
- Subagent markdown (`reviewers/*.md`) is plugin-agnostic — describes *what to check* and the JSON output schema. Plugins wrap it in their native format at bootstrap.

## `CodingAgentPlugin` Protocol

Signatures in `app/domain/coding_agent/types.py`. Each task method takes a `Workspace`, a mode-specific Pydantic context, and an optional `OnActivity` callback.

- `review` — full base..head diff → `list[FindingDraft]`; reviewer applies admission + converts to `vcs.Finding`.
- `incremental_review` — `prev_sha..head` only → `list[FindingDraft]`.
- `verify_fix` — original finding + original code + current code → still-present verdict.
- `stale_check` — original finding + current code + diff summary → still-applies verdict.
- `answer_question` — finding + anchor code + thread history + question → `answer: str`. No verdict, no state transition.

## Registry

`app/domain/coding_agent/service.py`. Process-global `_registry` keyed by `plugin.meta.id`. `register_plugin` rejects duplicates; `scoped_coding_agent(plugin)` (in `service.py`, not re-exported from the package) is the test-safe context manager — tests reach it via direct submodule import. `clear_coding_agent_plugins()` in `app/testing/seed` is used by testing helpers (`fake_coding_agent`, `stub_coding_agent`) that manage full registry snapshots. See [patterns.md § scoped_* context managers](patterns.md#scoped_-context-managers-for-import-time-registries).

## Subagent prompt files (`reviewers/`)

Six markdown files: `architecture.md`, `security.md`, `line-level.md`, `tests.md`, `docs.md` (all always-on); `skill.md` (conditional — only when the diff touches `**/SKILL.md` or `.claude/skills/**`). Plugin `plugins/claude_code` reads these at bootstrap and installs them in its native subagent format.

## Data owned

None. Registry is in-memory; subagent content is shipped markdown.

## How it's tested

`app/domain/coding_agent/test/test_registry.py` — register/get/duplicate-rejection, dispatcher logging, `validate_config` forwarding, `health_check_all` exception-to-unhealthy. Plugin-specific behaviour in `app/plugins/<plugin>/test/`.
