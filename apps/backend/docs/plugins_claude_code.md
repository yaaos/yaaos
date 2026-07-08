# plugins/claude_code

> Wraps the Claude Code CLI as a `core/coding_agent.CodingAgentPlugin`. Owns exec-spec assembly and stdout parsing.

## Scope

Implements `CodingAgentPlugin` — four methods (`compile_invocation`, `byok_requirement`, `parse_result`, `validate_settings`) plus `plugin_id = "claude_code"`. Owns the `claude_code_settings` and `claude_code_repos` tables and the `/api/claude_code/repos` HTTP routes. Knows nothing about tickets, review jobs, audit log, or workspace paths.

The Claude Code CLI runs exclusively inside the remote WorkspaceAgent (the customer-deployed Go binary in `apps/agent/`). The backend never execs the CLI directly.

Does NOT own skill-specific prompt content — every skill file (`.claude/skills/<skill>/SKILL.md` in the checkout) owns its own instructions. This plugin only renders the generic stage context (`domain/pipelines.StageInvocationContext`) every invocation carries, plus the engine-injected output schema; it has no per-skill knowledge and no skill allowlist.

## Module architecture

Singleton `_plugin = ClaudeCodePlugin()` holds no decrypted credentials — settings are loaded per-invocation so key rotation takes effect immediately. No per-call state; no locks.

### `compile_invocation`

Takes a `core/coding_agent.Invocation{skill, model, effort, context, wallclock_seconds}`. Works for any skill name — `invocation.skill` is untyped, resolved against the checkout by the agent's pre-spawn stat, not validated here. Validates `context` carries the fields every stage invocation supplies (`stage_name`, `input`, `artifact_path`); raises `CodingAgentError` when they're missing. `_render_stage_prompt` renders the full `StageInvocationContext` (input text, PR diff pointers, upstream artifacts, revision/re-entry text, prior findings, artifact write path) plus a strict-JSON-output directive built from the engine-injected `output_schema`, and tells the model which named skill to use. Assembles argv (`claude --print --output-format=stream-json --verbose --model <model> --effort <effort> --permission-mode=bypassPermissions`) — no `--allowed-tools` restriction; `bypassPermissions` already grants the full toolset, and tool scoping (e.g. a review skill staying read-only) is the skill file's own discipline, not a backend policy. Returns `InvokeCodingAgent{argv, env={}, stdin, wallclock_seconds}`. The Anthropic API key is NOT in `env` — it is delivered to the Go agent via `ConfigUpdate.byok_secrets["anthropic"]` and injected as `ANTHROPIC_API_KEY` at subprocess exec time.

### `byok_requirement`

Returns `"anthropic"` — the BYOK `provider_id` this plugin needs. Called by `core/coding_agent.build_byok_secrets_for_org` to look up the org's stored Anthropic key and include it in `AgentConfig.byok_secrets` on every ConfigUpdate.

### `parse_result`

Takes a terminal AgentEvent `outputs` dict. Reads `stdout` and `exit_code`. Delegates to `_parse_usage(stdout)` and `_render_activity_log(stdout)` internally. Reads `duration_ms` from the terminal `type=result` stream event inside stdout. Returns `RunResult{output, error_message=None, usage, duration_ms, exit_code, activity}`. Never raises on missing keys.

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

`_onboarding_anthropic_key_set(org_id)` — returns `True` iff a byok row exists AND the key probes ok. Registered as an onboarding contributor in `bootstrap()`.

### `bootstrap()`

Called once by `app/web.py` and `app/worker.py` at startup. Registers `_plugin` with `core/coding_agent.register_plugin`; registers `_onboarding_anthropic_key_set` as the `"anthropic_key_set"` onboarding contributor; registers `validate_anthropic_key` with `core/byok`.

### Test-mode wrapping

Never branches on `YAAOS_CODING_AGENT_STUB`. When that env var is set, `app/web.py` calls `testing.stub_coding_agent.wrap_all_registered_plugins()` after `bootstrap()`. See [testing_stub_coding_agent.md](testing_stub_coding_agent.md).

## Data owned

- `claude_code_settings` — one row per org: `cli_path` (optional; controls the binary name the remote agent resolves). Anthropic API key is stored in `byok_keys` (provider=`anthropic`), not here.
- `claude_code_repos` — one row per `(org_id, repo_external_id)`. Columns: `skill_name` (nullable text — reserved for per-repo skill overrides in future; currently unused), `created_at`, `updated_at`.

## HTTP routes

All under `/api/claude_code/`:

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/repos` | `CODING_AGENT_READ` | Live VCS repos joined with stored `skill_name`. Repo list from `core/vcs.list_installation_repos("github", org_id)`. Returns `{repos: [{repo_external_id, skill_name}]}`. Repos absent from DB included with `skill_name=null`; DB rows for gone repos omitted. |
| `GET` | `/repos/{repo_external_id:path}` | `CODING_AGENT_READ` | Skill name for one repo. `:path` type handles `owner/repo` slash. |
| `PUT` | `/repos/{repo_external_id:path}` | `CODING_AGENT_WRITE` | Upsert skill name for one repo. |

## How it's tested

Unit tests in `app/plugins/claude_code/test/`:
- `test_stream_parsing.py` — `_parse_stream_events` + `_parse_usage` + `_render_activity_log` private helpers: well-formed streams, garbage interleaved with valid JSON, partial streams (timeout case).
- `test_build_invocation_method.py` — `compile_invocation`: any skill name compiles, prompt renders the stage-invocation-context fields (input, PR pointers, strict-JSON-output directive), missing required context keys raise `CodingAgentError`.
- `test_settings_schema.py` — settings round-trip on `{mcp_proxy_ids}`.
- `test_defaults_endpoint.py` — auth gate + response shape for `GET /api/claude_code/defaults`.
- `test_set_claude_code_plugin_for_tests.py` — `set_claude_code_plugin_for_tests` swaps and restores the singleton for the block.

`set_claude_code_plugin_for_tests` (exported from `app.plugins.claude_code`) is the test seam for swapping the singleton `ClaudeCodePlugin` instance.

CLI subprocess + output parsing + Anthropic auth probe exercised end-to-end by e2e tests with `YAAOS_CODING_AGENT_STUB=1`.
