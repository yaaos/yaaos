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
| **User** | A human identity. UUID PK, soft-deletable via `deactivated_at`. Distinct from `actor_kind=github_user` which is the GitHub-as-VCS actor on legacy rows. Owned by `domain/identity`. |
| **Membership** | Link between a `User` and an `Org`. Composite PK `(user_id, org_id)`. Carries a per-org `handle` and one of three `Role`s. Same user can be `@jack` in one org and `@jkora` in another. |
| **Role** | `owner ≥ admin ≥ member`. Compared via `role.covers(required)` only. Action-to-role minimums declared at `Depends(require(Action.X))` call sites. |
| **Session** | Opaque server-side row keyed by `sha256(raw_token)`. Cookie is `HttpOnly; SameSite=Lax; Secure`. Double-submit CSRF via the per-session `csrf_token`. `sso_satisfied_for_org_id` + timestamp track the 8-hour SSO TTL. |
| **Invitation** | Owner/Admin-issued offer for an external email to join an org. Token is `itsdangerous`-signed, 7-day TTL, single-use. Stored as `sha256(token)`; raw tokens live only in the invitation email. |
| **Provider** | OAuth identity provider. Plugins (`oauth_github`, `oauth_test`) implement the `Provider` Protocol. Returns a `ProviderProfile` (`external_subject`, `primary_email`, `email_verified`, `display_name`, `mfa_satisfied`). |
| **SSO** | SAML 2.0 per-org single sign-on. SP-initiated. Verified assertion → session `sso_satisfied_for_org_id`. Middleware blocks org access when SSO is enabled and the session isn't satisfied. |
| **Break-glass Owner** | Owner picked as the SSO `exempt_owner`. Can sign in via OAuth + TOTP when SSO is broken. Each bypass writes a `break_glass_exempt_owner` audit entry. The exempt-Owner candidate must already have a verified TOTP secret. |
