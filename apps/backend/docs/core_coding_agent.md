# core/coding_agent

> Vendor-neutral abstraction over coding-agent CLIs — Protocol, registry, dispatch, and per-mode prompt assembly.

## Scope

Owns: `CodingAgentPlugin` Protocol, per-mode context/result types (`ExecSpec`, `Invocation`, `InvokeCodingAgent`, `RunResult`, `RunStatus`, …), telemetry enums, plugin registry, typed exception hierarchy, per-mode prompt builders and DTOs (`prompts.py`), and the `dispatch_invocation` helper.

Does NOT own: `ReviewContext`, `ReportedFinding`, `FindingDraftList`, `finding_output_schema`, or `parse_review_output` — those live in `domain/reviewer` and are the canonical skill-output types. Does NOT own prompt assembly for the remote-dispatch full-review path (that's `plugins/claude_code.build_review_invocation`), output-format choice, or workspace mechanics.

Lives in `core/` (not `domain/`) because it defines the `CodingAgentPlugin` Protocol and is depended on by `plugins/`. `IncrementalReviewContext.lessons` is typed `list[Any]` at the core boundary to avoid a core→domain import; callers supply `domain/lessons.Lesson` objects, which satisfy the duck-typed `.id`/`.title`/`.body` access in `prompts.py`. Protocol methods whose signatures formerly referenced `ReviewContext` or `ReportedFinding` use `Any` at the `core` boundary for the same reason.

## Why / invariants

- **Remote-dispatch only.** All review work dispatches via the `WorkspaceAgent` — the control plane never execs the CLI in-process. The `CodingAgentPlugin` Protocol owns the exec-spec build (`build_invocation`) and the parse step (`parse_result`); dispatch is the caller's responsibility via `dispatch_invocation`.
- **Live review path uses `build_invocation` + `dispatch_invocation`.** `CodeReview.dispatch` calls `plugin.build_invocation(Invocation(skill="pr_review", ...))` to get an `InvokeCodingAgent` exec block, then calls `coding_agent.dispatch_invocation(...)` to enqueue the command, insert a run row, and pin to the owning agent. The run sink calls `plugin.parse_result` on the terminal event and contributes `{"output": ...}` to the forwarded step outputs. `PostFindings` reads `inputs["output"]`.
- **Legacy protocol methods retained.** `build_review_invocation`, `parse_review_output`, `review_preflight_steps`, `parse_usage`, `render_activity` remain on the Protocol and the plugin for backward compatibility; no production call site uses them in the PR review path.
- **Remote path: plugin owns exec spec + parse; caller dispatches.** `build_invocation` returns an `InvokeCodingAgent{argv, env, stdin, wallclock_seconds}` — the exact command the Go agent spawns. The caller drives dispatch; the plugin owns translation.
- **`ExecSpec.env` / `InvokeCodingAgent.env` carries the Anthropic key.** Documented carve-out for wire-bound exec (matches `otlp_token` on ConfigUpdate). The key is never logged or placed in audit rows; it's decrypted on the control plane and placed into the exec block.
- **`ReviewContext` (in `domain/reviewer`) is the remote dispatch context.** Fields: `org_id`, `repo_external_id`, `pr_external_id`, `head_sha`, `base_sha`, `output_schema`. No diff blob — the skill clones the repo and computes `git diff base..head` itself.
- **`Invocation` is the high-level intent.** Fields: `skill`, `model`, `effort`, `context` (opaque mapping), `wallclock_seconds`. `build_invocation` translates it into an `InvokeCodingAgent`.
- **`dispatch_invocation` is the one-shot dispatch helper.** Mints a UUIDv7 `command_id`, calls `enqueue_command`, inserts a `coding_agent_runs` row, calls `pin_command_to_agent`, returns the `command_id`. All in the caller's transaction — durable iff the transaction commits.

## `CodingAgentPlugin` Protocol

Signatures in `app/core/coding_agent/types.py`.

### Remote-dispatch methods — legacy path

- `build_review_invocation(ctx: Any, *, session) -> Any` — resolves the skill handle, decrypts the API key, assembles the prompt + output-schema appendix, returns a `_LegacyInvocation`. Never dispatches. Both parameter and return type are `Any` at the `core` boundary to avoid core→domain imports.
- `parse_review_output(stdout: str) -> list[Any]` — thin delegator: calls `domain/reviewer.parse_review_output`. The real implementation lives in `domain/reviewer`; the plugin method exists for backward compatibility.
- `review_preflight_steps(ctx, *, session) -> tuple[str, ...]` — returns `WorkflowCommand` kind strings to insert before the review step. Returns `()` — no preflight needed.
- `parse_usage(stdout: str) -> Usage` — reads the terminal `type=result` stream event and extracts `input_tokens` / `output_tokens`. Returns an empty `Usage()` if there's no terminal event or the stream is empty. Never raises.
- `render_activity(stdout: str) -> ActivityLog` — walks every parseable stream event, drops null renders, stamps a monotonic `seq` (starting at 0) onto each surviving opaque dict, and returns the full log. Never raises.

### Remote-dispatch methods — new path

- `build_invocation(invocation: Invocation) -> InvokeCodingAgent` — translates a high-level `Invocation` into a concrete exec block. Raises `CodingAgentError` on unknown skills or missing context keys.
- `parse_result(terminal_event_payload: Mapping[str, Any]) -> RunResult` — decodes a terminal AgentEvent payload into a `RunResult`. Reads `stdout` and `exit_code` from the payload dict; internally calls `parse_usage` and `render_activity` on the stdout and packs the results (plus a separate `duration_ms`) into `RunResult`. The run sink calls this method exclusively. Never raises on missing keys.

### `dispatch_invocation`

`dispatch_invocation(*, workspace_id, org_id, agent_id, workflow_execution_id, plugin, invocation_data: InvokeCodingAgent, ctx: CommandContext, session) -> UUID`

One-shot helper that mints a UUIDv7 `command_id`, enqueues the `InvokeClaudeCode` AgentCommand via `core/agent_gateway.enqueue_command`, inserts a `coding_agent_runs` row (status=running), and pins the command to `agent_id`. Returns the `command_id`. Durable iff the caller's transaction commits. `org_id` is required — callers source it from their org context.

### Value objects

- `Invocation{skill, model, effort, context, wallclock_seconds}` — high-level intent passed to `build_invocation`. Skill-keyed; context is an opaque mapping the plugin interprets.
- `Effort = str` — plugin-specific effort level string (e.g. `"low"`, `"medium"`, `"high"`). Opaque to `core/coding_agent`.
- `InvokeCodingAgent{argv, env, stdin, wallclock_seconds}` — concrete exec block returned by `build_invocation`.
- `RunResult{output, error_message, usage, duration_ms, exit_code, activity}` — result returned by `parse_result`. `error_message` is always `None` from `parse_result`; the sink derives status from the wire event kind. `duration_ms` lives here, not on `Usage`.
- `RunStatus` — `StrEnum` with values `SUCCESS`, `FAILURE`, `TIMEOUT`, `CANCELLED`.
- `Usage{tokens_in: int | None, tokens_out: int | None}` — token counts only; populated from the terminal `result` event. `duration_ms` was moved to `RunResult`.
- `ActivityLog{events: list[Mapping[str, Any]]}` — opaque per-event dicts; persisted as a JSONB blob. Wire shape `{"events": [...]}` is unchanged.
- `ActivityEvent{seq: int, ts, kind, message, detail}` — typed class kept for the live-streaming `OnActivity` callback path (SSE fan-out); NOT used as the element type of `ActivityLog.events` (those are opaque dicts).

## Registry

`app/core/coding_agent/service.py`. `CodingAgentRegistry` holds the plugin map; the live instance is held in a `ContextVar` (`_registry_var`). A module-level `_default_registry` captures all import-time `bootstrap()` calls — production never calls `bind_coding_agent_registry()`. Per-test isolation binds a fresh `.copy()` of the session-scoped canonical snapshot via `plugin_registries_isolation` in `app/testing/isolation.py`. `register_plugin` rejects duplicates. `get_plugin` raises `PluginNotFoundError` on miss.

## Dispatch spans

Each IO dispatch function in `service.py` wraps its plugin call in a `coding_agent.{plugin_id}.{op}` OTel span. Covered: `review`, `incremental_review`, `verify_fix`, `stale_check`, `answer_question`, `validate_config`, and each iteration of `health_check_all`. CPU-only methods (`parse_review_output`, `parse_usage`, `render_activity`) do NOT get spans — they perform no IO. Dispatch functions that re-raise on error (all except `health_check_all`) let the OTel SDK auto-record the exception event and set `StatusCode.ERROR`. `health_check_all` swallows exceptions into an unhealthy `HealthStatus` result, so it explicitly calls `span.record_exception(e)` + `span.set_status(StatusCode.ERROR, ...)` before constructing the result row — ensuring the span is red even though no exception propagates.

## Run lifecycle

`app/core/coding_agent/models.py` owns the `coding_agent_runs` table. One row per `InvokeClaudeCode` agent command; created at dispatch and finalized when the terminal `AgentEvent` arrives.

### Table — `coding_agent_runs`

| Column | Purpose |
|---|---|
| `id` | UUIDv7 PK (`server_default=uuidv7()`). |
| `org_id` | Soft FK (no DB constraint) — for org-scoped queries. |
| `workflow_execution_id` | Soft FK — links the run to its workflow execution. |
| `step_id` | Workflow step id (e.g. `"review"`). |
| `agent_command_id` | FK to `agent_commands.id` — the exact command that ran. |
| `command_kind` | Command kind string (e.g. `"InvokeClaudeCode"`). |
| `plugin_id` | The coding-agent plugin that issued the run. The sink resolves which plugin parses the terminal event from this column — `core/coding_agent` hardcodes no vendor. |
| `model` | Model identifier from the exec spec (nullable). |
| `effort` | Effort level from the exec spec (nullable). |
| `status` | `running` → `success` or `failure`. |
| `tokens_in` / `tokens_out` | `NOT NULL DEFAULT 0`. Written by `finalize_run` from `Usage` parsed off the terminal stream event; 0 when the CLI omitted them. |
| `duration_ms` | Wall-clock duration (ms) computed from `started_at → completed_at`. |
| `exit_code` | Process exit code from the agent event outputs (nullable). |
| `started_at` | Set at `create_run`. |
| `completed_at` | Set at `finalize_run`. |

Index: `(org_id, command_kind, created_at)` for dashboard-style aggregations.

### Service functions

- `create_run(*, org_id, workflow_execution_id, step_id, agent_command_id, command_kind, plugin_id, model=None, effort=None, session) -> UUID` — inserts with `status=running`, flushes, returns the server-minted run id. `plugin_id` is the issuing plugin (`CodeReview.dispatch` passes the resolved plugin's `plugin_id`). Called in the same transaction so the row is durable iff the dispatch commits.
- `get_run_ref_for_command(agent_command_id, *, session) -> RunRef | None` — returns `(run_id, plugin_id)`; the run-sink uses it to resolve which plugin parses the terminal event.
- `finalize_run(run_id, *, usage: Usage, duration_ms: int | None, activity: ActivityLog | None, exit_code, status, session)` — updates `status`, `exit_code`, `tokens_in`/`tokens_out`, `duration_ms`, `completed_at`. Uses the explicit `duration_ms` kwarg when present, falling back to wall-clock (`completed_at − started_at`). When `activity` is non-`None` and the run's `org_id` is known, inserts one `coding_agent_activity` row carrying the rendered log as a JSONB payload.
- `get_run_id_for_command(agent_command_id, *, session) -> UUID | None` — lookup by command id.
- `get_run_id_for_workflow_step(workflow_execution_id, step_id, *, session) -> UUID | None` — lookup by `(workflow_execution_id, step_id)`.
- `get_step_activity(workflow_execution_id, step_id, *, session) -> ActivityLog | None` — two-hop projection: resolve `(wfx_id, step_id) → run_id` via `get_run_id_for_workflow_step`, then read the `coding_agent_activity` payload and validate into `ActivityLog`. Returns `None` when either hop is empty — most commonly because the weekly partition was dropped past the 4-week TTL. The Ticket page renders the `None` case as "activity expired".

### `AgentRunSink` (IoC seam)

`core/agent_gateway` defines the `AgentRunSink` Protocol and a single-slot registry (`register_run_sink` / `get_run_sink` / `clear_run_sink`) in `app/core/agent_gateway/run_sink.py`. This module registers `CodingAgentRunSinkImpl()` at import time via the `core/coding_agent.__init__` side effect. `agent_gateway.record_agent_event` calls the registered sink on every terminal `AgentEvent`; the sink no-ops silently for non-`InvokeClaudeCode` command kinds and returns `None`. For `InvokeClaudeCode` runs it reads the run row's `plugin_id` (via `get_run_ref_for_command`), resolves that plugin, passes the terminal event `outputs` dict directly to `plugin.parse_result(outputs)`, then calls `finalize_run` with the returned `RunResult` (including `result.duration_ms`). Returns `{"output": result.output, "error_message": result.error_message}` so the caller may attach the output to the workflow outcome. After the sink merge, `agent_gateway.record_agent_event` strips `stdout` from the forwarded dict (`outputs.pop("stdout", None)`) — the sink's `output` key is the single source of truth for downstream steps; stale raw stdout never flows forward. Plugin resolution is defensive: a `PluginNotFoundError` logs a warning and returns early — the run row stays unfinalised and the workflow still proceeds. See [core_agent_gateway.md](core_agent_gateway.md).

### Table — `coding_agent_activity`

Partitioned by RANGE on `created_at` (weekly child partitions, ~4-week TTL). This is the codebase's first partitioned table; the parent table and DDL live in [`core/database`](core_database.md). Maintenance — daily `@scheduled` task `coding_agent_activity_partition_maintenance` (cron `0 1 * * *`, in `core/coding_agent/partition_maintenance.py`) — calls `core/database.maintain_coding_agent_activity_partitions()`: creates partitions for the current week + the next two and drops partitions whose week is more than 4 weeks before the current week. Idempotent: `CREATE TABLE IF NOT EXISTS` / `DROP TABLE IF EXISTS`. Raw partition DDL lives in `core/database` (the only module the table-access checker allows raw SQL against `coding_agent_activity`); this module owns scheduling only.

| Column | Purpose |
|---|---|
| `run_id` | FK to `coding_agent_runs.id` (`ON DELETE CASCADE`). Part of the composite PK. |
| `created_at` | Insertion time (`server_default=now()`). Partition key + tail of the composite PK. |
| `org_id` | Soft FK — for org-scoped queries / partition pruning. |
| `payload` | JSONB blob — serialized `ActivityLog` (`events` list of opaque dicts, each with `seq`/`ts`/`kind`/`message`/`detail`). |

The row's SQLAlchemy mapped class (`CodingAgentActivityRow`) lives on the shared `Base` and declares `postgresql_partition_by="RANGE (created_at)"`, so `Base.metadata.create_all` emits the partitioned parent — keeping the ORM column shape and the Alembic baseline DDL from drifting. The partitioned parent is created by the Alembic baseline; `migrate()` seeds the initial child partition window `(current week, +1, +2)` via `maintain_coding_agent_activity_partitions()` immediately after Alembic finishes. The daily scheduled task calls the same function to keep the window rolling. Deleting the parent run cascades the activity rows.

## Data owned

- In-memory: plugin registry (`CodingAgentRegistry` in `ContextVar`).
- Persistent: `coding_agent_runs` table (one row per `InvokeClaudeCode` command); `coding_agent_activity` partitioned table (one row per finalized run carrying the rendered activity log).

## How it's tested

- `app/core/coding_agent/test/test_registry.py` — register/get/duplicate-rejection, `validate_config` forwarding, `health_check_all` exception-to-unhealthy.
- `app/core/coding_agent/test/test_dispatch_spans_service.py` — service test: a plugin `review` that raises `CodingAgentError` produces a `coding_agent.{plugin_id}.review` span with `StatusCode.ERROR` and an `exception` event.
- `app/core/coding_agent/test/test_health_check_span_service.py` — service test: a plugin `health_check` that raises produces a `coding_agent.{plugin_id}.health_check` span with `StatusCode.ERROR` and an `exception` event, while `health_check_all` still returns an unhealthy `HealthStatus` (no re-raise).
- `app/core/coding_agent/test/test_invocation.py` — `build_invocation` exec-block shape, argv/stdin/env, allowed-tools constants.
- `app/core/coding_agent/test/test_dispatch_invocation_service.py` — service test: `dispatch_invocation` returns a UUIDv7, inserts a `coding_agent_runs` row (status=running, correct plugin_id and step_id), and is resolvable via `get_run_id_for_command`. Each call mints a distinct command_id.
- `app/core/coding_agent/test/test_run_lifecycle_service.py` — service tests: `create_run`/`finalize_run` round-trip (tokens default to 0 before finalize; explicit token kwarg lands on the row; `plugin_id` persists), activity blob persists to `coding_agent_activity` (events are opaque dicts), `get_step_activity` returns the rendered log when present and `None` when no run exists or the activity row is absent (aged-out partition).
- `app/plugins/claude_code/test/test_stream_parsing.py` — `parse_usage` (extracts tokens, tolerates missing usage block, empty stream; no `duration_ms` on `Usage`) and `render_activity` (monotonic seq across the full stream, null-render filtering, empty-stream → empty log; events are opaque dicts).
- `app/plugins/claude_code/test/test_build_invocation_method.py` — unit tests for `ClaudeCodePlugin.build_invocation`: returns `InvokeCodingAgent`, argv non-empty (starts with `claude`), model/effort propagated, wallclock propagated, `env` carries API key when supplied, unknown skill raises `CodingAgentError`, missing context key raises `CodingAgentError`.
- `app/plugins/claude_code/test/test_parse_result_method.py` — unit tests for `ClaudeCodePlugin.parse_result`: returns `RunResult`, output = stdout, exit_code propagated, error_message always None, usage tokens parsed from stream, graceful on missing/malformed stdout, `duration_ms` is on `RunResult` (not on `Usage`).
- `app/core/coding_agent/test/test_sink_uses_parse_result_service.py` — service test: `CodingAgentRunSinkImpl.handle_terminal_event` calls `plugin.parse_result(outputs)`, writes token counts + duration_ms + exit_code + status to the run row, creates a `coding_agent_activity` blob, and returns `{"output": ..., "error_message": ...}`; `completed_failure` writes `status=failure`; non-`InvokeClaudeCode` kinds return `None`.
- `app/core/database/test/test_coding_agent_activity_migration.py` — verifies that after `migrate()`: the parent is RANGE-partitioned, ≥3 weekly child partitions exist, `_coding_agent_activity_partition_ddl` names partitions deterministically for a known UTC date, and a `created_at=now()` row routes to the current-week child.
- Plugin-specific behaviour in `app/plugins/<plugin>/test/`.
