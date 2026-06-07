# plugins/claude_code

> Wraps the Claude Code CLI as a `domain/coding_agent.CodingAgentPlugin`. Owns parent dispatcher prompt, subagent installer, output parsing, and Anthropic credentials.

## Scope

Implements `CodingAgentPlugin` — in-process methods (`review`, `incremental_review`, `verify_fix`, `stale_check`, `answer_question`) and remote-dispatch methods (`build_review_invocation`, `parse_review_output`, `review_preflight_steps`), plus `validate_config` and `health_check`. Spawns one parent reviewer per run (in-process path) or produces an exec spec for the remote agent (remote-dispatch path). Returns `ReportedFinding`s (raw strings) — the reviewer's `publish_findings` validates and posts them via `vcs.post_finding`. Knows nothing about tickets, review jobs, audit log, or workspace paths.

## Module architecture

Singleton holds no decrypted credentials — settings loaded per-invocation so key rotation takes effect immediately.

### Subagent installer (`installer.py`)

`install_subagents()` reads the six markdown files in `app/domain/coding_agent/reviewers/` and writes them to `$HOME/.claude/agents/yaaos-*.md` with YAML frontmatter prepended. Idempotent; called from `bootstrap()`. The `yaaos-` prefix prevents collisions with any repo's own agents while leaving a deliberate override seam.

### Prompt files

Per-mode prompts live in `prompts/` (`full_review.md`, `incremental_review.md`, `verify_fix.md`, `stale_check.md`, `answer_question.md`). Loaded once at import via `_load_prompt(name)`. The `.md` files are the versioned source of truth.

### `build_review_invocation` — remote-dispatch exec spec

Takes a `ReviewContext{org_id, repo_external_id, pr_external_id, head_sha, base_sha, output_schema}`. Decrypts the Anthropic key; assembles argv (`claude --print --output-format=stream-json …`), prompt (instructions + `git diff base..head` directive + `output_schema` appendix), and env (`ANTHROPIC_API_KEY`). Returns `Invocation{kind="code-review", exec: ExecSpec, limits: InvokeClaudeCodeLimits(1200s)}`. The exec spec is serialized into the `InvokeClaudeCodeCommand` the Go agent executes.

### `parse_review_output` — stream-json parse

Receives raw stdout from the agent's terminal event. Finds the terminal `type=result` event, extracts `result`, validates against `_FindingDraftList`. Raises `ValueError` on any failure — `PostFindings` gates on this and returns `schema_invalid` failure when it raises.

### `review` — in-process pipeline (retained)

Retained for future re-introduction. Builds the same argv/prompt as `build_review_invocation` but runs via the workspace `run_coding_agent_cli` call directly. Same `_prepare_invocation` → `_run_and_parse_envelope` structure as the other in-process modes.

1. **Load settings + build argv** (`_prepare_invocation`) — decrypts Anthropic key; assembles argv with `Task` in allowed tools, restricted Bash git commands. Default timeout `_DEFAULT_TIMEOUT_SECONDS = 1200`.
2. **Assemble prompt** — review instructions + schema appendix. Diff is **not** inlined — agent runs `git diff base_sha..HEAD` itself.
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
