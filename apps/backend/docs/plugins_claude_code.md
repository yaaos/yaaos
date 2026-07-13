# plugins/claude_code

> Wraps the Claude Code CLI as a `core/coding_agent.CodingAgentPlugin`. Owns exec-spec assembly and stdout parsing.

## Scope

Implements `CodingAgentPlugin` — eight methods (`compile_invocation`, `build_command`, `parse_result`, `parse_activity_line`, `validate_settings`, `stage_options`, `skill_path`, `render_skill_bundle`) plus `plugin_id = "claude_code"`, `display_name = "Claude Code"`, and `command_kind = "InvokeClaudeCode"`. Owns the `claude_code_settings` table. Knows nothing about tickets, review jobs, audit log, or workspace paths. Skill selection is not this plugin's concern — a pipeline stage's own `skill_name` picks the skill (see [domain_pipelines.md](domain_pipelines.md)).

The Claude Code CLI runs exclusively inside the remote WorkspaceAgent (the customer-deployed Go binary in `apps/agent/`). The backend never execs the CLI directly.

Does NOT own skill-specific prompt content — every skill file (`.claude/skills/<skill>/SKILL.md` in the checkout) owns its own instructions. This plugin only renders the generic stage context (`domain/pipelines.StageInvocationContext`) every invocation carries, plus the engine-injected output schema; it has no per-skill knowledge and no skill allowlist.

## Module architecture

Singleton `_plugin = ClaudeCodePlugin()` holds no decrypted credentials — settings are loaded per-invocation so key rotation takes effect immediately. No per-call state; no locks.

### `compile_invocation`

Takes a `core/coding_agent.Invocation{skill, model, effort, context, wallclock_seconds}`. Works for any skill name — `invocation.skill` is untyped, resolved against the checkout by the agent's pre-spawn stat, not validated here. Validates `context` carries the fields every stage invocation supplies (`stage_name`, `input`, `artifact_path`); raises `CodingAgentError` when they're missing. Calls `domain/pipelines.render_stage_prompt` (the shared prompt renderer) with a `skill_directive` (`@<skill_path>` line) built from `skill_path(invocation.skill)`, which renders the full `StageInvocationContext` (input text, PR diff pointers, upstream artifacts, revision/re-entry text, prior findings, artifact write path) plus a strict-JSON-output directive from the engine-injected `output_schema`. Assembles argv (`claude --print --output-format=stream-json --verbose --model <model> --effort <effort> --permission-mode=bypassPermissions`) — no `--allowed-tools` restriction; `bypassPermissions` already grants the full toolset, and tool scoping (e.g. a review skill staying read-only) is the skill file's own discipline, not a backend policy. Returns `InvokeCodingAgent{argv, env={}, stdin, wallclock_seconds}`. The Anthropic API key is NOT in `env` — it is delivered to the Go agent via `ConfigUpdate.api_keys["anthropic"]` (forward-all from `core/coding_agent.build_api_key_secrets_for_org`) and injected as `ANTHROPIC_API_KEY` at subprocess exec time.

### `build_command`

Async. Builds the wire `InvokeClaudeCodeCommand` from a `core/coding_agent.CommandBuildContext` (envelope fields — `command_id`, `workspace_id`, `traceparent`, `skill_path`, `invocation_body`) and the compiled `InvokeCodingAgent`'s `wallclock_seconds`. `mcp_servers=()`, `result_spec={}`. No credential gate — the Anthropic API key travels via `ConfigUpdate.api_keys`, not a dispatch-time check. `invocation` and `session` are accepted for Protocol parity and unused. Not yet called by `dispatch_invocation` — see [core_coding_agent.md § `CodingAgentPlugin` Protocol](core_coding_agent.md#codingagentplugin-protocol).

### `stage_options`

Returns `StageOptions(models=MODELS, efforts=EFFORTS)` — the tuple of model IDs and effort levels the Anthropic Claude Code CLI accepts. Surfaced via the `/api/coding-agents` list endpoint so the SPA's stage editor can populate dropdowns without a separate fetch.

### `skill_path`

Returns `f".claude/skills/{skill_name}/SKILL.md"` — the conventional checkout-relative path for Claude Code skills. Called by `dispatch_invocation` when building the `skill_path` field on the enqueued command.

### `render_skill_bundle`

Passthrough renderer: emits the canonical `.claude/` tree as-is. Each `SkillSource` → `BundleFile(".claude/skills/{name}/SKILL.md", content)` + extra files. Each `AgentSource` → `BundleFile(".claude/agents/{name}.md", content)`. No format conversion. Called by `build_skills_bundle_zip` when building the `claude_code` skills bundle.

### `parse_result`

Takes a terminal AgentEvent `outputs` dict. Reads `stdout` and `exit_code`. Delegates to `_parse_usage(stdout)` and `_render_activity_log(stdout)` internally. Reads `duration_ms` from the terminal `type=result` stream event inside stdout. Returns `RunResult{output, error_message=None, usage, duration_ms, exit_code, activity}`. Never raises on missing keys.

### `parse_activity_line`

Decodes one raw `stream-json` newline into a normalized `ActivityEvent | None`. Blank lines return `None`. Parseable events map to `ActivityEvent.kind` via the same `_render_activity` helper used by `parse_result`'s `_render_activity_log` — but operates on a single line, not the full stdout blob. Unrecognized event types produce `kind="unknown"` with the raw line as `message`. Used by `CodingAgentRunSinkImpl.handle_progress_event` to normalize live `progress` AgentEvent frames before publishing to the workspace-activity SSE channel.

### `validate_settings`

Parses the raw settings dict through `ClaudeCodeSettings(extra="forbid")`. Unknown keys raise `ValueError` (Pydantic's `ValidationError` is a `ValueError` subclass, so `extra="forbid"` rejects foreign keys with a `ValueError`). Returns `model_dump(mode="python")` — a normalized dict with `mcp_proxy_ids: list[UUID]` (defaults to `[]`). Delegates to `settings_schema.validate_settings`; the plugin method is the Protocol entry point.

### Stream-json parsing helpers

Three module-private functions, each operating on `stdout: str`:

- `_parse_stream_events(stdout)` — newline-delimiter JSON parser; skips blank/unparseable lines silently.
- `_parse_usage(stdout)` → `Usage` — finds the last `type=result` event, extracts `usage.input_tokens` and `usage.output_tokens`. Returns empty `Usage()` when absent.
- `_render_activity_log(stdout)` → `ActivityLog` — walks every parseable event through `_render_activity`, drops nulls, stamps monotonic `seq`, constructs typed `ActivityEvent` instances. Returns `ActivityLog(events=[])` for empty stdout.

`_render_activity` converts one stream event into a raw dict with `{seq, ts, kind, message, detail}` — `ts` is a `datetime` object. `_render_activity_log` wraps each non-null result in `ActivityEvent(...)` which validates `kind` against the canonical six-value `Literal`. Trust-boundary discipline: `tool_result` blocks (raw workspace file/Bash output) appear as size-and-error-flag only — never the body content. `Edit`/`Write`/`MultiEdit`/`NotebookEdit` input dicts keep `file_path` only. This redaction lives in `_safe_tool_input`.

### Anthropic auth probe

`_probe_anthropic_auth(api_key)` — calls `GET https://api.anthropic.com/v1/models`. `200` → ok; `401`/`403` → invalid key. Cached 5 minutes keyed by `sha256(api_key)`. When `YAAOS_CODING_AGENT_STUB` is set the probe short-circuits to `(True, "ok (stub)")` — no outbound network call.

`_onboarding_anthropic_key_set(org_id)` — returns `True` iff an `org_api_keys` row exists AND the key probes ok. Registered as an onboarding contributor in `bootstrap()`.

### `bootstrap()`

Called once by `app/web.py` and `app/worker.py` at startup. Registers `_plugin` with `core/coding_agent.register_plugin`; registers `_onboarding_anthropic_key_set` as the `"anthropic_key_set"` onboarding contributor; registers `validate_anthropic_key` with `core/api_keys`.

### Test-mode wrapping

Never branches on `YAAOS_CODING_AGENT_STUB`. When that env var is set, `app/web.py` calls `testing.stub_coding_agent.wrap_all_registered_plugins()` after `bootstrap()`. See [testing_stub_coding_agent.md](testing_stub_coding_agent.md).

## Data owned

- `claude_code_settings` — one row per org: `cli_path` (optional; controls the binary name the remote agent resolves). Anthropic API key is stored in `org_api_keys` (provider=`anthropic`), not here.

## HTTP routes

None — `ClaudeCodePlugin` has no dedicated HTTP surface. Model/effort data is served by `GET /api/coding-agents` via `plugin.stage_options()` (see [core_coding_agent.md](core_coding_agent.md)).

## How it's tested

Unit tests in `app/plugins/claude_code/test/`:
- `test_stream_parsing.py` — `_parse_stream_events` + `_parse_usage` + `_render_activity_log` private helpers: well-formed streams, garbage interleaved with valid JSON, partial streams (timeout case).
- `test_build_invocation_method.py` — `compile_invocation`: any skill name compiles, prompt renders the stage-invocation-context fields (input, PR pointers, strict-JSON-output directive), missing required context keys raise `CodingAgentError`.
- `test_build_command.py` — `build_command`: returns `InvokeClaudeCodeCommand` with envelope fields from the `CommandBuildContext`, `limits.wallclock_seconds` from the compiled exec block, `mcp_servers=()`, `result_spec={}`.
- `test_settings_schema.py` — settings round-trip on `{mcp_proxy_ids}`.
- `test_defaults_endpoint.py` — asserts `GET /api/claude_code/defaults` returns 404 (the plugin exposes no HTTP surface), `GET /api/coding-agents` rows carry `display_name`/`models`/`efforts` from the plugin, and `GET /api/coding-agents/available` lists registered plugins.
- `test_set_claude_code_plugin_for_tests.py` — `set_claude_code_plugin_for_tests` swaps and restores the singleton for the block.

`set_claude_code_plugin_for_tests` (exported from `app.plugins.claude_code`) is the test seam for swapping the singleton `ClaudeCodePlugin` instance.

CLI subprocess + output parsing + Anthropic auth probe exercised end-to-end by e2e tests with `YAAOS_CODING_AGENT_STUB=1`.
