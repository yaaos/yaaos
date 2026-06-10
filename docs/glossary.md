# Glossary

> Shared vocabulary across backend and frontend. Terms appear in code, URLs, and UI.

| Term | Meaning |
|---|---|
| **Org** | Tenant boundary. UUID PK + immutable `slug`. Every non-user row is `org_id`-scoped. Soft-deleted via `archived_at`. |
| **Ticket** | yaaos unit of work. References a PR; flows `open → in_review → complete / abandoned`. |
| **PR** | VCS-side artefact mirrored into `pull_requests`. Owned by `domain/tickets` (a property of a ticket; table `pull_requests` unchanged). |
| **Review job** | One review run for one PR (`queued → running → posted / failed / skipped / cancelled`). Owned by `domain/reviewer`. |
| **Skill** | A customer-authored `SKILL.md` file checked into the repo, identified by a short name (e.g. `code-review`). The WorkspaceAgent passes the name to Claude Code via `--skill`; Claude Code locates and executes the file. Configured per-repo on the Coding Agents settings page; stored as `claude_code_repos.skill_name`. |
| **Subagent** | A Claude Code sub-agent. The review skill may dispatch subagents internally; yaaos does not define or install them. |
| **Coding agent** | The external CLI the remote WorkspaceAgent runs (Claude Code). Protocol: `core/coding_agent.CodingAgentPlugin`. yaaos never calls an LLM directly, and never execs the CLI in-process. |
| **Workspace** | Provisioned execution environment owned by a remote WorkspaceAgent. Lifecycle owned by `core/workspace`; the only registered `WorkspaceProvider` is `remote_agent`. Dispatch via AgentCommand; org-level IAM ARN (`orgs.registered_iam_arn`) authorizes the agent pod. |
| **Finding** | One reviewer comment in the canonical schema: optional `file`/`line`, `category`, `severity` (`blocker / should_fix / nit`), `confidence` (`verified / plausible / speculative`), `rationale`, `rule_violated`, `rule_source`, `suggested_fix`, plus a persisted `finding_display_id` (per-PR monotonic int, rendered as `<category-prefix>-<id>` like `sec-3`). Defined in `domain/reviewer`. The skill (not yaaos) decides what to surface; yaaos validates the schema and posts the result. |
| **ReportedFinding** | Raw skill output before schema validation — same fields as `Finding` but raw strings (no enum validation). Lives in `core/coding_agent`. `domain/reviewer.publish_findings` validates and converts to `Finding`. |
| **Lesson** | Repo-scoped institutional memory: `{title, body, source_pr_url}`, 1000-char body cap. Surfaces in agent prompts. Owned by `domain/lessons`. |
| **Plugin** | Vendor-specific implementation of a Protocol in `core/`. Three Protocols: `VCSPlugin` (`core/vcs`, github), `CodingAgentPlugin` (`core/coding_agent`, claude_code), `WorkspaceProvider` (`core/workspace`). Vendor SDKs only in `apps/backend/app/plugins/`. |
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
| **VCS plugin** | Implements `core/vcs.VCSPlugin`. Ships `github` only. At most one per org; state on `orgs.vcs_plugin_id` + `orgs.vcs_settings`. |
| **Plugin install** | A specific `(org_id, plugin_id)` adoption. Mutations audit (`vcs.installed / vcs.cleared / coding_agent.installed / coding_agent.uninstalled`). |
| **Verified GitHub username** | `users.github_username`. Denorm written by the OAuth callback on every sign-in. Re-binding is "sign in with GitHub again." Never user-typed. |
| **Session-timeout override** | Nullable `orgs.session_timeout_override` (minutes). `require()` dep rejects sessions past `last_seen_at + override`. Null falls back to `SESSION_IDLE_TIMEOUT` (12h). |
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
| **WorkspaceAgent** | Customer-deployed Go binary in `apps/agent/` that holds source code locally and spawns workspace processes. Zero biz logic — all policy comes from control plane via AgentCommand payload. Each running instance is an agent instance tracked by a `workspace_agents` row keyed on `(org_id, instance_id)`. |
| **Workflow** | Typed Pydantic data structure: `name`, `version`, ordered `steps`, `entry_step_id`. `pr_review_v1` ships today. Owned by `core/workflow`. |
| **WorkflowCommand** | One unit of work within a workflow. Three categories: **Workspace** (parks in `awaiting_agent`), **Local** (inline), **HITL** (parks in `awaiting_human`). |
| **AgentCommand** | Wire message backend → WorkspaceAgent. Five kinds: `ProvisionWorkspace`, `WriteFiles`, `RefreshWorkspaceAuth`, `InvokeClaudeCode`, `CleanupWorkspace`. Defined in `apps/backend/openapi/agent-api.yaml`. Persisted in `agent_commands` (durable queue). |
| **AgentEvent** | Wire message WorkspaceAgent → backend. Kinds: `progress`, `received`, `completed_success`, `completed_failure`, `completed_skipped`. `received` cancels the lease requeue. Terminal events resume parked workflow. |
| **agent_commands** | Durable command queue in Postgres. One row per dispatched AgentCommand; lifecycle: `pending → claimed → delivered → done`. Commands survive backend restarts. `attempt` increments on each lease-timeout requeue; capped at `MAX_ATTEMPT`. |
| **Command lease** | 30-second window after a command is `claimed`: the agent must POST a `received` event to flip the row to `delivered`. Without it the reaper requeues to `pending` on the next `cleanup_loop` tick. |
| **Capacity-pull** | The agent declares `new_workspaces` (capacity for new ProvisionWorkspace commands) and `workspace_ids` (idle Active workspaces) on each claim request; the backend selects a matching batch from `agent_commands`. |
| **WorkflowExecution** | In-flight workflow run. State: `pending → running → (awaiting_agent \| awaiting_human)* → done \| failed \| cancelled`. Carries `step_state` JSONB + `otel_trace_context`. |
| **Pending human decision** | HITL pause row in `pending_human_decisions`. Resumed via `core/workflow.resume_hitl()`; resolution + re-enqueue in one transaction. |
| **Outbox** | DB-atomic outbound queue (`outbox_entries`). Written in the caller's transaction; drained post-commit. Backs `core/tasks.enqueue`. |
| **`core/tasks`** | Thin taskiq + Redis wrapper. Three task names: `workflow.start_step`, `workflow.handle_agent_event`, `workflow.route_workflow`. |
| **Activity event** | High-frequency CodingAgent telemetry. Flows WebSocket → `core/sse` → SSE → UI. Never persisted; demand-pull. |
| **InstanceID** | The role-session-name extracted from the STS assumed-role ARN (`arn:aws:sts::ACCT:assumed-role/ROLE/SESSION` → `SESSION`). Derived by the backend at identity exchange; stable across pod restarts when the ECS task reuses the same session name. Stored as `workspace_agents.instance_id`. The agent learns its own `instance_id` from the exchange response — it never supplies it. |
| **VerifiedInstanceID** | Synonym for `instance_id` when emphasizing that it was derived from a backend-verified STS ARN rather than self-reported by the agent. |
| **Stale-claim guard** | `POST /api/v1/commands/{id}/events` returns `410 Gone` when inbound `command_id` no longer matches `workspaces.current_command_id`. Agent abandons silently. |
| **Failure-report-precedes-disposal** | `core/workspace.release_claim` clears `current_command_id` before the workflow engine is resumed. Command-to-workflow correlation lives on `agent_commands.workflow_execution_id` — terminal events resolve their workflow via the command row, not the workspace row. |
| **Demand-pull** | Activity events only flow when ≥1 UI tab is subscribed. `SubscriberRegistry` issues `subscribe`/`unsubscribe` on `0 → 1` / `1 → 0` transitions. |
