# plugins/claude_code

> Wraps the Claude Code CLI as a `core/coding_agent.CodingAgentPlugin`. Owns exec-spec assembly and stdout parsing.

## Scope

Implements `CodingAgentPlugin` — two methods (`build_invocation`, `parse_result`) plus `plugin_id = "claude_code"`. Owns the `claude_code_settings` and `claude_code_repos` tables and the `/api/claude_code/repos` HTTP routes. Knows nothing about tickets, review jobs, audit log, or workspace paths.

The Claude Code CLI runs exclusively inside the remote WorkspaceAgent (the customer-deployed Go binary in `apps/agent/`). The backend never execs the CLI directly.

Does NOT own: `ReviewContext`, `FindingDraftList`, `parse_review_output`, or review output validation — those live in `domain/reviewer`.

## Module architecture

Singleton `_plugin = ClaudeCodePlugin()` holds no decrypted credentials — settings are loaded per-invocation so key rotation takes effect immediately. No per-call state; no locks.

### `build_invocation`

Takes a `core/coding_agent.Invocation{skill, model, effort, context, wallclock_seconds}`. Supports only `skill="pr_review"`. Validates `context` carries required keys (`org_id`, `repo_external_id`, `pr_external_id`, `head_sha`, `base_sha`). Reads `context["anthropic_api_key"]` if supplied (production callers inject it from `byok`). Assembles argv (`claude --print --output-format=stream-json --verbose --model <model> --effort <effort> --allowed-tools=…`), a review prompt (with base/head SHA + strict JSON output directive), and `env["ANTHROPIC_API_KEY"]`. Returns `InvokeCodingAgent{argv, env, stdin, wallclock_seconds}`. Raises `CodingAgentError` on unknown skills or missing context keys.

### `parse_result`

Takes a terminal AgentEvent `outputs` dict. Reads `stdout` and `exit_code`. Delegates to `_parse_usage(stdout)` and `_render_activity_log(stdout)` internally. Reads `duration_ms` from the terminal `type=result` stream event inside stdout. Returns `RunResult{output, error_message=None, usage, duration_ms, exit_code, activity}`. Never raises on missing keys.

### Stream-json parsing helpers

Three module-private functions, each operating on `stdout: str`:

- `_parse_stream_events(stdout)` — newline-delimiter JSON parser; skips blank/unparseable lines silently.
- `_parse_usage(stdout)` → `Usage` — finds the last `type=result` event, extracts `usage.input_tokens` and `usage.output_tokens`. Returns empty `Usage()` when absent.
- `_render_activity_log(stdout)` → `ActivityLog` — walks every parseable event through `_render_activity`, drops nulls, stamps monotonic `seq`. Returns `ActivityLog(events=[])` for empty stdout.

`_render_activity` converts one stream event into a user-facing `{seq, ts, kind, message, detail}` dict. Trust-boundary discipline: `tool_result` blocks (raw workspace file/Bash output) appear as size-and-error-flag only — never the body content. `Edit`/`Write`/`MultiEdit`/`NotebookEdit` input dicts keep `file_path` only. This redaction lives in `_safe_tool_input`.

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
- `test_settings_schema.py` — settings round-trip on `{mcp_proxy_ids}`.
- `test_defaults_endpoint.py` — auth gate + response shape for `GET /api/claude_code/defaults`.

CLI subprocess + output parsing + Anthropic auth probe exercised end-to-end by e2e tests with `YAAOS_CODING_AGENT_STUB=1`.
