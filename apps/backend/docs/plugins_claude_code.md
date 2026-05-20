# plugins/claude_code

> Wraps the Claude Code CLI as a `domain/coding_agent.CodingAgentPlugin`. Owns parent dispatcher prompt, subagent installer, output parsing, and Anthropic credentials.

## Purpose

Adapter for [Claude Code](https://docs.claude.com/en/docs/claude-code), the only coding-agent CLI today. Implements `review`, `incremental_review`, `verify_fix`, `stale_check`, `answer_question`, `validate_config`, `health_check`. Spawns ONE parent reviewer per review run; the parent dispatches `yaaos-*` subagents (installed locally) via the Task tool and synthesizes their findings. Owns prompt assembly, the plugin-internal output schema (`_FindingDraftDto`, `_FindingDraftList`), the subagent installer, and Anthropic credentials. Returns `FindingDraft`s (`source_agent` populated by the parent's synthesis pass) — the reviewer aggregate handles admission and the conversion back to `vcs.Finding` for posting. Knows nothing about yaaos tickets, review jobs, audit log, or the workspace's working directory.

## Public interface

- Singleton `ClaudeCodePlugin` registered into `domain/coding_agent` at `bootstrap()`; also registers `anthropic_key_set` onboarding contributor, the `anthropic` BYOK validator (`byok_validator.validate_anthropic_key`), and installs subagent definitions.
- M03 settings model in `settings_schema.py`: orchestrator + sub-agents validated as a single Pydantic tree — agents list bounded to 1..8, sub-agent names unique within `agents`, name length ≤ 64, `model`/`version`/`effort` checked against the enums in `defaults.py`. The plugin's `validate_settings({})` substitutes the code defaults so the picker's install path doesn't have to pre-populate the JSONB.
- Side-effect import of `web.py` wires HTTP routes (prefix `/api/claude_code`) and an `on_startup` hook:
  - `POST /api_key` (`public_route`) — set/rotate the Anthropic key (M01 setup flow; M03 BYOK at `/api/byok/anthropic` supersedes for per-org storage).
  - `GET /health` (`public_route`) — wraps `health_check()`.
  - `GET /defaults` (`CODING_AGENT_READ`) — orchestrator + sub-agent defaults + model/version/effort dropdown enums. Imported at request time so a code change to `defaults.py` surfaces on the next request — never cached at module load. Consumed by the bespoke Claude Code settings page to render "Reset to default" + "Overridden" badges.
  - `bootstrap_anthropic_env` (startup hook) — decrypts the stored key into `os.environ["ANTHROPIC_API_KEY"]` at app boot so direct LLM calls (e.g., `domain/reviewer/llm/classifier.classify_reply`) authenticate via LangChain's default env resolution. No-op if the env var is already set (Braintrust gateway, test env) or no row exists yet (pre-onboarding).
- Plugin credentials live under the plugin's own URL space, not a generic `/api/settings/*`.
- Domain code never imports this module; uses `domain/coding_agent`'s registry.

## Module architecture

Singleton constructed at import time. Holds no decrypted credentials — settings loaded per-invocation, so key rotation takes effect immediately.

### Subagent installer (`installer.py`)

`install_subagents()` reads the six markdown files in `app/domain/coding_agent/reviewers/` and writes them to `$HOME/.claude/agents/yaaos-*.md` with YAML frontmatter (`name`, `description`) prepended. Idempotent — fine to re-run on every backend startup. Called from `bootstrap()`.

Name prefix is mandatory: `yaaos-architecture`, `yaaos-security`, `yaaos-line-level`, `yaaos-tests`, `yaaos-docs`, `yaaos-skill`. Project-level agents in the target repo win over user-level in Claude Code's resolution — the prefix makes collisions impossible while leaving a deliberate override seam (a repo can ship its own `.claude/agents/yaaos-architecture.md` to replace ours).

Per-workspace install at provision time would be the M02+ Docker-workspace shape. Today there's one HOME shared by every review; startup-time install is correct and cheaper.

### Prompt files

Per-mode prompt content lives next to the plugin under `prompts/`: `full_review.md`, `incremental_review.md`, `verify_fix.md`, `stale_check.md`, `answer_question.md`. Loaded once at import via `_load_prompt(name)` (reads `prompts/{name}.md`, returns the string). The .md files are the versioned, reviewable source of truth; the runtime code only does string-formatting + appendix logic.

### `review`

Single parent invocation. The CLI is given the Task tool so the parent can dispatch subagents.

**Step 0 — materialize MCP context (`_materialize_mcp_config`):**

When `ReviewContext.agent_config["mcp"]` is set (populated by `domain/reviewer.queue._build_mcp_payload`), writes `.mcp.json` into the workspace via `Workspace.write_text`. The file lists each connected provider as an HTTP MCP server with `Authorization: Bearer <per-review-token>` and the URL pointing at `domain/mcp_proxy`'s `/api/mcp/<review_id>/<provider>` endpoint. Returns the per-server `mcp__<server>__<tool>` allowlist additions (known read tools + the row's `allowed_tools` write subset) that `_prepare_invocation` appends to `--allowed-tools`. Defense-in-depth — the proxy is the actual gate. The full-review prompt header gains an MCP context block listing the connected providers and the broken-creds fallback instruction.

**Step 1 — load settings + build argv (`_prepare_invocation`):**

`_load_settings_for_invocation` selects the single `claude_code_settings` row and decrypts the Anthropic key. Returns `(api_key, cli_path)`. No key or no CLI path: early `AGENT_ERROR`. The default timeout is a module constant (`_DEFAULT_TIMEOUT_SECONDS = 1200`); per-call override via `agent_config["timeout_seconds"]`.

Argv: `claude --print --output-format=stream-json --verbose --permission-mode=bypassPermissions --allowed-tools=Read,Glob,Grep,LS,NotebookRead,TodoWrite,WebFetch,WebSearch,Task,Bash(git diff:*),Bash(git log:*),Bash(git show:*),Bash(git blame:*),Bash(git ls-files:*),Bash(git rev-parse:*),Bash(git status) --model <alias> --effort <level>`. `Task` lets the parent dispatch yaaos-* subagents. `Bash` is restricted to read-only git commands so the agent can `git diff base_sha..HEAD` itself rather than yaaos inlining the diff into the prompt (saves tens of thousands of tokens on large PRs and avoids duplicating the diff across N subagent task briefs). No `Bash` for non-git, no `Write`, no `Edit`. Timeout: `agent_config["timeout_seconds"]` overrides the `_DEFAULT_TIMEOUT_SECONDS` module constant (1200s / 20 min) — sized for real-PR reviews where the parent fans out to multiple subagents. `--model` + `--effort` are hardcoded module constants (`_MODEL`, `_EFFORT`) at M01 — `opus` (alias resolves to latest Opus) and `medium`. UI to configure them is M02+ work; the resolved model name reported in the terminal `result` event is captured into `InvocationTelemetry.model` so consumers persist the actual name. `stream-json` (requires `--verbose`) emits one JSON event per line as work progresses — parsed inline so consumers can react live, with the same parsed stream also serving as the per-event log for stuck or timed-out runs.

Env: copy of `os.environ` with `ANTHROPIC_API_KEY` injected. Key never on argv.

**Step 2 — assemble prompt + schema appendix:**

`_assemble_review_prompt(ctx)` builds the parent-dispatcher prompt: explains the available `yaaos-*` subagents and their triggers (always-on vs conditional), instructs the parent to dispatch all relevant subagents in a single turn via parallel Task calls, collect findings tagged with `source_agent`, verify each finding by re-reading the cited code, drop hallucinated findings whose snippet doesn't match, rank, and emit one merged JSON. The diff itself is **not** inlined — instead the prompt names the base sha + head sha and instructs the agent to run `git diff base_sha..HEAD` (or per-file slices) itself using the restricted Bash access. PR title/body, optional repo-language block, and optional lessons are appended. `ctx.prior_yaaos_comment_bodies` is intentionally NOT surfaced — telling the agent to avoid duplicates fights the aggregate's fingerprint dedup (plan §10.10); the agent does a fresh analysis each run and the aggregate handles re-observation silently.

`_schema_appendix(_FindingDraftList)` appends a STRICT instruction with `_FindingDraftList.model_json_schema()` — plan §10.1 schema (severity: `blocker` / `major` / `minor` / `nit`; `rule_id`; `concrete_failure_scenario`; `confidence`; `file_path` / `line_start` / `line_end`). This is the only mechanism constraining output shape — Claude Code's `--output-format=json` controls the wrapper envelope, not content.

**Step 3 — run via workspace:**

Workspace owns subprocess lifecycle (`cwd`, process group, SIGTERM → 2s grace → SIGKILL). Prompt piped via stdin (avoids `ARG_MAX`). Plugin sees only `CodingAgentCliResult`.

`WorkspaceExecError` → `AGENT_ERROR`. `timed_out=True` → `TIMEOUT`. Non-zero exit → `AGENT_ERROR` with first stderr line.

**Step 4 — parse stream-json events:**

`--output-format=stream-json --verbose` emits one JSON event per line: `system` (init, reports `model` + `session_id`), `assistant` (model turn, may contain `tool_use` blocks — Task dispatches surface here with the target subagent name), `user` (tool_result blocks), terminal `result` (with `usage`, `modelUsage`, final `result` text). The workspace's `on_stream_line` callback hands each raw line to a plugin-local handler that parses it via `_parse_stream_events`, logs it via `_log_stream_event`, and renders it to an `ActivityEvent` via `_render_activity`; rendered events are dispatched to the caller's `on_activity` callback (consumed by `domain/reviewer` to buffer + persist + broadcast). The terminal `result` event populates `InvocationTelemetry` (including the resolved `model` reported by the CLI) and supplies `agent_text`. Missing `result` event → `AGENT_ERROR` with raw output captured.

Per-event logging runs on every code path including timeout and non-zero exit — the partial trace from a stuck run is the primary diagnostic. The log line `claude_code.stream.tool_use` with `tool=Task` shows which subagent was in flight; absence of multiple Task tool_use events in a single `claude_code.stream.assistant` turn indicates the parent is dispatching serially rather than in parallel.

`_render_activity(event)` maps each known stream-event shape to an `ActivityEvent(kind, message, detail)` with the user-facing `message` pre-rendered server-side. Unknown event types are logged + skipped — forward-compatible.

**Step 5 — strict-parse agent response:**

Strict JSON parse → validate against `_FindingDraftList`. No markdown-fence fallback. Failure → `PARSE_FAILURE` with raw text in `telemetry.raw_output`; reviewer can audit and re-prompt.

**Step 6 — convert to vendor-neutral types:**

Each DTO maps to a `coding_agent.FindingDraft` (carrying `source_agent` set by the parent's synthesis). `_compute_state_v2(findings)`: empty → `APPROVED`, any `blocker`/`major` → `CHANGES_REQUESTED`, else `COMMENT`. The reviewer aggregate (in `domain/reviewer/queue.py`) applies the §10.1 admission pipeline and converts admitted survivors to `vcs.Finding` for posting — that mapping lives in `_findingdrafts_to_raw` + `_raw_to_vcs_findings`, not here.

### `answer_question`

Triggered when the reply classifier picks the `question` intent (see `domain_reviewer.md` § Core user flows). Same `_prepare_invocation` → `_run_and_parse_envelope` pipeline as `verify_fix`, with three differences:

- **Tool surface is leaner.** `_prepare_invocation(allowed_tools_override=...)` swaps the `Task`-bearing toolset for `Read,Glob,Grep,LS,Bash(git diff:*),Bash(git log:*),Bash(git show:*),Bash(git blame:*),Bash(git ls-files:*),Bash(git rev-parse:*),Bash(git status)` — read-only repo + git, no subagent dispatch. The parent answers the developer's question itself.
- **Prompt is `prompts/answer_question.md`.** Receives the finding context, code at the anchor, the full conversation history, the question, and base+head SHAs (so the agent can `git diff` the PR itself if needed). Same reviewer-voice rules as findings.
- **Output schema is `_AnswerQuestionDto = {answer: str}`.** Single text field. No list, no severity, no anchor — the reviewer module posts the answer back as a yaaos reply on the existing thread.

### `validate_config`

Schema check only. Allowed keys: `timeout_seconds` (positive int). Unknown keys error. Model + effort are hardcoded module constants at M01.

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

- `claude_code_settings` — one row per org. Columns: `encrypted_anthropic_api_key`, `default_model` (optional), `cli_path` (optional).

## How it's tested

Unit tests in `app/plugins/claude_code/test/`:

- `test_prompt_and_state.py` — parent prompt assembly (subagent list, diff, lessons, prior-comment truncation) and verdict computation.
- `test_installer.py` — installer writes frontmatter, is idempotent, leaves unrelated files alone.
- `test_stream_parsing.py` — `_parse_stream_events` handles well-formed streams, blank lines, garbage interleaved with valid JSON, and partial streams (timeout case with no `result` event). `_log_stream_event` smoke-tests every event type and tolerates missing fields.

CLI subprocess + envelope parsing + Anthropic auth probe are exercised end-to-end by e2e tests with `YAAOS_CODING_AGENT_STUB=1` swapping in `StubCodingAgentPlugin`.
