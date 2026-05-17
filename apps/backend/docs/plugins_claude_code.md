# plugins/claude_code

> Wraps the Claude Code CLI as a `domain/coding_agent.CodingAgentPlugin`. Owns parent dispatcher prompt, subagent installer, output parsing, and Anthropic credentials.

## Purpose

Adapter for [Claude Code](https://docs.claude.com/en/docs/claude-code), the only coding-agent CLI today. Implements `review`, `validate_config`, `health_check`. Spawns ONE parent reviewer per review run; the parent dispatches `yaaos-*` subagents (installed locally) via the Task tool and synthesizes their findings. Owns prompt assembly, the plugin-internal output schema (`_FindingDto`, `_FindingList`), the subagent installer, and Anthropic credentials. Converts agent text → `vcs.Finding` (with `source_agent` populated by the parent) before results leave the plugin. Knows nothing about yaaos tickets, review jobs, audit log, or the workspace's working directory.

## Public interface

- Singleton `ClaudeCodePlugin` registered into `domain/coding_agent` at `bootstrap()`; also registers `anthropic_key_set` onboarding contributor and installs subagent definitions.
- Side-effect import of `web.py` wires HTTP routes (prefix `/api/claude_code`):
  - `POST /api_key` — set/rotate the Anthropic key (`{api_key: str}`). Empty rejected with 400. Fernet-encrypts, upserts on `claude_code_settings`, invalidates the auth-probe cache.
  - `GET /health` — wraps `health_check()`.
- Plugin credentials live under the plugin's own URL space, not a generic `/api/settings/*`.
- Domain code never imports this module; uses `domain/coding_agent`'s registry.

## Module architecture

Singleton constructed at import time. Holds no decrypted credentials — settings loaded per-invocation, so key rotation takes effect immediately.

### Subagent installer (`installer.py`)

`install_subagents()` reads the six markdown files in `app/domain/coding_agent/reviewers/` and writes them to `$HOME/.claude/agents/yaaos-*.md` with YAML frontmatter (`name`, `description`) prepended. Idempotent — fine to re-run on every backend startup. Called from `bootstrap()`.

Name prefix is mandatory: `yaaos-architecture`, `yaaos-security`, `yaaos-line-level`, `yaaos-tests`, `yaaos-docs`, `yaaos-skill`. Project-level agents in the target repo win over user-level in Claude Code's resolution — the prefix makes collisions impossible while leaving a deliberate override seam (a repo can ship its own `.claude/agents/yaaos-architecture.md` to replace ours).

Per-workspace install at provision time would be the M02+ Docker-workspace shape. Today there's one HOME shared by every review; startup-time install is correct and cheaper.

### `review`

Single parent invocation. The CLI is given the Task tool so the parent can dispatch subagents.

**Step 1 — load settings + build argv (`_prepare_invocation`):**

`_load_settings_for_invocation` selects the single `claude_code_settings` row and decrypts the Anthropic key. Returns `(api_key, cli_path, default_timeout_seconds)`. No key or no CLI path: early `AGENT_ERROR`.

Argv: `claude --print --output-format=stream-json --verbose --permission-mode=bypassPermissions --allowed-tools=Read,Glob,Grep,LS,NotebookRead,TodoWrite,WebFetch,WebSearch,Task` plus optional `--model` / `--max-turns` from `agent_config`. `Task` is what lets the parent reviewer dispatch yaaos-* subagents — without it the parent can't fan out. No `Bash`, `Write`, `Edit`. `agent_config["timeout_seconds"]` overrides the 600s default. `stream-json` (requires `--verbose`) emits one JSON event per line as work progresses — we parse it post-hoc to log a per-event trace so stuck or timed-out runs leave readable diagnostics.

Env: copy of `os.environ` with `ANTHROPIC_API_KEY` injected. Key never on argv.

**Step 2 — assemble prompt + schema appendix:**

`_assemble_review_prompt(ctx)` builds the parent-dispatcher prompt: explains the available `yaaos-*` subagents and their triggers (always-on vs conditional), instructs the parent to dispatch via Task, collect findings tagged with `source_agent`, verify each finding by re-reading the cited code, drop hallucinated findings whose snippet doesn't match, rank, and emit one merged JSON. PR title/body, fenced diff, optional repo-language block, optional lessons, optional prior yaaos comments are appended.

`_schema_appendix(_FindingList)` appends a STRICT instruction with `_FindingList.model_json_schema()`. This is the only mechanism constraining output shape — Claude Code's `--output-format=json` controls the wrapper envelope, not content.

**Step 3 — run via workspace:**

Workspace owns subprocess lifecycle (`cwd`, process group, SIGTERM → 2s grace → SIGKILL). Prompt piped via stdin (avoids `ARG_MAX`). Plugin sees only `CodingAgentCliResult`.

`WorkspaceExecError` → `AGENT_ERROR`. `timed_out=True` → `TIMEOUT`. Non-zero exit → `AGENT_ERROR` with first stderr line.

**Step 4 — parse stream-json events:**

`--output-format=stream-json --verbose` emits one JSON event per line: `system` (init), `assistant` (model turn, may contain `tool_use` blocks — Task dispatches surface here with the target subagent name), `user` (tool_result blocks), terminal `result` (with `usage`, `total_cost_usd`, final `result` text). `_parse_stream_events(stdout)` parses every line (skipping blank / non-JSON noise); `_log_stream_event` emits one structured log entry per event with type-appropriate fields (tool name + subagent for `tool_use`, excerpt + is_error for `tool_result`, duration + turns + cost for `result`). The terminal `result` event populates `InvocationTelemetry` and supplies `agent_text`. Missing `result` event → `AGENT_ERROR` with raw output captured.

Per-event logging runs on every code path including timeout and non-zero exit — the partial trace from a stuck run is the primary diagnostic. The log line `claude_code.stream.tool_use` with `tool=Task` shows which subagent was in flight; absence of multiple Task tool_use events in a single `claude_code.stream.assistant` turn indicates the parent is dispatching serially rather than in parallel.

**Step 5 — strict-parse agent response:**

Strict JSON parse → validate against `_FindingList`. No markdown-fence fallback. Failure → `PARSE_FAILURE` with raw text in `telemetry.raw_output`; reviewer can audit and re-prompt.

**Step 6 — convert to vendor-neutral types:**

`_dto_to_finding(dto)` maps `_FindingDto` → `vcs.Finding`, carrying `source_agent` through. `_compute_state(findings)`: empty → `APPROVED`, any `must-fix` → `CHANGES_REQUESTED`, else `COMMENT`.

### `validate_config`

Schema check only. Allowed keys: `timeout_seconds` (positive int), `max_turns` (positive int), `model` (non-empty string). Unknown keys error. No model-id enumeration — Anthropic ships new ones often.

### `health_check`

Cascade:
1. No API key → `"anthropic api key not set"`.
2. No `claude` binary → `"claude binary not found"`.
3. Probe Anthropic via `_probe_anthropic_auth(api_key)`.

### Anthropic auth probe

Real `GET https://api.anthropic.com/v1/models` with configured key. `200` → ok. `401`/`403` → "anthropic api key is invalid". Other → error message naming the failure.

Cached in module-level `_AUTH_CACHE` keyed on `sha256(api_key)`, 5-minute TTL. `_set_anthropic_key` invalidates on rotation so a stale "healthy" can't be served.

**Stub-mode bypass.** When `YAAOS_CODING_AGENT_STUB` is set, probe short-circuits to ok for any non-empty key. The stub plugin (`testing_stub_coding_agent.md`) never calls Anthropic anyway.

### Onboarding contributor

`_onboarding_anthropic_key_set(org_id)` returns True iff encrypted key row exists AND the key authenticates via the cached probe.

### Concurrency

Singleton; concurrent `review` calls expected. Each spawns its own subprocess and reads its own settings row. No per-call state; no locks.

### Test-mode wrapping

This file never branches on test env vars. When `YAAOS_CODING_AGENT_STUB` is set, `app/main.py` calls `testing.stub_coding_agent.wrap_all_registered_plugins()` after `bootstrap()` runs. See `testing_stub_coding_agent.md`.

## Data owned

- `claude_code_settings` — one row per org. Columns: `encrypted_anthropic_api_key`, `default_model` (optional), `cli_path` (optional), `default_timeout_seconds` (default 600).

## How it's tested

Unit tests in `app/plugins/claude_code/test/`:

- `test_prompt_and_state.py` — parent prompt assembly (subagent list, diff, lessons, prior-comment truncation) and verdict computation.
- `test_installer.py` — installer writes frontmatter, is idempotent, leaves unrelated files alone.
- `test_stream_parsing.py` — `_parse_stream_events` handles well-formed streams, blank lines, garbage interleaved with valid JSON, and partial streams (timeout case with no `result` event). `_log_stream_event` smoke-tests every event type and tolerates missing fields.

CLI subprocess + envelope parsing + Anthropic auth probe are exercised end-to-end by e2e tests with `YAAOS_CODING_AGENT_STUB=1` swapping in `StubCodingAgentPlugin`.
