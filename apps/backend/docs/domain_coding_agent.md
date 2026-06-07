# domain/coding_agent

> Vendor-neutral abstraction over coding-agent CLIs — Protocol, registry, dispatch, and shipped subagent prompt content.

## Scope

Owns: `CodingAgentPlugin` Protocol, per-mode context/result types (`ExecSpec`, `Invocation`, `ReviewContext`, …), telemetry enums, plugin registry, typed exception hierarchy, shipped `reviewers/*.md` prompt content.

Does NOT own: prompt assembly, output-format choice (plugin concerns), workspace mechanics.

Lives in `domain/` (not `core/`) because return types reference `domain/lessons.Lesson` in the in-process path.

## Why / invariants

- **Status-not-exception contract:** in-process task methods (`review`, `incremental_review`, …) MUST NOT raise on agent-level failures (timeout, bad JSON, non-zero exit) — those become `status` + `error_message`. Only infrastructure errors (`WorkspaceExecError`, etc.) are raised. Consumers (`reviewer`) branch on `result.status`.
- **Five named in-process methods, plus three remote-dispatch methods.** Five in-process task methods (`review`, `incremental_review`, `verify_fix`, `stale_check`, `answer_question`) and three remote-dispatch methods (`build_review_invocation`, `parse_review_output`, `review_preflight_steps`). Adding a mode requires a Protocol change.
- **Remote path: plugin owns exec spec + parse; caller dispatches.** `build_review_invocation` returns a typed `Invocation{kind, exec: ExecSpec, limits}` — the exact command the Go agent spawns. `parse_review_output` receives the agent's raw stream-json stdout and returns `list[ReportedFinding]` or raises `ValueError`. The caller (`CodeReview.dispatch` + `PostFindings.execute`) drives dispatch and parse; the plugin owns translation.
- **`ExecSpec.env` carries the Anthropic key.** Documented carve-out for wire-bound exec (matches `otlp_token` on ConfigUpdate). The key is never logged or placed in audit rows; it's decrypted on the control plane and placed into the exec block.
- **`ReviewContext` is the remote dispatch context.** Fields: `org_id`, `repo_external_id`, `pr_external_id`, `head_sha`, `base_sha`, `output_schema`. No diff blob — the skill clones the repo and computes `git diff base..head` itself.
- Subagent markdown (`reviewers/*.md`) is plugin-agnostic — describes *what to check* and the JSON output schema. Plugins wrap it in their native format at bootstrap.

## `CodingAgentPlugin` Protocol

Signatures in `app/domain/coding_agent/types.py`.

### In-process task methods

Each takes a `Workspace`, a mode-specific Pydantic context, and an optional `OnActivity` callback.

- `review` — full base..head diff → `ReviewResult{findings: list[ReportedFinding]}`.
- `incremental_review` — `prev_sha..head` only → `list[ReportedFinding]`.
- `verify_fix` — original finding + original code + current code → still-present verdict.
- `stale_check` — original finding + current code + diff summary → still-applies verdict.
- `answer_question` — finding + anchor code + thread history + question → `answer: str`.

### Remote-dispatch methods (Shape B)

- `build_review_invocation(ctx: ReviewContext, *, session) -> Invocation` — resolves the skill handle, decrypts the API key, assembles the prompt + output-schema appendix, returns the exec spec. Never dispatches.
- `parse_review_output(stdout: str) -> list[ReportedFinding]` — finds the terminal `type=result` stream event, extracts `result`, parses against `FindingDraftList`. Raises `ValueError` on any failure.
- `review_preflight_steps(ctx, *, session) -> tuple[str, ...]` — returns `WorkflowCommand` kind strings to insert before the review step. Returns `()` — skill-assignment resolution is a follow-up.

## Registry

`app/domain/coding_agent/service.py`. `CodingAgentRegistry` holds the plugin map; the live instance is held in a `ContextVar` (`_registry_var`). A module-level `_default_registry` captures all import-time `bootstrap()` calls — production never calls `bind_coding_agent_registry()`. Per-test isolation binds a fresh `.copy()` of the session-scoped canonical snapshot via `plugin_registries_isolation` in `app/testing/isolation.py`. `register_plugin` rejects duplicates. `get_plugin` raises `PluginNotFoundError` on miss.

## Subagent prompt files (`reviewers/`)

Six markdown files: `architecture.md`, `security.md`, `line-level.md`, `tests.md`, `docs.md` (all always-on); `skill.md` (conditional — only when the diff touches `**/SKILL.md` or `.claude/skills/**`). Plugin `plugins/claude_code` reads these at bootstrap and installs them in its native subagent format.

## Data owned

None. Registry is in-memory; subagent content is shipped markdown.

## How it's tested

- `app/domain/coding_agent/test/test_registry.py` — register/get/duplicate-rejection, `validate_config` forwarding, `health_check_all` exception-to-unhealthy.
- `app/domain/coding_agent/test/test_invocation.py` — `build_invocation` exec-block shape, argv/stdin/env, allowed-tools constants.
- Plugin-specific behaviour in `app/plugins/<plugin>/test/`.
