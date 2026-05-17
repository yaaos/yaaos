# Glossary

Shared vocabulary across backend and frontend. These terms appear in code, URLs, and UI; this list keeps usage consistent.

| Term | Meaning |
|---|---|
| **Org** | Tenant boundary. One org today; every domain function takes `org_id`. |
| **Ticket** | yaaos's unit of work. References a PR; flows `open` → `in_review` → `complete` / `abandoned`. |
| **PR** | The VCS-side artefact. Mirrored from GitHub into `pull_requests`. Owned by `domain/pull_requests`. |
| **Review job** | One review run for one PR. One per ticket. States: `queued` → `running` → `posted` / `failed` / `skipped` / `cancelled`. Owned by `domain/reviewer`. |
| **Subagent** | A shipped reviewer specialty (`yaaos-architecture`, `yaaos-security`, `yaaos-line-level`, `yaaos-tests`, `yaaos-docs`, `yaaos-skill`). Defined as markdown in `app/domain/coding_agent/reviewers/`, installed into `~/.claude/agents/` by the coding-agent plugin, dispatched by the parent reviewer via the Task tool. Each finding carries the subagent that surfaced it in `source_agent`. |
| **Coding agent** | The external CLI yaaos shells out to (Claude Code). Protocol: `domain/coding_agent.CodingAgentPlugin` with `review`. yaaos never calls an LLM directly. |
| **Workspace** | Provisioned environment where the CLI runs (tempdir + git clone today). Lifecycle owned by `core/workspace`; provisioning via `WorkspaceProvider` plugins. |
| **Finding** | One reviewer comment: `file`, `line_start`/`line_end`, `severity` (`must-fix` / `nit` / `suggestion` / `info`), `title`, `body`, optional `rationale`, optional `snippet`, optional `source_agent` (which subagent surfaced it). Vendor-neutral; defined in `domain/vcs`. |
| **Lesson** | Repo-scoped institutional memory: `{title, body, source_pr_url}`, 1000-char body cap. Surfaces in agent prompts; UI shows applied-lesson chips. Owned by `domain/memory`. |
| **Plugin** | Vendor-specific implementation of a Protocol in `domain/` or `core/`. Three Protocols: `VCSPlugin` (github), `CodingAgentPlugin` (claude_code), `WorkspaceProvider` (in_process_workspace). Vendor SDKs only allowed in `apps/backend/app/plugins/`. |
| **Verdict** | Terminal state of a posted review: `APPROVED` / `CHANGES_REQUESTED` / `COMMENT`. Decided by the CLI; returned in `ReviewResult.state`. |
| **Skip reason** | Why a job didn't run: `fork`, `bot_author`, `trivial_diff`, `too_large`, `secrets_detected`, `ui_cancel`, `superseded`. Recorded on the row and rendered in UI. |
| **Audit entry** | One row in `audit_log`. Append-only. Kind is `<entity>.<verb_past>`. Payload is a Pydantic model owned by the writing module. |
| **Actor** | Who initiated an action — `{kind: "github_user" | "agent" | "system", login?, agent_id?}`. Required on every audit entry. Defined in `core/primitives`. |
| **Onboarding** | Dashboard's pre-ready state. Two checks: GitHub App installed + Anthropic API key set (validated by live probe). Computed by `domain/settings.get_onboarding_status()`. |
