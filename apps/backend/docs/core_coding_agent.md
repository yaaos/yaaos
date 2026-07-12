# core/coding_agent

> Vendor-neutral abstraction over coding-agent CLIs — Protocol, registry, dispatch, run lifecycle, and per-org install state.

## Scope

Owns: `CodingAgentPlugin` Protocol (`compile_invocation`, `parse_result`, `parse_activity_line`, `validate_settings`, `stage_options`, `skill_path`, `render_skill_bundle`, plus `display_name`/`command_kind` attributes), `StageOptions` VO (advertised model/effort lists), skills-bundle VOs (`SkillSource`, `AgentSource`, `BundleFile`), high-level intent and exec-block types (`Invocation`, `InvokeCodingAgent`), run-result types (`RunResult`, `RunStatus`, `Usage`, `ActivityEvent`, `ActivityLog`), typed exception hierarchy (`CodingAgentError`, `PluginNotFoundError`), plugin registry (`CodingAgentRegistry`), dispatch helper (`dispatch_invocation`), skills-bundle builder (`build_skills_bundle_zip`), API key aggregator (`build_api_key_secrets_for_org`), per-org install state (`org_coding_agents` table, `CodingAgentInstall` VO, install/update/uninstall/list service functions, `/api/coding-agents` routes), and the `coding_agent_runs` + `coding_agent_activity` tables.

Does NOT own: prompt assembly, skill resolution, output-format choice, or workspace mechanics — those are plugin- or caller-owned (`plugins/claude_code`, `domain/pipelines`).

Lives in `core/` (not `domain/`) because it defines the `CodingAgentPlugin` Protocol and is depended on by `plugins/`.

## Why / invariants

- **Remote-dispatch only.** All review work dispatches via the `WorkspaceAgent` — the control plane never execs the CLI in-process.
- **Plugin owns skill resolution, stdout parsing, and settings validation.** `core/coding_agent` owns dispatch and the run lifecycle; plugins own the exec-spec shape, parse logic, and schema enforcement for their settings.
- **API key secrets delivered via ConfigUpdate, not invocation env.** `build_api_key_secrets_for_org` forward-forwards all stored org keys (from `core/api_keys.list_keys_for_org` + `get` per provider) and registers as the `ApiKeySecretsProvider` IoC seam in `core/agent_gateway`. `_build_config_update_dto` calls the provider to populate `AgentConfig.api_keys` on every ConfigUpdate. The agent-side env maps (`apiKeyProviderEnvVars` / `apiKeyProcessEnvVars`) are the effective allowlist — unknown providers are ignored there by design. `InvokeCodingAgent.env` is intentionally empty — no credentials travel via the exec env.
- **`dispatch_invocation` is Layer 3 — the full intent-to-wire helper.** Takes `invocation`, `plugin`, `ctx`, a required caller-minted `command_id`, `session`. Loads the workspace owner for `org_id`, calls `plugin.compile_invocation(invocation)` to get the exec block, builds the appropriate command type (`InvokeClaudeCodeCommand` for `claude_code`, `InvokeCodexCommand` for `codex`) based on `plugin.command_kind`, delegates to `dispatch_via_workspace` (Layer 2 in `core/workspace`) with `claim_workspace=True`, then inserts a `coding_agent_runs` row. Returns `command_id`. Durable iff the caller's transaction commits.
- **`command_id` is caller-minted, not minted inside `dispatch_invocation`.** `domain/pipelines`'s skill-stage dispatch mints it before calling `dispatch_invocation`, since it also needs the id for the skill's `artifact_path = "$TMPDIR/<command_id>.md"` — no default, no shim.

## `CodingAgentPlugin` Protocol

Signatures in `app/core/coding_agent/types.py`.

- `plugin_id: str` — registry key and run-row attribute.
- `display_name: str` — human-readable plugin name surfaced in the `/api/coding-agents` list (e.g. `"Claude Code"`).
- `command_kind: str` — the `agent_commands.command_kind` value this plugin produces (`"InvokeClaudeCode"` for `claude_code`, `"InvokeCodex"` for `codex`). Used by `dispatch_invocation` to choose which command struct to build.
- `compile_invocation(invocation: Invocation) -> InvokeCodingAgent` — pure function: translates skill + model + effort + context + wallclock cap into the exact argv/env/stdin the Go agent runs. Returns `env={}` — credentials are delivered via ConfigUpdate `api_keys`, not the exec env. Raises `CodingAgentError` when required context keys are missing.
- `parse_result(terminal_event_payload: Mapping[str, Any]) -> RunResult` — pure function: decodes a terminal AgentEvent `outputs` dict into a `RunResult`. Reads `stdout` and `exit_code`; extracts `RunResult.output` from the `result` field of the terminal stream-json event (the agent's structured response JSON — the caller validates it against the invocation's own output schema); populates `usage`, `activity`, `duration_ms`. Never raises on missing keys.
- `validate_settings(settings: Mapping[str, Any]) -> dict[str, Any]` — pure function: validates a raw settings dict and returns the normalized form. Raises `ValueError` on invalid input (unknown keys, bad types). The `/api/coding-agents` install and update endpoints call this before persisting to `org_coding_agents.settings`; a `ValueError` becomes a 422 with `{"error": "invalid_settings", "message": ...}`.
- `parse_activity_line(line: str) -> ActivityEvent | None` — pure function: decodes one raw `stream-json` output line into a normalized `ActivityEvent`. Returns `None` for blank lines or lines the plugin does not recognize. Called by `CodingAgentRunSinkImpl.handle_progress_event` on every `progress` AgentEvent's `stream_line` field to produce the `{kind, ts, message, detail}` frame published on the workspace-activity SSE channel.
- `stage_options() -> StageOptions` — returns the plugin's advertised `{models, efforts}` tuples. The `/api/coding-agents` list endpoint attaches these to each installed-agent row so the SPA's stage editor can populate model/effort dropdowns per agent without a separate fetch.
- `skill_path(skill_name: str) -> str` — returns the on-disk path of the named skill inside the agent's checkout (e.g. `.claude/skills/<skill_name>/SKILL.md`). `dispatch_invocation` uses this instead of hard-coding the convention so a future plugin can place skills elsewhere.
- `render_skill_bundle(skills: Sequence[SkillSource], agents: Sequence[AgentSource]) -> list[BundleFile]` — pure function: transforms the parsed canonical skill/agent sources into the vendor-native bundle layout. `claude_code` passes through `.claude/skills/<name>/SKILL.md` + `.claude/agents/<name>.md` unchanged; `codex` re-targets to `.codex/skills/<name>/SKILL.md`, generates `.codex/agents/<name>.toml` (TOML literal multiline-string prompt, includes the defensive-restatement directive), and emits an `AGENTS.md` at the repo root with the delegation-authorization sentence required by the codex multi-agent protocol.

### `build_skills_bundle_zip` and skills-bundle VOs

`build_skills_bundle_zip(plugin_id: str) -> bytes` — async; reads `settings.yaaos_skills_source_dir` (baked into the backend image at `/app/yaaos_skills` in production; dev default: repo `.claude/`), loads all `pipeline-*` skill directories and `pipeline-*.md` agent files, parses YAML frontmatter from each, calls `plugin.render_skill_bundle(skills, agents)`, and packages the output into a ZIP archive (reproducible — fixed mtime `2020-01-01`). Raises `PluginNotFoundError` on an unknown plugin (→ 404); raises `FileNotFoundError` when the source directory is missing from the image (→ 500).

Skills-bundle VOs (all frozen Pydantic models):

- `SkillSource{name, frontmatter, body, extra_files}` — one parsed skill directory: name (from frontmatter `name` field or directory name), parsed YAML `frontmatter` dict, `body` string (content after the frontmatter block), and `extra_files` tuple of `BundleFile` for any non-`SKILL.md` files in the directory.
- `AgentSource{name, frontmatter, body}` — one parsed agent `.md` file.
- `BundleFile{path, content}` — one file in the output ZIP: repo-root-relative path + text content.

### `dispatch_invocation`

`dispatch_invocation(*, invocation: Invocation, plugin: CodingAgentPlugin, ctx: DispatchContext, command_id: UUID, session: AsyncSession) -> UUID`

Layer 3 helper in `service.py`. `ctx: DispatchContext` (`core/agent_gateway`) carries the calling run's correlation fields (`run_id`, `ticket_id`, `stage_execution_id`, `attempt`, `traceparent`). `workspace_id` is read from `invocation.workspace_id`. Flow:
1. `get_workspace_owner(invocation.workspace_id)` — loads `org_id` + `owning_agent_id` from the workspace row; raises `WorkspaceNotFoundError` if absent.
2. `plugin.compile_invocation(invocation)` — translates high-level intent to an exec block; raises `CodingAgentError` on a malformed context.
3. `skill_path = plugin.skill_path(invocation.skill)` — delegates to the plugin so different plugins can place skills at different checkout-relative paths.
4. Builds a command from the caller-supplied `command_id` with the exec block, `skill_path`, and `command_kind = plugin.command_kind`.
5. `dispatch_via_workspace(command, workspace_id, ctx, session, claim_workspace=True)` — Layer 2: enqueues, pins to owning agent, atomically claims via `try_claim`; raises `WorkspaceClaimFailed` when busy or inactive.
6. `create_run(...)` — inserts `coding_agent_runs` row.
Returns `command_id`. `org_id` is resolved from the workspace row, not a caller parameter.

The agent stats `skill_path` before spawning claude — absent → `completed_failure` with `failure_reason="skill not found: <path>"`. Zero policy on the agent side; the path is entirely plugin-computed on the backend.

### Value objects

- `SkillSource{name, frontmatter, body, extra_files}` · `AgentSource{name, frontmatter, body}` · `BundleFile{path, content}` — skills-bundle VOs; see [§ `build_skills_bundle_zip` and skills-bundle VOs](#build_skills_bundle_zip-and-skills-bundle-vos) above.
- `StageOptions{models: tuple[str, ...], efforts: tuple[str, ...]}` — frozen Pydantic model returned by `plugin.stage_options()`. Carried on `CodingAgentView` so the SPA can populate model/effort dropdowns per installed agent without a separate fetch.
- `Invocation{skill, model, effort, context, wallclock_seconds}` — high-level intent. Context is an opaque mapping the plugin interprets per skill.
- `Effort = str` — plugin-specific effort level. Opaque to `core/coding_agent`.
- `InvokeCodingAgent{argv, env, stdin, wallclock_seconds}` — concrete exec block.
- `RunResult{output, error_message, usage, duration_ms, exit_code, activity}` — `output` is the structured response JSON extracted from the stream-json `result` field (the caller validates it against the invocation's own output schema — `domain/pipelines`' `SkillReturn`/`SkillReviewReturn`). `error_message` is always `None` from `parse_result`; the sink derives status from the wire event kind. `duration_ms` lives here, not on `Usage`.
- `RunStatus` — `StrEnum`: `SUCCESS`, `FAILURE`, `TIMEOUT`, `CANCELLED`.
- `Usage{tokens_in: int | None, tokens_out: int | None}` — token counts from the terminal `result` stream event.
- `ActivityEvent{seq, ts, kind, message, detail}` — one rendered event. `kind` is a `Literal` over six values: `session_start`, `subagent_dispatched`, `tool_call_started`, `assistant_message`, `tool_call_finished`, `result`. `ts` is a `datetime` (Pydantic coerces ISO strings on parse). `detail` is a kind-specific `dict[str, Any]` — shapes documented in `app/core/coding_agent/types.py`. Construction rejects unknown `kind` values. `ACTIVITY_EVENT_KINDS` is a `frozenset` of the same values, exported for tests.
- `ActivityLog{events: list[ActivityEvent]}` — typed list of activity events; persisted as a JSONB blob. Wire shape `{"events": [{seq, ts, kind, message, detail}, ...]}` unchanged; `ts` serializes as ISO-8601.

## Registry

`app/core/coding_agent/service.py`. `CodingAgentRegistry` holds the plugin map in a `ContextVar[CodingAgentRegistry | None]` with `default=None`; `_get()` lazily creates the instance on first access per context. Production composition roots do nothing — the default instance materialises on first `register_plugin` call. `list_plugins()` returns all registered plugins. Per-test isolation binds a fresh `.copy()` via `plugin_registries_isolation` in `app/testing/isolation.py`. `register_plugin` rejects duplicates. `get_plugin` raises `PluginNotFoundError` on miss.

## Run lifecycle

`app/core/coding_agent/run_service.py` manages `coding_agent_runs` rows.

### Table — `coding_agent_runs`

| Column | Purpose |
|---|---|
| `id` | UUIDv7 PK. |
| `org_id` | Soft FK — org-scoped queries. |
| `run_id` | Soft FK — links to the pipeline run. |
| `stage_execution_id` | Soft FK — links to the stage execution (e.g. the review stage). |
| `agent_command_id` | FK to `agent_commands.id`. |
| `command_kind` | Command kind string (`"InvokeClaudeCode"` or `"InvokeCodex"`). |
| `plugin_id` | Plugin that issued the run (sink resolves which plugin parses the terminal event). |
| `status` | `running` → `success` or `failure`. |
| `tokens_in` / `tokens_out` | `NOT NULL DEFAULT 0`. Written from `Usage.parse_result`. |
| `duration_ms` | Wall-clock duration (ms). |
| `exit_code` | Process exit code (nullable). |
| `started_at` | Set at `create_run`. |
| `completed_at` | Set at `finalize_run`. |

### Service functions

- `create_run(...)` — inserts with `status=running`, flushes, returns the run id. In `__all__`; cross-module tests (e.g. `agent_gateway/test/`) seed run rows via this public function.
- `get_run_ref_for_command(agent_command_id, *, session)` — returns `(run_id, plugin_id)`; used by the run sink to resolve which plugin parses the terminal event.
- `finalize_run(run_id, *, usage, duration_ms, activity, exit_code, status, session)` — updates status, tokens, duration, activity blob.
- `get_stage_activity(run_id, stage_execution_id, *, session)` — public; two-hop: resolve the coding-agent run id, then read the `coding_agent_activity` JSONB payload. Returns `None` when absent (partition TTL, no run).
- `get_run_id_for_command(agent_command_id, *, session)` — internal helper (not in `__all__`); lookup by command id. Reachable via direct submodule import within `core/coding_agent/test/`.
- `get_run_id_for_stage(run_id, stage_execution_id, *, session)` — internal helper (not in `__all__`); lookup by `(run_id, stage_execution_id)`. Reachable via direct submodule import within `core/coding_agent/test/`.

### `AgentRunSink` (IoC seam)

`core/agent_gateway` defines the `AgentRunSink` Protocol. `core/coding_agent.__init__` registers `CodingAgentRunSinkImpl()` at import.

- **Terminal events** — `record_agent_event` calls `handle_terminal_event` on every terminal `AgentEvent`; for invoke kinds (`InvokeClaudeCode`, `InvokeCodex` — matched via `_INVOKE_KINDS`) it resolves the plugin via `get_run_ref_for_command`, calls `plugin.parse_result(outputs)`, then calls `finalize_run`. Returns an `AgentEventEnrichment` (`output` + `error_message`) for the downstream run.
- **Progress events** — `record_agent_event` also calls `handle_progress_event(*, org_id, run_id, event)` on every `progress` AgentEvent. The sink looks up the `coding_agent_runs` row for `event.command_id`, resolves the plugin via `plugin_id`, calls `plugin.parse_activity_line(event.stream_line)`, and if the result is non-`None`, publishes the normalized `ActivityEvent` dict to `core/sse.publish_workspace_activity` on the `{org_id}:workspace_activity:{run_id}` channel.

See [core_agent_gateway.md](core_agent_gateway.md).

### Table — `coding_agent_activity`

Partitioned RANGE on `created_at` (weekly child partitions, ~4-week TTL). One row per finalized run; `payload` is the `ActivityLog` JSONB blob. Daily `@scheduled` task `coding_agent_activity_partition_maintenance` (in `partition_maintenance.py`) keeps the partition window rolling.

| Column | Purpose |
|---|---|
| `run_id` | FK to `coding_agent_runs.id` (`ON DELETE CASCADE`). |
| `created_at` | Partition key + part of composite PK. |
| `org_id` | Soft FK. |
| `payload` | JSONB `ActivityLog` blob (`{"events": [...]}`). |

## Install state

`app/core/coding_agent/installs.py` owns per-org coding-agent installs and the `/api/coding-agents` HTTP routes (in `installs_web.py`).

### Value objects

- `CodingAgentInstall{org_id, plugin_id, settings, created_at, updated_at, created_by}` — one row per org-plugin pair.
- `CodingAgentAlreadyInstalledError` — raised by `install_coding_agent` when the row already exists.
- `CodingAgentNotInstalledError` — raised by `update_coding_agent_settings` when no row exists.

### Service functions

All take `session: AsyncSession` as the first positional argument; they flush but do not commit. Callers commit.

- `list_coding_agents(session, org_id) -> list[CodingAgentInstall]` — all installs for the org.
- `install_coding_agent(session, *, org_id, plugin_id, settings, actor, created_by=None) -> CodingAgentInstall` — inserts row, audits `coding_agent.installed`. Raises `CodingAgentAlreadyInstalledError` on conflict.
- `update_coding_agent_settings(session, *, org_id, plugin_id, settings, actor) -> CodingAgentInstall` — replaces `settings` + stamps `updated_at`, audits `coding_agent.settings_updated`. Raises `CodingAgentNotInstalledError` when no row exists.
- `uninstall_coding_agent(session, *, org_id, plugin_id, actor) -> bool` — deletes row, audits `coding_agent.uninstalled`. Returns `True` if deleted, `False` if not found (no audit on no-op).

### Table — `org_coding_agents`

Composite PK `(org_id, plugin_id)`. One row per installed plugin per org.

| Column | Purpose |
|---|---|
| `org_id` | FK → `orgs.id` (CASCADE delete). |
| `plugin_id` | Plugin registry key (`"claude_code"`, etc.). |
| `settings` | JSONB blob; plugin-shaped (validated by `plugin.validate_settings` before write). |
| `created_at` / `updated_at` | Server-default `now()`. |
| `created_by` | Nullable FK → `users.id` (SET NULL). |

### HTTP routes — `/api/coding-agents`

`installs_web.py` registers under `module_name="coding_agent"` with `url_prefix="/api/coding-agents"`. All routes are `RouteSecurity.ORG_SCOPED`.

| Method | Path | Action | Notes |
|---|---|---|---|
| `GET` | `/api/coding-agents` | `CODING_AGENT_READ` | Returns list of installs; each row carries `display_name`, `models`, `efforts` from the plugin. |
| `GET` | `/api/coding-agents/available` | `CODING_AGENT_READ` | Returns all registered plugins (`plugin_id`, `display_name`); used to populate the "Add coding agent" picker. |
| `GET` | `/api/coding-agents/{plugin_id}/skills-bundle` | `CODING_AGENT_READ` | Returns `application/zip` — the vendor-native skills bundle for the given plugin. Built on the fly by `build_skills_bundle_zip(plugin_id)`. `Content-Disposition: attachment; filename="yaaos-pipeline-skills-{plugin_id}.zip"`. 404 on unknown plugin; 500 if `YAAOS_SKILLS_SOURCE_DIR` is missing from the image. |
| `POST` | `/api/coding-agents` | `CODING_AGENT_WRITE` | Installs a plugin; calls `plugin.validate_settings` before write; 409 on duplicate. |
| `PATCH` | `/api/coding-agents/{plugin_id}` | `CODING_AGENT_WRITE` | Replaces settings; calls `plugin.validate_settings`; 404 when not installed. |
| `DELETE` | `/api/coding-agents/{plugin_id}` | `CODING_AGENT_WRITE` | Uninstalls; 404 when not installed. |

## Data owned

- In-memory: plugin registry (`CodingAgentRegistry` in `ContextVar`).
- Persistent: `org_coding_agents`, `coding_agent_runs`, `coding_agent_activity` (partitioned).

## How it's tested

- `app/core/coding_agent/test/test_registry.py` — register/get/duplicate-rejection; `set_coding_agents_for_tests` isolation.
- `app/core/coding_agent/test/test_protocol_surface_service.py` — asserts exact `__all__` set (including `StageOptions`, install-state symbols, skills-bundle VOs, and `build_skills_bundle_zip`), Protocol has exactly `compile_invocation` + `parse_result` + `parse_activity_line` + `validate_settings` + `stage_options` + `skill_path` + `render_skill_bundle`, retired names not importable.
- `app/core/coding_agent/test/test_skills_bundle.py` — unit: frontmatter parsing, `_load_skill_sources` builds `SkillSource` (pipeline-* only, extra_files), `_load_agent_sources` builds `AgentSource` (pipeline-* only), `ClaudeCodePlugin.render_skill_bundle` passthrough, `CodexPlugin.render_skill_bundle` — paths, `AGENTS.md` authorization sentence, TOML defensive restatement, TOML structure. Service: `build_skills_bundle_zip` returns valid ZIP for both plugins; 404 on unknown plugin.
- `app/core/coding_agent/test/test_coding_agents.py` — install service + `/api/coding-agents` endpoint tests: install/list, audit emission, duplicate → 409, settings update + audit, uninstall + audit, role enforcement (member → 403, unauthenticated → 401), `validate_settings` rejection → 422.
- `app/core/coding_agent/test/test_dispatch_invocation_service.py` — service: `dispatch_invocation` returns exactly the caller-supplied `command_id` (a UUIDv7), inserts run row, resolvable via `get_run_id_for_command`, and sets `skill_path` from `plugin.skill_path(invocation.skill)` and `command_kind` from `plugin.command_kind`.
- `app/core/coding_agent/test/test_run_lifecycle_service.py` — service: create/finalize round-trip, activity blob, `get_step_activity`.
- `app/core/coding_agent/test/test_sink_uses_parse_result_service.py` — service: `CodingAgentRunSinkImpl.handle_terminal_event` calls `plugin.parse_result`, writes run row, returns `output` + `error_message`; kinds outside `_INVOKE_KINDS` return `None`.
- `app/plugins/claude_code/test/test_stream_parsing.py` — `_parse_usage` + `_render_activity_log` private helpers.
- `app/plugins/claude_code/test/test_build_invocation_method.py` — `ClaudeCodePlugin.compile_invocation` unit tests: any skill name compiles, prompt renders the stage-invocation-context fields, missing required context keys raise `CodingAgentError`.
- `app/plugins/claude_code/test/test_parse_result_method.py` — `ClaudeCodePlugin.parse_result` unit tests.
- `app/plugins/claude_code/test/test_parse_activity_line.py` — `ClaudeCodePlugin.parse_activity_line` unit tests: blank line → `None`; `assistant_message` type → `kind="assistant_message"`, `message` set; tool-use `input_start` type → `kind="tool_call_started"`, `detail` has `tool` key; unrecognized type → `kind="unknown"`, `message` from raw line.
- `app/plugins/codex/test/test_parse_result_method.py` — `CodexPlugin.parse_result` unit tests.
- `app/plugins/codex/test/test_parse_activity_line.py` — `CodexPlugin.parse_activity_line` unit tests: `item.completed` (assistant_message) → `kind="assistant_message"`; `turn.completed` → `kind="result"`; blank/unrecognized → `None`.
- `app/plugins/codex/test/test_validate_settings.py` — `CodexPlugin.validate_settings` unit tests: empty settings accepted, unknown keys rejected.
