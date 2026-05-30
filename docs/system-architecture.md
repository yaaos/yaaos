# System architecture

> How the apps fit together and the conventions spanning them. App-internal architecture lives in each app's own docs.

## Apps

- `apps/backend/` — FastAPI service; serves API + bundled SPA. Only app in production.
- `apps/web/` — React SPA (Vite); built into the backend image at build time.
- `apps/fake-github/` — peer service used only by the test stack.
- `apps/e2e/` — Playwright suite driving the test stack.
- `docker/` — production + test compose.

## Runtime topology

- One Docker image: FastAPI + built SPA + background work as in-process `asyncio` coroutines via `core/primitives.spawn()`. Periodic loops start in FastAPI's `lifespan`.
- Claude Code CLI baked into the image; spawned once per review inside the ticket's workspace. The parent reviewer dispatches `yaaos-*` subagents via the Task tool. Subagent definitions are markdown files installed into `~/.claude/agents/` at bootstrap. The CLI owns all LLM calls — yaaos makes zero direct LLM calls.
- Postgres holds all state. Single DB; each module owns its tables by convention.
- OTel collector optional; `core/observability` skips SDK setup if `OTEL_EXPORTER_OTLP_ENDPOINT` is unset.

## Inter-app flows

### PR open → review posted

1. GitHub sends HMAC-signed `pull_request.opened` to `POST /api/intake/github`.
2. `domain/intake.web` looks up the `github` IntakeType and calls `handle()`.
3. The intake type verifies HMAC, parses payload, and (for opened/reopened/ready_for_review) inserts a race-safe ticket + PR row and starts a `pr_review_v1` workflow via `core/workflow` — single transaction.
4. Workflow engine routes `CheckShouldReview → SecretsScan → ProvisionWorkspace → CodeReview → PostFindings → CleanupWorkspace`. Each step is a `WorkflowCommand` under `domain/reviewer/commands/`.
5. `CodeReview` dispatches via the configured provider (in-memory locally; remote-agent in prod). The parent Claude Code agent dispatches `yaaos-*` subagents in parallel, synthesizes findings, and returns one merged result. `PostFindings` runs admission then posts a single `vcs.Review` to GitHub. `CleanupWorkspace` always runs as the workflow's `final` step.

Every state transition writes to `audit_log`. SSE events publish for the SPA.

### UI live update via SSE

SPA mounts one `EventSource` on `GET /api/sse/general` (`withCredentials: true`) at app root. Each event invalidates TanStack Query caches:

| Event `kind` | Invalidates |
|---|---|
| `ticket_status_changed` | `["tickets"]`, `["tickets", id]`, `["tickets", id, "audit"]`, `["reviewer", "metrics"]` |
| anything else | silently ignored |

Events carry `ticket_id`, `previous_status`, `new_status`. Polling (5s / 3s) remains as a safety net.

### GitHub auth chain

Two yaaos-owned GitHub registrations, both env-only — credentials never in the DB:

- **GitHub App** (`YAAOS_GITHUB_APP_*`) — per-org installs + webhook receiver. Auth: App JWT (private key) → installation token.
- **GitHub OAuth App** (`YAAOS_GITHUB_OAUTH_*`) — "Sign in with GitHub". Auth: `client_id`/`client_secret` → user access token. No install concept, no webhooks.

Login (OAuth App): `GET /api/auth/login?provider=github` → signed `state` → 302 to GitHub → callback exchanges code → [identity orchestrator](../apps/backend/docs/core_identity.md) finds/creates user.

Install (GitHub App): `POST /api/github/install/start` signs `state={org_id}`, returns install URL. Callback verifies state, looks up account via App JWT, writes `github_app_installations` row.

Outbound API: `plugins/github` mints short-lived RS256 App JWT, exchanges for installation token (~1h, in-memory). Used as Bearer for REST and `GIT_ASKPASS` for `git clone`.

See per-module deep dives: [`plugins_github`](../apps/backend/docs/plugins_github.md), [`core_identity`](../apps/backend/docs/core_identity.md).

### MCP context for reviewer agents

Per-org, per-review. Coding-agent CLIs call hosted MCP servers (Linear, Notion) through a yaaos-owned proxy — authorization in one place, every JSON-RPC method writes an audit row.

Flow: `reviewer.queue` mints a per-review token (raw `secrets.token_urlsafe(32)`, sha256 → `mcp_review_tokens`), builds an MCP payload (`integrations.known_providers()` filtered to enabled + non-failed), injects `{token, base_url, servers}` into `agent_config["mcp"]`. The workspace writes `.mcp.json`; the CLI calls `mcp__linear__get_issue`; the proxy at `POST /api/mcp/{review_id}/linear` verifies the sha256 bearer, resolves `org_id`, enforces the write-tool allowlist, decrypts the org's access token, forwards to upstream, audits the call. Token revoked before workspace teardown.

**Attribution.** Manual UI review → `actor_kind=user`. Webhook review → `actor_kind=system`. Reviews always execute as the org service account, never as the triggering developer.

**Refresh serialization (deferred).** When implemented, `domain/integrations.refresh()` will use `pg_advisory_xact_lock(hashtext('mcp:' || org_id || ':' || provider))` to prevent double-spend on refresh tokens. Until then, token expiry surfaces `broken_creds` and the hourly health-check + email notifies the operator.

**Broken-creds surfacing (six layers).** Health-check flips `last_refresh_status`; audit row `mcp.<provider>.token_refresh_failed`; email to Owners (24h dedup); `GET /api/integrations/broken-summary` (cross-org; Owners + Admins only); red banner in the app shell; warning block in Coding Agents settings; review-output prefix when the agent hit `broken_creds`/`not_connected` mid-run.

**Audit shape.** One row per JSON-RPC method: `{kind: "mcp.<provider>.dispatched", payload: {provider, method, tool, args_hash, result_summary, upstream_account}}`. Never the full upstream response.

### WorkspaceAgent + workflow engine

Three concepts span all apps:

- **Workflow engine** (`core/workflow`) — typed `Workflow` definitions driven by three taskiq task bodies over `core/tasks` + `core/outbox`. Workspace commands park in `awaiting_agent` and resume on terminal AgentEvent. Five workflows: `pr_review_v1`, `incremental_review_v1`, `verify_fix_v1`, `stale_check_v1`, `answer_question_v1`. See [`core_workflow.md`](../apps/backend/docs/core_workflow.md).
- **Workspace provider abstraction** (`core/workspace`) — `InMemoryWorkspaceProvider` (in-process) and `RemoteAgentWorkspaceProvider` (dispatches via wire protocol to customer-deployed Go agent). Single-flight claim via `try_claim`/`release_claim`. See [`core_workspace.md`](../apps/backend/docs/core_workspace.md).
- **WorkspaceAgent** (`apps/agent/`) — customer-deployed Go binary; holds source code locally. Five HTTPS endpoints + one bidirectional WebSocket under `/api/v1/`. Full protocol contract: [`docs/workspace-agent-protocol.md`](../docs/workspace-agent-protocol.md).
  - `POST /api/v1/identity/exchange` — SigV4-signed STS → 24h bearer. Replays the customer's `GetCallerIdentity` against AWS STS; canonicalizes ARN; matches against `orgs.registered_iam_arn`; issues bearer via `bearer_tokens` ledger (sha256 stored, plaintext returned once).
  - `POST /api/v1/agents/{id}/heartbeat` — liveness + workspace inventory reconciliation.
  - `POST /api/v1/agents/{id}/commands/claim` — long-poll for next AgentCommand.
  - `POST /api/v1/workspaces/{id}/events` — workspace-state transitions.
  - `POST /api/v1/commands/{id}/events` — AgentCommand events; terminal events resume workflow.
  - `WSS /api/v1/agents/{id}/activity` — bidirectional activity stream; demand-pull (no traffic unless a UI tab is subscribed). See [`core_agent_gateway.md`](../apps/backend/docs/core_agent_gateway.md).

### End-to-end (remote-agent path)

1. GitHub webhook → `POST /api/intake/github_pr` verifies HMAC, dedups via `X-Github-Delivery`, creates ticket, starts `pr_review_v1`. Records `traceparent` so downstream tasks share the trace.
2. `route_workflow` picks up; `CheckShouldReview` (Local). `ProvisionWorkspace` (Workspace) parks workflow in `awaiting_agent`, dispatches via `core/agent_gateway.enqueue_command`.
3. Agent long-polls, runs operation, reports via `POST /api/v1/commands/{id}/events`. Backend's `record_agent_event` validates stale-claim guard, resolves `command_id → workspaces → current_holder_workflow_id`, enqueues `handle_agent_event`.
4. `handle_agent_event` clears claim, enqueues `route_workflow` → `CodeReview → PostFindings → CleanupWorkspace`.
5. Activity events from workspace flow over WebSocket only when a UI tab is subscribed.

### Test stack

`docker-compose.test.yml`: Postgres + `apps/fake-github` + backend with `GITHUB_API_BASE_URL=http://fake-github:8080` and `YAAOS_CODING_AGENT_STUB=1`. Plugins stubbed via `app/testing/`. E2E specs drive preconditions via `POST /api/testing/reset` + `seed/*`.

## Cross-app conventions

### Time

- UTC on the wire. Postgres `timestamptz`; Python `datetime.now(UTC)`; Pydantic emits `Z`-suffixed ISO 8601.
- Browser converts to local at render only via `formatTime` / `formatDateTime` in `apps/web/src/shared/utils/ago.ts`.

### Webhook authenticity

- Inbound webhooks MUST carry `X-Hub-Signature-256`. `plugins/github` verifies HMAC against `YAAOS_GITHUB_APP_WEBHOOK_SECRET` before dispatch.
- `apps/fake-github` signs outbound test webhooks with the same secret so the production verification path runs unchanged.
- Idempotency: `github_webhook_events` keyed by `X-GitHub-Delivery`; duplicates skipped with `INSERT ... ON CONFLICT DO NOTHING`.

### Audit log

One append-only `audit_log` table owned by `core/audit_log`. Row shape: `{id, org_id, created_at, entity_kind, entity_id, kind ("<entity>.<verb_past>"), payload (Pydantic-validated JSONB), actor}`. Reads never write. Progress steps go to structlog, not audit. Each domain module writes its own entries.

### Org scoping

Every domain function takes `org_id` kwarg; every query filters by it. Per-request org from `X-Org-Slug` header (HTTP) or `org_context()` async-context-manager (background jobs).

### Identity & access

- Auth middleware (`core/auth`): every `/api/*` route declares security via `Depends(require(action))` or `Depends(public_route)`; a post-response guard 500s any 2xx that left `route_security_resolved` unset.
- Sessions: opaque server-side rows (sha256-hashed tokens), `HttpOnly; SameSite=Lax; Secure` cookies, double-submit CSRF on mutations. SSO satisfaction tracked per-session per-org with 8h TTL.
- Background jobs open `org_context(org_id, actor_kind, actor_id)` to set the same contextvars + OTel + structlog fields the HTTP middleware sets.
- Session rotation on role change, invite accept, SSO satisfaction. `sessions.revoke_all_for_user` on member removal + logout-all.

Per-module deep dives: [`core_auth`](../apps/backend/docs/core_auth.md), [`core_identity`](../apps/backend/docs/core_identity.md), [`domain_orgs`](../apps/backend/docs/domain_orgs.md), [`core_saml`](../apps/backend/docs/core_saml.md).

### Secrets at rest

All at-rest secrets go through [`core/secrets`](../apps/backend/docs/core_secrets.md) — Fernet wrapper resolving the master key from `YAAOS_TOTP_MASTER_KEY` (fallback `YAAOS_ENCRYPTION_KEY` in non-prod). Plaintext crosses the boundary only at write and at the specific call site that needs the decrypted value; never logged, never echoed in errors, never in audit payloads.

### Persistence (new tables)

`workflow_executions`, `pending_human_decisions`, `outbox_entries`, `workspace_agents`, `bearer_tokens`. Existing tables extended: `tickets` (type, idempotency_key, payload, current_workflow_execution_id), `workspaces` (provider, current_command_id, current_holder_workflow_id, max_idle_seconds), `orgs` (workspace_provider, registered_iam_arn, aws_region). Activity events are never persisted — they exist only in flight from WebSocket → `core/sse` → SSE → UI.

### Dumb frontend

SPA renders data and dispatches actions. It does not compute verdicts, derive status, hold permissions, or own any rule the backend doesn't also enforce. See [`apps/web/docs/patterns.md`](../apps/web/docs/patterns.md).

## Stack at a glance

| Concern | Choice |
|---|---|
| Backend | Python 3.14, FastAPI |
| Frontend | Node 22, React + TanStack Router + TanStack Query + Tailwind |
| Data store | Postgres 18 |
| ORM / migrations | SQLAlchemy 2.0 async + Alembic (hand-edited) |
| Background work | `asyncio.create_task` via `core/primitives.spawn` |
| API | REST + SSE |
| Tests | pytest, Vitest, Playwright |
| Telemetry | OpenTelemetry SDK → collector → sink |

Boot-time env vars: see [`apps/backend/docs/core_config.md`](../apps/backend/docs/core_config.md).
