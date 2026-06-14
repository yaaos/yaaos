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
- Claude Code CLI runs inside the WorkspaceAgent container (customer-deployed); spawned once per review by the agent. The CLI owns all LLM calls — the backend makes zero direct LLM calls and never execs the CLI in-process.
- Postgres holds all state. Single DB; each module owns its tables by convention.
- OTel collector optional; backend `core/observability` attaches OTLP exporters only when `YAAOS_DASH0_ENDPOINT`, `YAAOS_DASH0_DATASET`, and `YAAOS_BACKEND_DASH0_BEARER_TOKEN` are all set. The web SPA also runs an OTel SDK (`core/observability`); export is triple-gated on `VITE_OTEL_COLLECTOR_ENDPOINT` + `VITE_DASH0_AUTH_TOKEN` + `VITE_DASH0_DATASET`.

## Inter-app flows

### PR open → review posted

1. GitHub sends HMAC-signed `pull_request.opened` to `POST /api/intake/github`.
2. `domain/intake.web` looks up the `github` IntakeType and calls `handle()`.
3. The intake type verifies HMAC, parses payload, and (for opened/reopened/ready_for_review) inserts a race-safe ticket + PR row and starts a `pr_review_v1` workflow via `core/workflow` — single transaction.
4. Workflow engine routes `CheckShouldReview → SecretsScan → ProvisionWorkspace → CodeReview → PostFindings → CleanupWorkspace`. Each step is a `WorkflowCommand` under `domain/reviewer/commands/`.
5. `ProvisionWorkspace`, `CodeReview`, and `CleanupWorkspace` are Workspace-category commands — each parks the workflow in `awaiting_agent` and dispatches an AgentCommand over the wire to the remote WorkspaceAgent; the terminal AgentEvent resumes routing. `PostFindings` persists each `Finding` then posts each one to GitHub via `vcs.post_finding` (named primitive args — no finding value object crosses the `vcs` boundary). The `CodeReview` step reads the per-repo **skill name** from `claude_code_repos.skill_name` via `plugins/claude_code.resolve_skill` — if absent, the step fails before dispatching an AgentCommand.

Every state transition writes to `audit_log`. SSE events publish for the SPA.

### UI live update via SSE

SPA mounts one org-keyed `EventSource` on `GET /api/sse/general?org=<slug>` (`withCredentials: true`) from the root `AppShell`. The `?org=` query param carries the org because the browser `EventSource` API cannot set the `X-Yaaos-Org-Slug` header that `/api/sse` routes otherwise require; the backend accepts it for SSE routes and applies the same membership check. Each event invalidates TanStack Query caches:

| Event `kind` | Invalidates |
|---|---|
| `ticket_status_changed` | `["tickets"]`, `["tickets", id]`, `["tickets", id, "audit"]`, `["reviewer", "metrics"]` |
| anything else | silently ignored |

Events carry `ticket_id`, `previous_status`, `new_status`. There is no polling fallback (`refetchOnWindowFocus` is off, no `refetchInterval` on SSE-driven queries); SSE is the only live-update path for the dashboard. The client reconciles list-level caches on every (re)connect (`onopen`) and the server emits a connect prelude so that fires promptly.

**Agent liveness.** The `core/workspace` reaper loop calls `compute_agent_liveness_transitions` each tick. That function reads `workspace_agents.last_heartbeat_at` and writes `state` (reachable / stale / offline) only on transition. Each transition emits one `agent_liveness_changed` event on the org's general channel. The SPA's subscriber maps `agent_liveness_changed` → invalidate `["agents"]`; the dashboard `AgentCard` row refreshes live without polling. The `GET /api/orgs/{slug}/agents` endpoint returns agents within a 1-hour UI-retention window.

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

Flow: `reviewer.queue` mints a per-review token (raw `secrets.token_urlsafe(32)`, sha256 → `mcp_review_tokens`), builds an MCP payload (`integrations.known_providers()` filtered to enabled + non-failed), threads `{token, base_url, servers}` into the coding-agent invocation so the CLI has MCP server config. The workspace writes `.mcp.json`; the CLI calls `mcp__linear__get_issue`; the proxy at `POST /api/mcp/{review_id}/linear` verifies the sha256 bearer, resolves `org_id`, enforces the write-tool allowlist, decrypts the org's access token, forwards to upstream, audits the call. Token revoked before workspace teardown.

**Attribution.** Manual UI review → `actor_kind=user`. Webhook review → `actor_kind=system`. Reviews always execute as the org service account, never as the triggering developer.

**Refresh serialization (deferred).** When implemented, `domain/integrations.refresh()` will use `pg_advisory_xact_lock(hashtext('mcp:' || org_id || ':' || provider))` to prevent double-spend on refresh tokens. Until then, token expiry surfaces `broken_creds` and the hourly health-check + email notifies the operator.

**Broken-creds surfacing (six layers).** Health-check flips `last_refresh_status`; audit row `mcp.<provider>.token_refresh_failed`; email to Owners (24h dedup); `GET /api/integrations/broken-summary` (cross-org; Owners + Admins only); red banner in the app shell; warning block in Coding Agents settings; review-output prefix when the agent hit `broken_creds`/`not_connected` mid-run.

**Audit shape.** One row per JSON-RPC method: `{kind: "mcp.<provider>.dispatched", payload: {provider, method, tool, args_hash, result_summary, upstream_account}}`. Never the full upstream response.

### WorkspaceAgent + workflow engine

Three concepts span all apps:

- **Workflow engine** (`core/workflow`) — typed `Workflow` definitions driven by three taskiq task bodies over `core/tasks` + `core/outbox`. Workspace commands park in `awaiting_agent` and resume on terminal AgentEvent. One workflow today: `pr_review_v1` (the PR review path, [`domain/reviewer`](../apps/backend/docs/domain_reviewer.md)). See [`core_workflow.md`](../apps/backend/docs/core_workflow.md).
- **Workspace provider abstraction** (`core/workspace`) — `RemoteAgentWorkspaceProvider` dispatches via wire protocol to the customer-deployed Go agent. Single-flight claim via `try_claim`/`release_claim`. See [`core_workspace.md`](../apps/backend/docs/core_workspace.md).
- **WorkspaceAgent** (`apps/agent/`) — customer-deployed Go binary; holds source code locally. Five HTTPS endpoints + one bidirectional WebSocket under `/api/v1/`. Agent identity on operational channels is bearer-derived — no `{agent_id}` path segment. Full protocol contract: [`docs/workspace-agent-protocol.md`](../docs/workspace-agent-protocol.md).
  - `POST /api/v1/agent/identity` — SigV4-signed STS → 1-hour bearer. Replays the customer's `GetCallerIdentity` against AWS STS (or mock-aws in dev/test); audience-checks `X-Yaaos-Audience`; canonicalizes ARN; derives `instance_id` from role-session-name; matches against `orgs.registered_iam_arn`; issues bearer via `bearer_tokens` ledger (sha256 stored, plaintext returned once). Region mismatch (verified ARN matched org, wrong region) writes an `identity_exchange_failed` audit row on the org.
  - `DELETE /api/v1/agent/identity` — graceful-shutdown "going away" signal. Sets agent offline, revokes bearer, expires held workspaces, synthesizes terminal failures for in-flight commands. Dashboard flips offline without waiting for the sweeper.
  - `POST /api/v1/agent/heartbeat` — liveness + workspace inventory reconciliation. Persists `claimed_workspace_count = len(workspaces)`.
  - `POST /api/v1/agent/commands/claim` — long-poll for next AgentCommand.
  - `POST /api/v1/workspaces/{id}/events` — workspace-state transitions.
  - `POST /api/v1/commands/{id}/events` — AgentCommand events; terminal events resume workflow.
  - `WSS /api/v1/agent/activity` — bidirectional activity stream; demand-pull (no traffic unless a UI tab is subscribed). See [`core_agent_gateway.md`](../apps/backend/docs/core_agent_gateway.md).

### End-to-end (remote-agent path)

1. GitHub webhook → `POST /api/intake/github_pr` verifies HMAC, dedups via `X-Github-Delivery`, creates ticket, starts `pr_review_v1`. Records `traceparent` so downstream tasks share the trace.
2. `route_workflow` picks up; `CheckShouldReview` (Local). `ProvisionWorkspace` (Workspace) parks workflow in `awaiting_agent`, dispatches via `core/agent_gateway.enqueue_command`.
3. Agent long-polls, runs operation, reports via `POST /api/v1/commands/{id}/events`. Backend's `record_agent_event` validates stale-claim guard, resolves `command_id → agent_commands.workflow_execution_id`, enqueues `handle_agent_event`.
4. `handle_agent_event` clears claim, enqueues `route_workflow` → `CodeReview → PostFindings → CleanupWorkspace`.
5. Every `InvokeClaudeCode` dispatch creates a `coding_agent_runs` row (`status=running`, started_at). The registered `CodingAgentRunSink` (in `core/coding_agent`) fires on the matching terminal `AgentEvent`, calls the `claude_code` plugin's `parse_usage` + `render_activity` against the captured stdout, writes `status`/`exit_code`/`tokens_in`/`tokens_out`/`duration_ms` onto the run row, and persists the rendered `ActivityLog` JSONB to the partitioned `coding_agent_activity` table (weekly partitions, ~4-week TTL). `reviews.run_id` links each review to its run. See [`apps/backend/docs/core_coding_agent.md`](../apps/backend/docs/core_coding_agent.md).
6. Activity events from workspace flow over WebSocket only when a UI tab is subscribed.

### Test stack

`docker-compose.test.yml`: Postgres + `apps/fake-github` + backend with `GITHUB_API_BASE_URL=http://fake-github:8080` and `YAAOS_CODING_AGENT_STUB=1`. Plugins stubbed via `app/testing/`. E2E specs drive preconditions via `POST /api/testing/reset` + `seed/*`.

### Periodic work

Cluster-safe recurring tasks run in every worker process via `core/tasks.scheduler_loop`. Schedules are declared at import time with `@scheduled(name, cron)` or `schedule_task(name, cron, task_ref=...)`. Per-tick `INSERT INTO scheduled_runs (schedule_id, fire_time) ... ON CONFLICT DO NOTHING` is the sole gate that decides which worker enqueues for a slot — no leader election, no SPOF. Registered schedules:

- `scheduled_runs_prune` (daily, `0 0 * * *`, `core/tasks`) — deletes `scheduled_runs` rows >7 days old.
- `identity_purge` (hourly, `0 * * * *`, `core/identity`) — purges expired sessions, unverified TOTP secrets older than 24h, and audit entries older than `AUDIT_LOG_RETENTION`.
- `workspace_reaper` (per minute, `* * * * *`, `core/workspace`) — TTL expiry, idle-timeout, agent-loss detection, destroy retries.
- `coding_agent_activity_partition_maintenance` (daily, `0 1 * * *`, `core/coding_agent`) — creates the current ISO week + the next two partitions of `coding_agent_activity`, drops partitions >4 weeks old; raw partition DDL is in `core/database`.

See [`apps/backend/docs/core_tasks.md`](../apps/backend/docs/core_tasks.md).

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

**Identity-exchange failures.** Only org-attributable failures reach audit (`entity_kind="org"`): a region mismatch where the canonical ARN matched a registered org writes `identity_exchange_failed` with `{category, attempted_arn, source_ip}`. Non-attributable failures (unregistered ARN, parse/replay/AWS errors) stay structlog-only — `audit_entries.org_id` is mandatory.

### Org scoping

Every domain function takes `org_id` kwarg; every query filters by it. Per-request org from `X-Yaaos-Org-Slug` header (HTTP) or `org_context()` async-context-manager (background jobs).

### Identity & access

- **CSP header injection** (`core/webserver.CSPMiddleware`, outermost): emits `Content-Security-Policy` or `…-Report-Only` on every response — including Cloudflare's 403s. Mode via `YAAOS_CSP_MODE`; policy directives live in `core/webserver/csp.py`.
- **Cloudflare ingress gate** (`core/auth.CloudflareIngressMiddleware`, outermost security gate): rejects any request not carrying the `X-Yaaos-cf-Ingress` header with HTTP 403. `/api/health` is exempt (Fly's internal checker bypasses Cloudflare). No-op when `YAAOS_CLOUDFLARE_INGRESS_SECRET` is empty (dev/test/e2e). Runs before auth and routes — direct `.fly.dev` hits are blocked before they reach any business logic.
- Auth middleware (`core/auth`): every `/api/*` route declares security via `Depends(require(action))` or `Depends(public_route)`; a post-response guard 500s any 2xx that left `route_security_resolved` unset.
- Sessions: opaque server-side rows (sha256-hashed tokens), `HttpOnly; SameSite=Lax; Secure` cookies, double-submit CSRF on mutations. SSO satisfaction tracked per-session per-org with 8h TTL.
- Background jobs open `org_context(org_id, actor_kind, actor_id)` to set the same contextvars + OTel + structlog fields the HTTP middleware sets.
- Session rotation on role change, invite accept, SSO satisfaction. `sessions.revoke_all_for_user` on member removal + logout-all.

Per-module deep dives: [`core_auth`](../apps/backend/docs/core_auth.md), [`core_identity`](../apps/backend/docs/core_identity.md), [`domain_orgs`](../apps/backend/docs/domain_orgs.md), [`core_saml`](../apps/backend/docs/core_saml.md).

### Secrets at rest

All at-rest secrets go through [`core/secrets`](../apps/backend/docs/core_secrets.md) — Fernet wrapper resolving the master key from `YAAOS_TOTP_MASTER_KEY` (fallback `YAAOS_ENCRYPTION_KEY` in non-prod). Plaintext crosses the boundary only at write and at the specific call site that needs the decrypted value; never logged, never echoed in errors, never in audit payloads.

### Persistence (new tables)

`workflow_executions`, `pending_human_decisions`, `outbox_entries`, `workspace_agents`, `bearer_tokens`. Existing tables extended: `tickets` (type, idempotency_key, payload, current_workflow_execution_id), `workspaces` (current_command_id, max_idle_seconds, owning_agent_id), `orgs` (registered_iam_arn, aws_region). Activity events are never persisted — they exist only in flight from WebSocket → `core/sse` → SSE → UI.

### Committed OpenAPI artifacts

Two static specs are committed under `apps/backend/openapi/`:

- `agent-api.yaml` — the wire contract between the backend and the Go WorkspaceAgent. Hand-maintained; drift-gated by [`app/core/agent_gateway/test/test_openapi_mirror_drift.py`](../apps/backend/app/core/agent_gateway/test/test_openapi_mirror_drift.py).
- `web-api.json` — the full REST surface consumed by the web SPA. Generated by `apps/backend/bin/dump_web_openapi`; drift-gated by [`app/core/webserver/test/test_web_openapi_drift.py`](../apps/backend/app/core/webserver/test/test_web_openapi_drift.py). `/api/testing/*` paths are stripped before writing — test-only backdoors do not appear in the artifact. Backend CI fails if either artifact is stale.

### Distributed tracing

- Web SPA runs `@opentelemetry/sdk-trace-web`. `FetchInstrumentation` injects a W3C `traceparent` header on same-origin `/api/*` fetches — browser spans become children of the backend trace automatically. Cross-origin fetches (collector, CDN, third-party) never receive `traceparent`.
- `FastAPIInstrumentor` on the backend extracts `traceparent` and continues the same trace. The backend stamps `yaaos.org_id`/`yaaos.user_id` on its spans authoritatively from session context.
- **No baggage crosses the wire.** Identity is stamped independently on each side. `traceparent` is the only cross-wire trace context.
- Backend export requires `YAAOS_DASH0_ENDPOINT` + `YAAOS_DASH0_DATASET` + `YAAOS_BACKEND_DASH0_BEARER_TOKEN` (three-way AND). Web export is triple-gated: `VITE_OTEL_COLLECTOR_ENDPOINT` + `VITE_DASH0_AUTH_TOKEN` + `VITE_DASH0_DATASET` must all be set; any missing field falls back to a no-op span processor. All three are `VITE_*` vars (embedded in the bundle at build time); the auth token must be a web-signal-restricted, ingest-only Dash0 token.

### Dumb frontend

SPA renders data and dispatches actions. It does not compute verdicts, derive status, hold permissions, or own any rule the backend doesn't also enforce. See [`apps/web/docs/patterns.md`](../apps/web/docs/patterns.md).

## Stack at a glance

| Concern | Choice |
|---|---|
| Backend | Python 3.14, FastAPI |
| Frontend | Node 24, React + TanStack Router + TanStack Query + Tailwind |
| Data store | Postgres 18 |
| ORM / migrations | SQLAlchemy 2.0 async + Alembic |
| Background work | `asyncio.create_task` via `core/primitives.spawn` |
| API | REST + SSE |
| Tests | pytest, Vitest, Playwright |
| Telemetry | OpenTelemetry SDK → collector → sink |

Boot-time env vars: see [`apps/backend/docs/core_config.md`](../apps/backend/docs/core_config.md).
