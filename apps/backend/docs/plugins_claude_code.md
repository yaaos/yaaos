# plugins/claude_code

> Wraps the Claude Code CLI as a `core/coding_agent.CodingAgentPlugin`. Owns output parsing and Anthropic credentials.

## Scope

Implements `CodingAgentPlugin` — in-process methods (`review`, `incremental_review`, `verify_fix`, `stale_check`, `answer_question`) and remote-dispatch methods (`build_review_invocation`, `parse_review_output`, `review_preflight_steps`), plus `validate_config` and `health_check`. Returns `ReportedFinding`s (raw strings) — the reviewer's `publish_findings` validates and posts them via `vcs.post_finding`. Knows nothing about tickets, review jobs, audit log, or workspace paths.

## Module architecture

Singleton holds no decrypted credentials — settings loaded per-invocation so key rotation takes effect immediately.

### Prompt files

Per-mode prompts live in `prompts/` (`incremental_review.md`, `verify_fix.md`, `stale_check.md`, `answer_question.md`). Loaded once at import via `_load_prompt(name)`. The `.md` files are the versioned source of truth.

### `build_review_invocation` — remote-dispatch exec spec

Takes a `ReviewContext{org_id, repo_external_id, pr_external_id, head_sha, base_sha, output_schema}`. Reads `skill_name` for the repo via `resolve_skill` — raises `CodingAgentError` if absent or empty. Decrypts the Anthropic key; assembles argv (`claude --print --output-format=stream-json …`), prompt (review instructions + `git diff base..head` directive + `output_schema` appendix), and env (`ANTHROPIC_API_KEY`). Returns `Invocation{kind=<skill_name>, exec: ExecSpec, limits: InvokeClaudeCodeLimits(1200s)}`. The exec spec is serialized into the `InvokeClaudeCodeCommand` the Go agent executes.

### `parse_review_output` — stream-json parse

Receives raw stdout from the agent's terminal event. Finds the terminal `type=result` event, extracts `result`, validates against `_FindingDraftList`. Raises `ValueError` on any failure — `PostFindings` gates on this and returns `schema_invalid` failure when it raises.

### `review` — in-process pipeline (retained)

Retained for future re-introduction. Builds the same argv/prompt as `build_review_invocation` but runs via the workspace `run_coding_agent_cli` call directly.

1. **Load settings + build argv** (`_prepare_invocation`) — decrypts Anthropic key; assembles argv. Default timeout `_DEFAULT_TIMEOUT_SECONDS = 1200`.
2. **Assemble prompt** — review instructions + schema appendix.
3. **Run via workspace** — `WorkspaceExecError` → `AGENT_ERROR`; `timed_out=True` → `TIMEOUT`.
4. **Parse stream-json events** — `_render_activity` maps known event shapes to `ActivityEvent`; unknown types skipped.
5. **Strict-parse response** — JSON → `_FindingDraftList`. `_compute_state_v2`: empty → `APPROVED`; any `blocker` → `CHANGES_REQUESTED`; else `COMMENT`.

### `answer_question`

Same `_prepare_invocation` → `_run_and_parse_envelope` pipeline as `verify_fix` with a leaner tool surface (no `Task`), the `answer_question.md` prompt, and `_AnswerQuestionDto = {answer: str}` output schema.

### `validate_config`

Schema-only. Allowed keys: `timeout_seconds` (positive int). Model + effort are hardcoded module constants.

### `health_check`

1. No API key → error. 2. No `claude` binary → error. 3. Probe `GET https://api.anthropic.com/v1/models`. `200` → ok; `401`/`403` → invalid key. Cached 5 minutes keyed on `sha256(api_key)`; invalidated on key rotation. When `YAAOS_CODING_AGENT_STUB` is set, probe short-circuits to ok.

### Concurrency

Singleton; each `review` call spawns its own subprocess and reads its own settings row. No per-call state; no locks.

### Test-mode wrapping

Never branches on env vars. When `YAAOS_CODING_AGENT_STUB` is set, `app/web.py` calls `testing.stub_coding_agent.wrap_all_registered_plugins()` after `bootstrap()`. See [testing_stub_coding_agent.md](testing_stub_coding_agent.md).

## Data owned

`claude_code_settings` — one row per org: `cli_path` (optional). Anthropic API key is stored in `byok_keys` (provider=`anthropic`), not here.

`claude_code_repos` — one row per `(org_id, repo_external_id)`. Columns: `skill_name` (nullable text — the SKILL.md identifier the agent should invoke), `created_at`, `updated_at`.

## HTTP routes

All under `/api/claude_code/`:

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/repos` | `CODING_AGENT_READ` | Live VCS repos joined with stored `skill_name`. Repo list comes from `core/vcs.list_installation_repos("github", org_id)` — never a direct github-plugin import. Returns `{repos: [{repo_external_id, skill_name}]}`. Repos in the live list but absent from DB included with `skill_name=null`; repos in DB but gone from the live list omitted. |
| `GET` | `/repos/{repo_external_id:path}` | `CODING_AGENT_READ` | Read skill name for one repo. `:path` type handles `owner/repo` slash. |
| `PUT` | `/repos/{repo_external_id:path}` | `CODING_AGENT_WRITE` | Write skill name for one repo; creates the row if absent. |

## How it's tested

Unit tests in `app/plugins/claude_code/test/`:
- `test_prompt_and_state.py` — verdict computation.
- `test_stream_parsing.py` — `_parse_stream_events` handles well-formed streams, garbage interleaved with valid JSON, and partial streams (timeout case).
- `test_settings_schema.py` — settings round-trip on `{mcp_proxy_ids}`.
- `test_defaults_endpoint.py` — auth gate + response shape for `GET /api/claude_code/defaults`.
- `test_repo_skill_service.py` — service tests (`@pytest.mark.service`): `resolve_skill`/`set_repo_skill` round-trips against real Postgres; unit tests: `build_review_invocation` raises when skill absent/empty, uses resolved skill name as `Invocation.kind`.

CLI subprocess + envelope parsing + Anthropic auth probe exercised end-to-end by e2e tests with `YAAOS_CODING_AGENT_STUB=1`.
