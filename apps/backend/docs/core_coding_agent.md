# core/coding_agent

> Vendor-neutral abstraction over coding-agent CLIs — Protocol, registry, dispatch, and per-mode prompt assembly.

## Scope

Owns: `CodingAgentPlugin` Protocol, per-mode context/result types (`ExecSpec`, `Invocation`, `ReviewContext`, …), telemetry enums, plugin registry, typed exception hierarchy, per-mode prompt builders and DTOs (`prompts.py`).

Does NOT own: prompt assembly for the remote-dispatch full-review path (that's `plugins/claude_code.build_review_invocation`), output-format choice, workspace mechanics.

Lives in `core/` (not `domain/`) because it defines the `CodingAgentPlugin` Protocol and is depended on by `plugins/`. `IncrementalReviewContext.lessons` is typed `list[Any]` at the core boundary to avoid a core→domain import; callers supply `domain/lessons.Lesson` objects, which satisfy the duck-typed `.id`/`.title`/`.body` access in `prompts.py`.

## Why / invariants

- **Status-not-exception contract:** in-process task methods (`review`, `incremental_review`, …) MUST NOT raise on agent-level failures (timeout, bad JSON, non-zero exit) — those become `status` + `error_message`. Only infrastructure errors (`WorkspaceExecError`, etc.) are raised. Consumers (`reviewer`) branch on `result.status`.
- **Five named in-process methods, plus five remote-dispatch methods.** Five in-process task methods (`review`, `incremental_review`, `verify_fix`, `stale_check`, `answer_question`) and five remote-dispatch methods (`build_review_invocation`, `parse_review_output`, `review_preflight_steps`, `parse_usage`, `render_activity`). Adding a mode requires a Protocol change.
- **Remote path: plugin owns exec spec + parse; caller dispatches.** `build_review_invocation` returns a typed `Invocation{kind, exec: ExecSpec, limits}` — the exact command the Go agent spawns. `parse_review_output` receives the agent's raw stream-json stdout and returns `list[ReportedFinding]` or raises `ValueError`. The caller (`CodeReview.dispatch` + `PostFindings.execute`) drives dispatch and parse; the plugin owns translation.
- **`ExecSpec.env` carries the Anthropic key.** Documented carve-out for wire-bound exec (matches `otlp_token` on ConfigUpdate). The key is never logged or placed in audit rows; it's decrypted on the control plane and placed into the exec block.
- **`ReviewContext` is the remote dispatch context.** Fields: `org_id`, `repo_external_id`, `pr_external_id`, `head_sha`, `base_sha`, `output_schema`. No diff blob — the skill clones the repo and computes `git diff base..head` itself.

## `CodingAgentPlugin` Protocol

Signatures in `app/core/coding_agent/types.py`.

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
- `parse_usage(stdout: str) -> Usage` — reads the terminal `type=result` stream event and extracts `input_tokens` / `output_tokens` / `duration_ms`. Returns an empty `Usage()` if there's no terminal event or the stream is empty. Never raises.
- `render_activity(stdout: str) -> ActivityLog` — walks every parseable stream event, drops null renders, stamps a monotonic `seq` (starting at 0) onto each surviving `ActivityEvent`, and returns the full log. Never raises.

### Value objects

- `Usage{tokens_in: int | None, tokens_out: int | None, duration_ms: int | None}` — populated from the terminal `result` event; any field may be `None` when the CLI omits it.
- `ActivityLog{events: tuple[ActivityEvent, ...]}` — pre-rendered, immutable log persisted to `coding_agent_activity` as a single JSONB blob.
- `ActivityEvent{seq: int, ts, kind, message, detail}` — one row per useful stream event; `seq` is monotonic per log.

## Registry

`app/core/coding_agent/service.py`. `CodingAgentRegistry` holds the plugin map; the live instance is held in a `ContextVar` (`_registry_var`). A module-level `_default_registry` captures all import-time `bootstrap()` calls — production never calls `bind_coding_agent_registry()`. Per-test isolation binds a fresh `.copy()` of the session-scoped canonical snapshot via `plugin_registries_isolation` in `app/testing/isolation.py`. `register_plugin` rejects duplicates. `get_plugin` raises `PluginNotFoundError` on miss.

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
| `tokens_in` / `tokens_out` | Populated from `Usage` parsed off the terminal stream event; NULL when the CLI omitted them. |
| `duration_ms` | Wall-clock duration (ms) computed from `started_at → completed_at`. |
| `exit_code` | Process exit code from the agent event outputs (nullable). |
| `started_at` | Set at `create_run`. |
| `completed_at` | Set at `finalize_run`. |

Index: `(org_id, command_kind, created_at)` for dashboard-style aggregations.

### Service functions

- `create_run(*, org_id, workflow_execution_id, step_id, agent_command_id, command_kind, plugin_id, model=None, effort=None, session) -> UUID` — inserts with `status=running`, flushes, returns the server-minted run id. `plugin_id` is the issuing plugin (`CodeReview.dispatch` passes the resolved plugin's `plugin_id`). Called in the same transaction so the row is durable iff the dispatch commits.
- `get_run_ref_for_command(agent_command_id, *, session) -> RunRef | None` — returns `(run_id, plugin_id)`; the run-sink uses it to resolve which plugin parses the terminal event.
- `finalize_run(run_id, *, usage: Usage, activity: ActivityLog | None, exit_code, status, session)` — updates `status`, `exit_code`, `tokens_in`/`tokens_out`, `duration_ms`, `completed_at`. Prefers `usage.duration_ms` when present, falling back to wall-clock (`completed_at − started_at`). When `activity` is non-`None` and the run's `org_id` is known, inserts one `coding_agent_activity` row carrying the rendered log as a JSONB payload.
- `get_run_id_for_command(agent_command_id, *, session) -> UUID | None` — lookup by command id.
- `get_run_id_for_workflow_step(workflow_execution_id, step_id, *, session) -> UUID | None` — lookup by `(workflow_execution_id, step_id)`.
- `get_step_activity(workflow_execution_id, step_id, *, session) -> ActivityLog | None` — two-hop projection: resolve `(wfx_id, step_id) → run_id` via `get_run_id_for_workflow_step`, then read the `coding_agent_activity` payload and validate into `ActivityLog`. Returns `None` when either hop is empty — most commonly because the weekly partition was dropped past the 4-week TTL. The Ticket page renders the `None` case as "activity expired".

### `AgentRunSink` (IoC seam)

`core/agent_gateway` defines the `AgentRunSink` Protocol and a single-slot registry (`register_run_sink` / `get_run_sink` / `clear_run_sink`) in `app/core/agent_gateway/run_sink.py`. This module registers `CodingAgentRunSinkImpl()` at import time via the `core/coding_agent.__init__` side effect. `agent_gateway.record_agent_event` calls the registered sink on every terminal `AgentEvent`; the sink no-ops silently for non-`InvokeClaudeCode` command kinds. For `InvokeClaudeCode` runs it reads the run row's `plugin_id` (via `get_run_ref_for_command`), resolves that plugin, reads the captured stdout from `event.outputs`, calls the plugin's `parse_usage` + `render_activity`, and passes the results into `finalize_run`. Plugin resolution is defensive: a `PluginNotFoundError` (the sink loaded without the issuing plugin in a misconfigured/multi-plugin env) logs a warning and returns early — the run row stays unfinalised and the workflow still proceeds. See [core_agent_gateway.md](core_agent_gateway.md).

### Table — `coding_agent_activity`

Partitioned by RANGE on `created_at` (weekly child partitions, ~4-week TTL). This is the codebase's first partitioned table; the parent table and DDL live in [`core/database`](core_database.md). Maintenance — daily `@scheduled` task `coding_agent_activity_partition_maintenance` (cron `0 1 * * *`, in `core/coding_agent/partition_maintenance.py`) — calls `core/database.maintain_coding_agent_activity_partitions()`: creates partitions for the current week + the next two and drops partitions whose week is more than 4 weeks before the current week. Idempotent: `CREATE TABLE IF NOT EXISTS` / `DROP TABLE IF EXISTS`. Raw partition DDL lives in `core/database` (the only module the table-access checker allows raw SQL against `coding_agent_activity`); this module owns scheduling only.

| Column | Purpose |
|---|---|
| `run_id` | FK to `coding_agent_runs.id` (`ON DELETE CASCADE`). Part of the composite PK. |
| `created_at` | Insertion time (`server_default=now()`). Partition key + tail of the composite PK. |
| `org_id` | Soft FK — for org-scoped queries / partition pruning. |
| `payload` | JSONB blob — serialized `ActivityLog` (`events` tuple, each with `seq`/`ts`/`kind`/`message`/`detail`). |

The row's SQLAlchemy mapped class (`CodingAgentActivityRow`) lives on the shared `Base` and declares `postgresql_partition_by="RANGE (created_at)"`, so `Base.metadata.create_all` emits the partitioned parent — keeping the ORM column shape and the migration DDL from drifting. Child partitions are seeded by an `after_create` listener (create_all path) or the `_apply_create_coding_agent_activity` migration (`migrate()`/prod path); both use the seed window `(current week, +1, +2)` matching the maintenance task. Deleting the parent run cascades the activity rows.

## Data owned

- In-memory: plugin registry (`CodingAgentRegistry` in `ContextVar`).
- Persistent: `coding_agent_runs` table (one row per `InvokeClaudeCode` command); `coding_agent_activity` partitioned table (one row per finalized run carrying the rendered activity log).

## How it's tested

- `app/core/coding_agent/test/test_registry.py` — register/get/duplicate-rejection, `validate_config` forwarding, `health_check_all` exception-to-unhealthy.
- `app/core/coding_agent/test/test_invocation.py` — `build_invocation` exec-block shape, argv/stdin/env, allowed-tools constants.
- `app/core/coding_agent/test/test_run_lifecycle_service.py` — service tests: `create_run`/`finalize_run` round-trip (tokens + duration land on the row; `plugin_id` persists), run-sink no-op for non-`InvokeClaudeCode`, run-sink resolves the plugin from the run row's `plugin_id` and skips (logs + returns, no raise, run stays unfinalised) when that plugin is unregistered, activity blob persists to `coding_agent_activity`, `reviews.run_id` populated via `publish_findings`, `get_step_activity` returns the rendered log when present and `None` when no run exists or the activity row is absent (aged-out partition).
- `app/plugins/claude_code/test/test_stream_parsing.py` — `parse_usage` (extracts tokens + duration, tolerates missing usage block, empty stream) and `render_activity` (monotonic seq across the full stream, null-render filtering, empty-stream → empty log).
- `app/core/database/test/test_coding_agent_activity_migration.py` — verifies the parent is RANGE-partitioned, ≥3 weekly child partitions exist, `_apply_create_coding_agent_activity` is idempotent under double-fire, the shared `_coding_agent_activity_partition_ddl` helper names partitions deterministically for a known UTC date (no backdated week), and a `created_at=now()` row routes to the current-week child.
- Plugin-specific behaviour in `app/plugins/<plugin>/test/`.
