# plugins/claude_code

> Wraps the Claude Code CLI as a `domain/coding_agent.CodingAgentPlugin`. Owns parent dispatcher prompt, subagent installer, output parsing, and Anthropic credentials.

## Scope

Implements `CodingAgentPlugin` (`review`, `incremental_review`, `verify_fix`, `stale_check`, `answer_question`, `validate_config`, `health_check`). Spawns one parent reviewer per run; the parent dispatches `yaaos-*` subagents via the Task tool and synthesizes findings. Returns `ReportedFinding`s (raw strings) — the reviewer's `publish_findings` validates and posts them via `vcs.post_finding`. Knows nothing about tickets, review jobs, audit log, or workspace paths.

## Module architecture

Singleton holds no decrypted credentials — settings loaded per-invocation so key rotation takes effect immediately.

### Subagent installer (`installer.py`)

`install_subagents()` reads the six markdown files in `app/domain/coding_agent/reviewers/` and writes them to `$HOME/.claude/agents/yaaos-*.md` with YAML frontmatter prepended. Idempotent; called from `bootstrap()`. The `yaaos-` prefix prevents collisions with any repo's own agents while leaving a deliberate override seam.

### Prompt files

Per-mode prompts live in `prompts/` (`full_review.md`, `incremental_review.md`, `verify_fix.md`, `stale_check.md`, `answer_question.md`). Loaded once at import via `_load_prompt(name)`. The `.md` files are the versioned source of truth.

### `review` — six-step pipeline

1. **MCP context** (`_materialize_mcp_config`) — if `ReviewContext.agent_config["mcp"]` is set, writes `.mcp.json` into the workspace. Each provider becomes an HTTP MCP server pointing at `domain/mcp_proxy`. Returns per-server `--allowed-tools` additions.

2. **Load settings + build argv** (`_prepare_invocation`) — decrypts Anthropic key; assembles the `claude --print --output-format=stream-json --verbose --permission-mode=bypassPermissions` command with allowed tools: `Read,Glob,Grep,LS,NotebookRead,TodoWrite,WebFetch,WebSearch,Task` + restricted Bash git commands. `Task` enables subagent dispatch. Bash is read-only git so the agent diffs itself rather than consuming inlined diffs. Default timeout `_DEFAULT_TIMEOUT_SECONDS = 1200`; overridable via `agent_config["timeout_seconds"]`.

3. **Assemble prompt** (`_assemble_review_prompt`) — parent-dispatcher prompt instructs parallel Task dispatches, finding verification, and a single merged JSON output. Diff is **not** inlined — agent runs `git diff base_sha..HEAD` itself. `ctx.prior_yaaos_comment_bodies` is intentionally excluded; the aggregate handles dedup. `_schema_appendix` appends `_FindingDraftList.model_json_schema()` as a strict output constraint.

4. **Run via workspace** — workspace owns subprocess lifecycle (SIGTERM → 2s → SIGKILL). `WorkspaceExecError` → `AGENT_ERROR`; `timed_out=True` → `TIMEOUT`.

5. **Parse stream-json events** — `--output-format=stream-json --verbose` emits per-line JSON. Plugin dispatches each to `on_activity` (persisted + broadcast by `domain/reviewer`). Partial stream on timeout/error is the primary diagnostic. `_render_activity` maps known event shapes to `ActivityEvent`; unknown types are logged + skipped.

6. **Strict-parse response** — JSON → `_FindingDraftList`. No markdown-fence fallback. Failure → `PARSE_FAILURE`. `_compute_state_v2`: empty → `APPROVED`; any `blocker`/`major` → `CHANGES_REQUESTED`; else `COMMENT`.

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

### Skill enumeration

Discovers all skills a repo can run reviews with, cached per `(org_id, repo_external_id)`.

- **Workflow:** `enumerate_skills_v1` (`enumerate_workflow.py`) — `ProvisionWorkspace → EnumerateSkills → CleanupWorkspace`, `finalizer_step_id="cleanup"`. Runs on a system-generated ticket (`type="skill_enumeration"`, `source="system"`) so the engine stays ticket-bound; system tickets are intentionally visible in the Tickets UI list.
- **Context provider:** `workflow_context.py` registers the generic `WorkflowContextProvider` for `skill_enumeration` tickets; it populates `WorkspaceTicketContext.clone_url` (from the payload's repo full-name) and `installation_token` (via `vcs.get_installation_token`). `core/workspace` never imports `vcs`; the plugin is the bridge.
- **Recipe (agent-side, in `apps/agent/internal/workspace/`):**
  1. Repo-local scan: `<clone>/.claude/skills/<dir>/SKILL.md` → `{name: <dir>, source: "repo", plugin_name: null}`.
  2. Plugin/marketplace install-then-scan: parse `<clone>/.claude/settings.json` for `extraKnownMarketplaces` + `enabledPlugins`; run `claude plugin marketplace add` per marketplace, `claude plugin install` per plugin (each call independent — failures log and skip).
  3. Cache scan: `~/.claude/plugins/cache/<marketplace>/<plugin>/skills/<skill>/SKILL.md` → `{name: "<plugin>:<skill>", source: "plugin", plugin_name: "<plugin>"}`.
  Repo-local always returns; plugin discovery is best-effort. The GitHub installation token is threaded via `GIT_ASKPASS` so private same-host marketplace fetches can authenticate.
- **Resume:** the `EnumerateSkills` WorkflowCommand's terminal event carries `{skills: SkillManifestEntry[]}`. `PersistSkillManifest` upserts into `claude_code_repos.skills` (JSONB), sets `enumerated_at`, emits SSE `skills_enumerated` (org channel, payload `repo_external_id`).
- **Endpoints:** `POST /api/claude-code/repos/{repo_external_id}/skills/refresh` starts the workflow; `GET /api/claude-code/repos` lists repos; `GET /api/claude-code/repos/{repo_external_id}/skills` reads the cached manifest. `SkillManifestEntry` is `{name, source: "repo"|"plugin", plugin_name: str|None}`.

## Data owned

`claude_code_settings` — one row per org: `encrypted_anthropic_api_key`, `default_model` (optional), `cli_path` (optional).

`claude_code_repos` — one row per `(org_id, repo_external_id)`. `skills` JSONB (default `[]`) holds the `SkillManifestEntry[]` manifest; `enumerated_at` records the last successful enumeration; `created_at`/`updated_at`. No `status` column — the workflow's own state is the source of truth for in-flight enumerations.

## How it's tested

Unit tests in `app/plugins/claude_code/test/`:
- `test_prompt_and_state.py` — prompt assembly and verdict computation.
- `test_installer.py` — installer writes frontmatter, is idempotent, leaves unrelated files alone.
- `test_stream_parsing.py` — `_parse_stream_events` handles well-formed streams, garbage interleaved with valid JSON, and partial streams (timeout case).

CLI subprocess + envelope parsing + Anthropic auth probe exercised end-to-end by e2e tests with `YAAOS_CODING_AGENT_STUB=1`.
