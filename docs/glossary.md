# Glossary

> Shared vocabulary across backend and frontend. Terms appear in code, URLs, and UI.

| Term | Meaning |
|---|---|
| **Org** | Tenant boundary. UUID PK + immutable `slug`. Every non-user row is `org_id`-scoped. Soft-deleted via `archived_at`. |
| **Ticket** | yaaos unit of work. References a PR; flows `open → in_review → complete / abandoned`. |
| **PR** | VCS-side artefact mirrored into `pull_requests`. Owned by `domain/pull_requests`. |
| **Review job** | One review run for one PR (`queued → running → posted / failed / skipped / cancelled`). Owned by `domain/reviewer`. |
| **Subagent** | A shipped reviewer specialty (`yaaos-architecture`, `-security`, `-line-level`, `-tests`, `-docs`, `-skill`). Markdown-defined in `app/domain/coding_agent/reviewers/`, installed into `~/.claude/agents/`. Each finding carries `source_agent`. |
| **Coding agent** | The external CLI yaaos shells out to (Claude Code). Protocol: `domain/coding_agent.CodingAgentPlugin`. yaaos never calls an LLM directly. |
| **Workspace** | Provisioned execution environment (tempdir + git clone). Lifecycle owned by `core/workspace` via `WorkspaceProvider`. |
| **Finding** | One reviewer comment: `file`, `line_start/end`, `severity` (`must-fix / nit / suggestion / info`), `title`, `body`, optional `rationale`, `snippet`, `source_agent`. Defined in `domain/vcs`. |
| **Lesson** | Repo-scoped institutional memory: `{title, body, source_pr_url}`, 1000-char body cap. Surfaces in agent prompts. Owned by `domain/lessons`. |
| **Plugin** | Vendor-specific implementation of a Protocol in `domain/` or `core/`. Three Protocols: `VCSPlugin` (github), `CodingAgentPlugin` (claude_code), `WorkspaceProvider`. Vendor SDKs only in `apps/backend/app/plugins/`. |
| **Verdict** | Terminal review state: `APPROVED / CHANGES_REQUESTED / COMMENT`. Decided by the CLI; returned in `ReviewResult.state`. |
| **Skip reason** | Why a job didn't run: `fork`, `bot_author`, `trivial_diff`, `too_large`, `secrets_detected`, `ui_cancel`, `superseded`. |
| **Audit entry** | One append-only row in `audit_log`. Kind: `<entity>.<verb_past>`. Payload: Pydantic model owned by the writing module. |
| **Actor** | Who initiated an action — `{kind: "github_user" \| "agent" \| "system" \| "user" \| "workspace" \| "sso", login?, agent_id?, user_id?, workspace_id?}`. Required on every audit entry. Defined in `core/audit_log`. |
| **Onboarding** | Dashboard pre-ready state. Two checks: GitHub App installed + Anthropic API key validated by live probe. Computed by `domain/orgs.get_onboarding_status()`. |
| **User** | Human identity. UUID PK, soft-deletable via `deactivated_at`. Owned by `core/identity`. |
| **Membership** | `(user_id, org_id)` link. Carries per-org `handle` and one `Role`. |
| **Role** | `owner ≥ admin ≥ member`. Compared via `role.covers(required)`. Minimums declared at `Depends(require(Action.X))` call sites. |
| **Session** | Opaque server-side row keyed by `sha256(raw_token)`. Cookie: `HttpOnly; SameSite=Lax; Secure`. Double-submit CSRF via per-session `csrf_token`. `sso_satisfied_for_org_id` + timestamp track 8h SSO TTL. |
| **Invitation** | Owner/Admin-issued offer for an external email. Token: `itsdangerous`-signed, 7-day TTL, single-use. Stored as `sha256(token)`; raw lives only in the invitation email. |
| **Provider** | OAuth identity provider. Plugins implement the `Provider` Protocol; return `ProviderProfile` (`external_subject`, `primary_email`, `email_verified`, `display_name`, `mfa_satisfied`). |
| **SSO** | SAML 2.0 per-org single sign-on. SP-initiated. Satisfied assertion → `sso_satisfied_for_org_id` on session. Middleware blocks org access when SSO is enabled and session isn't satisfied. |
| **Break-glass Owner** | SSO `exempt_owner`. Signs in via OAuth + TOTP when SSO is broken. Candidate must have a verified TOTP secret. Each bypass audits `break_glass_exempt_owner`. |
| **VCS plugin** | Implements `domain/vcs.VCSPlugin`. Ships `github` only. At most one per org; state on `orgs.vcs_plugin_id` + `orgs.vcs_settings`. |
| **Plugin install** | A specific `(org_id, plugin_id)` adoption. Mutations audit (`vcs.installed / vcs.cleared / coding_agent.installed / coding_agent.uninstalled`). |
| **Verified GitHub username** | `users.github_username`. Denorm written by the OAuth callback on every sign-in. Re-binding is "sign in with GitHub again." Never user-typed. |
| **Session-timeout override** | Nullable `orgs.session_timeout_override` (minutes). `require()` dep rejects sessions past `last_seen_at + override`. Null falls back to `SESSION_IDLE_TIMEOUT` (12h). |
| **Orchestrator** | The single parent Claude Code session that dispatches subagents via the Task tool and synthesizes findings. One per `claude_code` install. |
| **Sub-agent** | Focused review pass run as its own Claude Code session, dispatched by the orchestrator. 1..8 per install; names must be unique (enforced by Pydantic). |
| **BYOK** | Bring-your-own-key per `(org_id, provider)`. Ships `anthropic` only. Plaintext only inbound on POST + outbound from `core/byok.get/.validate`; DB stores Fernet ciphertext. |
| **MCP** | Model Context Protocol — JSON-RPC-over-HTTP shape hosted integrations (Linear, Notion) speak to coding-agent CLIs. yaaos proxies every MCP request via `domain/mcp_proxy`. |
| **MCP review token** | Per-review opaque bearer authenticating the CLI to the yaaos proxy. Minted at review start (`sha256` persisted, raw returned once), revoked before workspace teardown, swept hourly. 2h absolute TTL. |
| **Integration** | yaaos record of a connected hosted provider for an org: encrypted OAuth tokens, per-tool allowlist, status. Owned by `domain/integrations`; one row per `(org_id, provider)`. |
| **Hosted MCP** | MCP server at a provider (mcp.linear.app, mcp.notion.com). yaaos proxy forwards to `ProviderConfig.mcp_url`. |
| **Org service account** | Single upstream OAuth identity an org has connected per provider. MCP dispatch always audits `upstream_account="org_service_account"` — never the triggering developer. |
| **Allowlist** | Per-`(org_id, provider)` list of write tools the proxy will forward. Read tools always allowed; write tools opt-in (empty = read-only). |
| **Broken-creds** | Integration state when `last_refresh_status == "failed"`. Surfaces in six places (banner, email, audit, settings badge, Claude Code warning, review-output prefix). |
| **Upstream identity** | Display string the OAuth flow returned for the connected account. Stored on `mcp_credentials.upstream_identity` for UI; never used as auth principal. |
| **Intake** | Inbound surface turning an external signal into a ticket. `domain/intake` ships a typed registry keyed by name; `POST /api/intake/{type}` dispatches via the registry. `github_pr` is the only type today. |
| **WorkspaceAgent** | Customer-deployed Go binary in `apps/agent/` that holds source code locally and spawns workspace processes. Zero biz logic — all policy comes from control plane via AgentCommand payload. |
| **Workflow** | Typed Pydantic data structure: `name`, `version`, ordered `steps`, `entry_step_id`. Five ship. Owned by `core/workflow`. |
| **WorkflowCommand** | One unit of work within a workflow. Three categories: **Workspace** (parks in `awaiting_agent`), **Local** (inline), **HITL** (parks in `awaiting_human`). |
| **AgentCommand** | Wire message backend → WorkspaceAgent. Five kinds: `CreateWorkspace`, `WriteFiles`, `RefreshWorkspaceAuth`, `InvokeClaudeCode`, `CleanupWorkspace`. Defined in `apps/backend/openapi/agent-api.yaml`. |
| **AgentEvent** | Wire message WorkspaceAgent → backend. Kinds: `progress`, `completed_success`, `completed_failure`, `completed_skipped`. Terminal events resume parked workflow. |
| **WorkflowExecution** | In-flight workflow run. State: `pending → running → (awaiting_agent \| awaiting_human)* → done \| failed \| cancelled`. Carries `step_state` JSONB + `otel_trace_context`. |
| **Pending human decision** | HITL pause row in `pending_human_decisions`. Resumed via `core/workflow.resume_hitl()`; resolution + re-enqueue in one transaction. |
| **Outbox** | DB-atomic outbound queue (`outbox_entries`). Written in the caller's transaction; drained post-commit. Backs `core/tasks.enqueue`. |
| **`core/tasks`** | Thin taskiq + Redis wrapper. Three task names: `workflow.start_step`, `workflow.handle_agent_event`, `workflow.route_workflow`. |
| **Activity event** | High-frequency CodingAgent telemetry. Flows WebSocket → `core/sse` → SSE → UI. Never persisted; demand-pull. |
| **Stale-claim guard** | `POST /api/v1/commands/{id}/events` returns `410 Gone` when inbound `command_id` no longer matches `workspaces.current_command_id`. Agent abandons silently. |
| **Failure-report-precedes-disposal** | `core/workspace.release_claim` clears `current_command_id` but preserves `current_holder_workflow_id`. Workflows always resolve their workspace after claim release. |
| **Demand-pull** | Activity events only flow when ≥1 UI tab is subscribed. `SubscriberRegistry` issues `subscribe`/`unsubscribe` on `0 → 1` / `1 → 0` transitions. |
