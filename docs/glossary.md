# Glossary

Shared vocabulary across backend and frontend. These terms appear in code, URLs, and UI; this list keeps usage consistent.

| Term | Meaning |
|---|---|
| **Org** | Tenant boundary. UUID PK + immutable unique `slug` used in `/orgs/{slug}/...` URLs. Multi-org from M02 onward; users may belong to many. Every non-user data row is `org_id`-scoped. Soft-deleted via `archived_at`. |
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
| **Actor** | Who initiated an action — `{kind: "github_user" | "agent" | "system" | "user" | "workspace" | "sso", login?, agent_id?, user_id?, workspace_id?}`. Required on every audit entry. Defined in `core/audit_log` (relocated from `core/primitives` in M04 Phase 6a). |
| **Onboarding** | Dashboard's pre-ready state. Two checks: GitHub App installed + Anthropic API key set (validated by live probe). Computed by `domain/orgs.get_onboarding_status()`. |
| **User** | A human identity. UUID PK, soft-deletable via `deactivated_at`. Distinct from `actor_kind=github_user` which is the GitHub-as-VCS actor on legacy rows. Owned by `domain/identity`. |
| **Membership** | Link between a `User` and an `Org`. Composite PK `(user_id, org_id)`. Carries a per-org `handle` and one of three `Role`s. Same user can be `@jack` in one org and `@jkora` in another. |
| **Role** | `owner ≥ admin ≥ member`. Compared via `role.covers(required)` only. Action-to-role minimums declared at `Depends(require(Action.X))` call sites. |
| **Session** | Opaque server-side row keyed by `sha256(raw_token)`. Cookie is `HttpOnly; SameSite=Lax; Secure`. Double-submit CSRF via the per-session `csrf_token`. `sso_satisfied_for_org_id` + timestamp track the 8-hour SSO TTL. |
| **Invitation** | Owner/Admin-issued offer for an external email to join an org. Token is `itsdangerous`-signed, 7-day TTL, single-use. Stored as `sha256(token)`; raw tokens live only in the invitation email. |
| **Provider** | OAuth identity provider. Plugins (`oauth_github`, `oauth_test`) implement the `Provider` Protocol. Returns a `ProviderProfile` (`external_subject`, `primary_email`, `email_verified`, `display_name`, `mfa_satisfied`). |
| **SSO** | SAML 2.0 per-org single sign-on. SP-initiated. Verified assertion → session `sso_satisfied_for_org_id`. Middleware blocks org access when SSO is enabled and the session isn't satisfied. |
| **Break-glass Owner** | Owner picked as the SSO `exempt_owner`. Can sign in via OAuth + TOTP when SSO is broken. Each bypass writes a `break_glass_exempt_owner` audit entry. The exempt-Owner candidate must already have a verified TOTP secret. |
| **VCS plugin** | Plugin implementing `domain/vcs.VCSPlugin`. M03 ships `github` only. At most one per org; install state stored on `orgs.vcs_plugin_id` + `orgs.vcs_settings`. Choice changed via Org Settings > VCS. |
| **Plugin install** | A specific `(org_id, plugin_id)` adoption: a chosen-VCS row or an `org_coding_agents` row. Mutations audit (`vcs.installed` / `vcs.cleared` / `coding_agent.installed` / `coding_agent.uninstalled`). |
| **Verified GitHub username** | `users.github_username`. Written by the GitHub OAuth login callback on every successful sign-in OR by the verify-only flow at `/api/account/github/verify`. Never user-typed. |
| **Session-timeout override** | Nullable `orgs.session_timeout_override` (minutes). When set, the `require()` dep rejects sessions whose `last_seen_at + override` is in the past. Null falls back to `SESSION_IDLE_TIMEOUT` (12h global default). |
| **Orchestrator** | The single parent Claude Code session that dispatches sub-agents via the Task tool and synthesizes their findings. One per `claude_code` install; configured under Org Settings > Coding Agents > Claude Code. |
| **Sub-agent** | A focused review pass run as its own Claude Code session, dispatched by the orchestrator. 1..8 per `claude_code` install. Names must be unique within an install (enforced by Pydantic at the API boundary). |
| **BYOK** | Bring-your-own-key per `(org_id, provider)`. M03 ships `anthropic` only. Plaintext lives only inbound on POST + outbound from `core/byok.get/.validate`; the DB stores Fernet ciphertext via `core/secrets`. |
| **MCP** | Model Context Protocol — the JSON-RPC-over-HTTP shape that hosted integrations (Linear, Notion) speak to coding-agent CLIs. yaaos proxies every MCP request from a review through `domain/mcp_proxy` so authorization + audit happen in one place. |
| **MCP review token** | Per-review opaque bearer that authenticates the coding-agent CLI to the yaaos proxy. Minted at review start (sha256 hex persisted in `mcp_review_tokens`, raw returned once), revoked before workspace teardown, swept hourly. 2h absolute TTL. |
| **Integration** | The yaaos record of a connected hosted provider for an org: encrypted OAuth tokens, per-tool allowlist, status. Owned by `domain/integrations`; one row per `(org_id, provider)`. |
| **Hosted MCP** | An MCP server that lives at a provider (mcp.linear.app, mcp.notion.com) rather than running locally. yaaos's proxy forwards to whichever URL the provider's `ProviderConfig.mcp_url` declares. |
| **Org service account** | The single upstream OAuth identity an org has connected per provider. Audit rows always tag MCP dispatch with `upstream_account="org_service_account"` — reviews never run as the developer who triggered them. |
| **Allowlist** | Per-`(org_id, provider)` list of write tools the proxy will forward. Read tools are always allowed; write tools are opt-in (empty = read-only). |
| **Broken-creds** | Status state for an integration whose `last_refresh_status == "failed"`. Surfaces in six places (banner, email, audit, settings badge, Claude Code warning, review-output prefix). |
| **Upstream identity** | Display string (email / handle / org name) the OAuth flow returned for the connected account. Stored on `mcp_credentials.upstream_identity` for UI; never used as an auth principal. |

## M05 — workspace agent + workflow engine

| Term | Meaning |
|---|---|
| **Intake** | The inbound surface that turns an external signal into a ticket. `domain/intake` ships a typed registry keyed by intake-type name; `POST /api/intake/{type}` dispatches via the registry. `github_pr` is the only type today; future types add a row to the registry without touching the endpoint. |
| **WorkspaceAgent** | The customer-deployed Go binary in `apps/agent/` that holds source code locally + spawns workspace processes. Always the same binary regardless of CodingAgent — zero biz logic. |
| **Workflow** | Typed Pydantic data structure registered at startup. `name`, `version`, ordered `steps`, `entry_step_id`. Five ship in M05 (`pr_review_v1`, `incremental_review_v1`, `verify_fix_v1`, `stale_check_v1`, `answer_question_v1`). Owned by `core/workflow`. |
| **WorkflowCommand** | One unit of work within a workflow. Three categories: **Workspace** (issues AgentCommands; workflow parks in `awaiting_agent`), **Local** (runs inline in the worker), **HITL** (writes a pending-decision row, parks in `awaiting_human`). 13 ship in M05 across `domain/reviewer/commands/` + `core/workspace/commands.py`. Engine in `core/workflow`. |
| **AgentCommand** | Wire-protocol message from backend to a WorkspaceAgent telling it what to do. Five kinds: `CreateWorkspace`, `WriteFiles`, `RefreshWorkspaceAuth`, `InvokeClaudeCode`, `CleanupWorkspace`. Carries `command_id`, `workspace_id`, `traceparent`, kind-specific payload. Defined in `apps/backend/openapi/agent-api.yaml`. |
| **AgentEvent** | Wire-protocol message from WorkspaceAgent back to backend reporting AgentCommand progress or terminal outcome. Kinds: `progress`, `completed_success`, `completed_failure`, `completed_skipped`. Terminal events resume the parked workflow via `core/workflow.handle_agent_event`. |
| **Workspace (M05)** | Persisted execution environment in `workspaces` table. `provider` discriminates `in_memory` vs `remote_agent`. Single-flight gated by `current_command_id` claim; `current_holder_workflow_id` links the holding workflow execution. M05 extends the M01 lifecycle without rewriting it. |
| **Workspace Agent (DB row)** | Per-pod identity in `workspace_agents`. Keyed by `(org_id, agent_pod_id)`. Refreshed by every successful identity-exchange + heartbeat. Drives the dispatch picker and the connection-status banner. |
| **WorkflowExecution** | One in-flight workflow run. Row in `workflow_executions`. State machine: `pending → running → (awaiting_agent | awaiting_human)* → done | failed | cancelled`. Carries `step_state` JSONB (per-step outcome + outputs for input resolution) + `otel_trace_context` (W3C traceparent). |
| **Pending human decision** | HITL pause. Row in `pending_human_decisions` carrying the question payload. Resumed via `core/workflow.resume_hitl()`; resolution writes back `resolution_payload + resolved_at` in the same transaction that re-enqueues the next routing task. |
| **Outbox** | DB-atomic outbound message queue (`outbox_entries`). `core/outbox.write(session, kind, payload)` writes a row in the caller's transaction; the drain delivers post-commit. Backs `core/tasks.enqueue` (atomic-in-session task enqueue) and future kinds like `pubsub_publish`. |
| **`core/tasks`** | Thin abstraction wrapping taskiq + Redis. `@task(name)` registers a body; `enqueue(task_ref, args, *, session)` writes a `taskiq_enqueue` outbox row. Three M05 task names: `workflow.start_step`, `workflow.handle_agent_event`, `workflow.route_workflow`. |
| **Activity event** | High-frequency CodingAgent telemetry event flowing through the `WSS /api/v1/agents/{id}/activity` WebSocket → `core/sse_pubsub` → per-workflow SSE → UI. Never persisted; demand-pull (no events flow unless a UI tab is subscribed). |
| **Stale-claim guard** | Backend returns `410 Gone` from `POST /api/v1/commands/{id}/events` when the `command_id` no longer matches the workspace's `current_command_id`. Agent abandons silently. |
| **Failure-report-precedes-disposal** | Invariant: `core/workspace.release_claim` clears `current_command_id` but preserves `current_holder_workflow_id`. The workflow can always resolve its workspace, even after the claim has been released. |
| **Demand-pull** | Activity events only flow when at least one UI tab is subscribed to the workflow. `SubscriberRegistry` in `core/agent_gateway` issues `subscribe`/`unsubscribe` to the WorkspaceAgent on `0 → 1` / `1 → 0` transitions. No webhook-triggered review generates wire traffic for activity unless someone's watching. |
