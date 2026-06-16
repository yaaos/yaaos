# core/coding_agent

> Vendor-neutral abstraction over coding-agent CLIs — two-method Protocol, registry, dispatch, and run lifecycle.

## Scope

Owns: `CodingAgentPlugin` Protocol (2 methods: `build_invocation`, `parse_result`), high-level intent and exec-block types (`Invocation`, `InvokeCodingAgent`), run-result types (`RunResult`, `RunStatus`, `Usage`, `ActivityLog`), typed exception hierarchy (`CodingAgentError`, `PluginNotFoundError`), plugin registry (`CodingAgentRegistry`), dispatch helper (`dispatch_invocation`), and the `coding_agent_runs` + `coding_agent_activity` tables.

Does NOT own: `ReviewContext`, `ReportedFinding`, `FindingDraftList`, or `parse_review_output` — those live in `domain/reviewer`. Does NOT own prompt assembly, skill resolution, output-format choice, or workspace mechanics.

Lives in `core/` (not `domain/`) because it defines the `CodingAgentPlugin` Protocol and is depended on by `plugins/`.

## Why / invariants

- **Remote-dispatch only.** All review work dispatches via the `WorkspaceAgent` — the control plane never execs the CLI in-process.
- **Two-method Protocol.** `build_invocation` translates a high-level `Invocation` into a concrete `InvokeCodingAgent` exec block. `parse_result` decodes a terminal AgentEvent payload into a `RunResult`. No other methods are on the Protocol.
- **Plugin owns skill resolution and stdout parsing.** `core/coding_agent` owns dispatch and the run lifecycle; plugins own the exec-spec shape and parse logic.
- **`InvokeCodingAgent.env` carries the Anthropic key.** Documented carve-out for wire-bound exec (matches `otlp_token` on ConfigUpdate). The key is never logged or placed in audit rows.
- **`dispatch_invocation` is the one-shot dispatch helper.** Mints a UUIDv7 `command_id`, calls `enqueue_command`, inserts a `coding_agent_runs` row, calls `pin_command_to_agent`, returns the `command_id`. All in the caller's transaction — durable iff the transaction commits.

## `CodingAgentPlugin` Protocol

Signatures in `app/core/coding_agent/types.py`.

- `plugin_id: str` — registry key and run-row attribute.
- `build_invocation(invocation: Invocation) -> InvokeCodingAgent` — pure function: translates skill + model + effort + context + wallclock cap into the exact argv/env/stdin the Go agent runs. Raises `CodingAgentError` on unknown skills or missing context keys.
- `parse_result(terminal_event_payload: Mapping[str, Any]) -> RunResult` — pure function: decodes a terminal AgentEvent `outputs` dict into a `RunResult`. Reads `stdout` and `exit_code`; populates `usage`, `activity`, `duration_ms`. Never raises on missing keys.

### `dispatch_invocation`

`dispatch_invocation(*, workspace_id, org_id, agent_id, workflow_execution_id, plugin, invocation_data: InvokeCodingAgent, ctx: CommandContext, session) -> UUID`

One-shot helper in `service.py`. Uses lazy imports of `InvokeClaudeCodeCommand` / `InvokeClaudeCodeLimits` from `core/agent_gateway` (those types stay in `agent_gateway` as part of the `AgentCommand` union but are not in `agent_gateway.__all__`; the lazy import avoids a module-level cross-layer reference). Returns `command_id`. `org_id` sourced from caller's org context.

### Value objects

- `Invocation{skill, model, effort, context, wallclock_seconds}` — high-level intent. Context is an opaque mapping the plugin interprets per skill.
- `Effort = str` — plugin-specific effort level. Opaque to `core/coding_agent`.
- `InvokeCodingAgent{argv, env, stdin, wallclock_seconds}` — concrete exec block.
- `RunResult{output, error_message, usage, duration_ms, exit_code, activity}` — `error_message` is always `None` from `parse_result`; the sink derives status from the wire event kind. `duration_ms` lives here, not on `Usage`.
- `RunStatus` — `StrEnum`: `SUCCESS`, `FAILURE`, `TIMEOUT`, `CANCELLED`.
- `Usage{tokens_in: int | None, tokens_out: int | None}` — token counts from the terminal `result` stream event.
- `ActivityLog{events: list[Mapping[str, Any]]}` — opaque per-event dicts; persisted as a JSONB blob.

## Registry

`app/core/coding_agent/service.py`. `CodingAgentRegistry` holds the plugin map in a `ContextVar`. A module-level `_default_registry` captures all import-time `bootstrap()` calls — production never calls `bind_coding_agent_registry()`. Per-test isolation binds a fresh `.copy()` via `plugin_registries_isolation` in `app/testing/isolation.py`. `register_plugin` rejects duplicates. `get_plugin` raises `PluginNotFoundError` on miss.

## Run lifecycle

`app/core/coding_agent/run_service.py` manages `coding_agent_runs` rows.

### Table — `coding_agent_runs`

| Column | Purpose |
|---|---|
| `id` | UUIDv7 PK. |
| `org_id` | Soft FK — org-scoped queries. |
| `workflow_execution_id` | Soft FK — links to workflow execution. |
| `step_id` | Workflow step id (e.g. `"review"`). |
| `agent_command_id` | FK to `agent_commands.id`. |
| `command_kind` | Command kind string (`"InvokeClaudeCode"`). |
| `plugin_id` | Plugin that issued the run (sink resolves which plugin parses the terminal event). |
| `status` | `running` → `success` or `failure`. |
| `tokens_in` / `tokens_out` | `NOT NULL DEFAULT 0`. Written from `Usage.parse_result`. |
| `duration_ms` | Wall-clock duration (ms). |
| `exit_code` | Process exit code (nullable). |
| `started_at` | Set at `create_run`. |
| `completed_at` | Set at `finalize_run`. |

### Service functions

- `create_run(...)` — inserts with `status=running`, flushes, returns the run id.
- `get_run_ref_for_command(agent_command_id, *, session)` — returns `(run_id, plugin_id)`; used by the run sink to resolve which plugin parses the terminal event.
- `finalize_run(run_id, *, usage, duration_ms, activity, exit_code, status, session)` — updates status, tokens, duration, activity blob.
- `get_run_id_for_command(agent_command_id, *, session)` — lookup by command id.
- `get_run_id_for_workflow_step(workflow_execution_id, step_id, *, session)` — lookup by `(workflow_execution_id, step_id)`.
- `get_step_activity(workflow_execution_id, step_id, *, session)` — two-hop: resolve run id, then read the `coding_agent_activity` JSONB payload. Returns `None` when absent (partition TTL, no run).

### `AgentRunSink` (IoC seam)

`core/agent_gateway` defines the `AgentRunSink` Protocol. `core/coding_agent.__init__` registers `CodingAgentRunSinkImpl()` at import. `record_agent_event` calls the sink on every terminal `AgentEvent`; for `InvokeClaudeCode` it resolves the plugin via `get_run_ref_for_command`, calls `plugin.parse_result(outputs)`, then calls `finalize_run`. Returns `{"output": result.output, "error_message": result.error_message}` for downstream workflow steps. See [core_agent_gateway.md](core_agent_gateway.md).

### Table — `coding_agent_activity`

Partitioned RANGE on `created_at` (weekly child partitions, ~4-week TTL). One row per finalized run; `payload` is the `ActivityLog` JSONB blob. Daily `@scheduled` task `coding_agent_activity_partition_maintenance` (in `partition_maintenance.py`) keeps the partition window rolling.

| Column | Purpose |
|---|---|
| `run_id` | FK to `coding_agent_runs.id` (`ON DELETE CASCADE`). |
| `created_at` | Partition key + part of composite PK. |
| `org_id` | Soft FK. |
| `payload` | JSONB `ActivityLog` blob (`{"events": [...]}`). |

## Data owned

- In-memory: plugin registry (`CodingAgentRegistry` in `ContextVar`).
- Persistent: `coding_agent_runs`, `coding_agent_activity` (partitioned).

## How it's tested

- `app/core/coding_agent/test/test_registry.py` — register/get/duplicate-rejection; `bind_coding_agent_registry` isolation.
- `app/core/coding_agent/test/test_protocol_surface_service.py` — asserts exact `__all__` set, Protocol has exactly `build_invocation` + `parse_result`, retired names not importable.
- `app/core/coding_agent/test/test_dispatch_invocation_service.py` — service: `dispatch_invocation` returns UUIDv7, inserts run row, resolvable via `get_run_id_for_command`.
- `app/core/coding_agent/test/test_run_lifecycle_service.py` — service: create/finalize round-trip, activity blob, `get_step_activity`.
- `app/core/coding_agent/test/test_sink_uses_parse_result_service.py` — service: `CodingAgentRunSinkImpl.handle_terminal_event` calls `plugin.parse_result`, writes run row, returns `output` + `error_message`; non-`InvokeClaudeCode` kinds return `None`.
- `app/plugins/claude_code/test/test_stream_parsing.py` — `_parse_usage` + `_render_activity_log` private helpers.
- `app/plugins/claude_code/test/test_build_invocation_method.py` — `ClaudeCodePlugin.build_invocation` unit tests.
- `app/plugins/claude_code/test/test_parse_result_method.py` — `ClaudeCodePlugin.parse_result` unit tests.
