# System architecture

How the apps fit together and the conventions spanning them. App-internal architecture lives in each app's own docs.

## Apps

- `apps/backend/` — FastAPI service; serves API + bundled SPA. Only app in production.
- `apps/web/` — React SPA (Vite); built into the backend image.
- `apps/fake-github/` — peer service used only by the test stack.
- `apps/e2e/` — Playwright suite driving the test stack.
- `docker/` — production + test compose.
- `plan/` — future-tense planning.

`web` is bundled into the backend image at build. `fake-github` and `e2e` only run from `docker/docker-compose.test.yml`.

## Runtime topology

- One Docker image runs FastAPI, serves the built SPA, and runs background work as in-process `asyncio` coroutines via `core/primitives.spawn()`. Periodic loops (workspace reaper, GitHub catch-up poller) start in FastAPI's `lifespan`.
- Claude Code CLI baked into the image; spawned once per review run inside the ticket's workspace. The parent reviewer dispatches `yaaos-*` subagents (architecture, security, line-level, tests, docs, conditional skill) via the Task tool and synthesizes their findings. Subagent definitions are static markdown files installed into `~/.claude/agents/` at backend bootstrap. The CLI owns all LLM communication — yaaos makes zero direct LLM calls.
- Postgres holds all state. Single DB; each module owns its tables by convention.
- OpenTelemetry collector recommended but not required; `core/observability` skips SDK setup if `OTEL_EXPORTER_OTLP_ENDPOINT` is unset.

## Inter-app flows

### PR open → review posted

1. GitHub (or `fake-github` in tests) sends HMAC-signed `pull_request.opened` to `POST /api/github/webhook`.
2. `plugins/github` verifies HMAC, parses into a `VCSEvent`, hands to `domain/intake`.
3. `domain/intake` upserts PR (`domain/pull_requests`) + ticket (`domain/tickets`), calls `reviewer.start_pr_review` which starts a `pr_review_v1` workflow execution via `core/workflow`.
4. The workflow engine routes `CheckShouldReview → SecretsScan → ProvisionWorkspace → CodeReview → PostFindings → CleanupWorkspace`. Each step is a `WorkflowCommand` body under `domain/reviewer/commands/`.
5. `CodeReview` provisions a workspace via the configured provider (in-memory locally; remote-agent in prod) and invokes `coding_agent.review`. The parent Claude Code agent dispatches `yaaos-*` subagents in parallel via the Task tool, synthesizes findings (re-reads cited code, dedupes, ranks), and returns one merged result. `PostFindings` runs admission, then posts a single `vcs.Review` to GitHub with each finding tagged by its `source_agent`. `CleanupWorkspace` always runs as the workflow's `final` step.

Every state transition writes to `audit_log`. SSE events publish for the SPA.

### UI live update via SSE

SPA mounts one `EventSource` on `GET /api/events` at app root. Each event invalidates TanStack Query caches:

| Event `kind` | Invalidates |
|---|---|
| `ticket_status_changed` | `["tickets"]`, `["tickets", id]`, `["tickets", id, "audit"]`, `["reviewer", "metrics"]` |
| `review_job_status_changed` | `["reviewer", "jobs", id]`, `["tickets", id, "audit"]`, `["reviewer", "metrics"]`, `["tickets"]` |
| `review_job_step_progress` | `["reviewer", "jobs", id]` only — in-place row update |

Events carry `pr_id` + `review_job_id` (no `agent_id` — one job per review run).

Polling (5s / 3s) remains as a safety net.

### GitHub App auth chain

One yaaos-owned GitHub App registration. Credentials in env vars (`YAAOS_GITHUB_APP_*`), never in the DB. The same registration drives both **"Sign in with GitHub"** (user-to-server OAuth) and **per-org installs** (App installation tokens).

1. Operator registers the platform App once at github.com → `Settings > Developer settings > GitHub Apps`, drops App ID / slug / PEM / client_id / client_secret / webhook secret into `.env`. See [docs/setup.md](setup.md).
2. **Login:** SPA hits `/api/auth/login?provider=github` → backend signs `state` and 302s to `${github_web_base_url}/login/oauth/authorize?client_id=...`. GitHub redirects back with `code`; backend exchanges via `POST /login/oauth/access_token` and reads `/user` + `/user/emails`. The [`identity` orchestrator](../apps/backend/docs/domain_identity.md#login-orchestrator) finds or creates the user.
3. **Install:** Owner hits `Org Settings > VCS > Install yaaos on GitHub`. SPA POSTs `/api/github/install/start` (which signs `state={org_id}` and returns `${github_web_base_url}/apps/${slug}/installations/new?state=...`). Browser follows; user picks repos; GitHub redirects to `/api/github/install_callback`. Backend verifies state, looks up the install's `account.login` via App JWT, and writes a `github_app_installations` row.
4. **Outbound API:** `plugins/github` signs a short-lived RS256 App JWT with the platform PEM, exchanges it at `POST /app/installations/{id}/access_tokens` for an installation token (~1h TTL, in-memory). Token used as Bearer for REST and `GIT_ASKPASS`-style for `git clone`.

### MCP context for reviewer agents (M04)

Per-org, per-review pipeline. Coding-agent CLIs call hosted MCP servers (Linear, Notion) through a yaaos-owned proxy so authorization happens in one place + every JSON-RPC method writes an audit row.

```
reviewer.queue                                 plugins/claude_code               proxy                upstream MCP
─────────────                                 ──────────────────              ──────                ──────────
mint_token(review_id)
  └─ secrets.token_urlsafe(32) → sha256 → mcp_review_tokens
_build_mcp_payload(review_id, org_id)
  └─ walks integrations.known_providers()
     filters enabled + last_refresh_status != "failed"
     ──────►  agent_config["mcp"] = {token, base_url, servers}
                                              materialize .mcp.json
                                              (workspace.write_text, refuses overwrite)
                                              cli --allowed-tools=…,mcp__<srv>__<tool>,…
                                              └─ calls mcp__linear__get_issue
                                                                                 POST /api/mcp/{review_id}/linear
                                                                                  ├─ sha256 bearer → mcp_review_tokens
                                                                                  ├─ resolve org_id from review row
                                                                                  ├─ load mcp_credentials
                                                                                  ├─ enforce allowlist (write tools)
                                                                                  ├─ decrypt access_token
                                                                                  ├─ forward Authorization: Bearer ─►
                                                                                  └─ audit mcp.linear.dispatched
revoke_token(review_id) BEFORE workspace teardown
```

**Single org service account.** Each org connects one upstream OAuth identity per provider. Audit rows always carry `upstream_account="org_service_account"`. The triggering identity (User vs System) lives on `actor_kind`. Reviews fire reads/writes uniformly as the bot — never as the developer who triggered them.

**Attribution.** Manual UI review → `actor_kind=user`, the user's `user_id`. Webhook review → `actor_kind=system`, no IDs. The proxy preserves whichever the review was scheduled with — the row says who *triggered* the work, the `upstream_account` says who *executed* it.

**Refresh serialization (deferred).** When implemented, `domain/integrations.refresh(org_id, provider)` will use `pg_advisory_xact_lock(hashtext('mcp:' || org_id || ':' || provider))` so concurrent reviewers don't double-spend a refresh token. Until then the proxy surfaces `broken_creds` on token expiry and the hourly health-check + email notify the operator to reconnect.

**Audit shape.** One row per JSON-RPC method: `{kind: "mcp.<provider>.dispatched", payload: {provider, method, tool, args_hash, result_summary, upstream_account}}`. Never the full upstream response — it can contain customer data.

**Broken-creds surfacing (six layers).** Health-check flips `last_refresh_status`; audit row `mcp.<provider>.token_refresh_failed`; email to Owners (24h dedup); `/api/auth/me`'s `broken_integrations` per org; red banner in the app shell; warning block on Coding Agents → Claude Code; review-output prefix when the agent hit `broken_creds`/`not_connected` mid-run.

### Test stack

`docker-compose.test.yml` brings up Postgres + `apps/fake-github` + backend with `GITHUB_API_BASE_URL=http://fake-github:8080` and `YAAOS_CODING_AGENT_STUB=1`. Plugins stubbed via `app/testing/`. E2E specs drive preconditions via `POST /api/testing/reset` + `seed/*`.

## Cross-app conventions

### Time
- UTC on the wire. Postgres `timestamptz`; Python `datetime.now(UTC)`; Pydantic emits `Z`-suffixed ISO 8601.
- Browser converts to local at render only — all FE timestamp display goes through `formatTime` / `formatDateTime` in `apps/web/src/shared/utils/ago.ts`.

### Webhook authenticity
- Inbound webhooks MUST carry `X-Hub-Signature-256`. `plugins/github` verifies HMAC against `YAAOS_GITHUB_APP_WEBHOOK_SECRET` before dispatch.
- `apps/fake-github` signs outbound test webhooks with the same secret so the production verification path runs unchanged.
- Idempotency: `github_webhook_events` keyed by `X-GitHub-Delivery` UUID; duplicates skipped with `INSERT ... ON CONFLICT DO NOTHING`.

### Audit log
One append-only `audit_log` table owned by `core/audit_log` records business-meaningful state changes. Row carries `{id, org_id, created_at, entity_kind, entity_id, kind ("<entity>.<verb_past>"), payload (Pydantic-validated JSONB), actor}`. Reads never write. Progress steps go to structlog, not audit. Each domain module writes its own entries.

### Org scoping
Every domain function takes `org_id` kwarg; every query filters by it. Multi-org from M02 onward; per-request org comes from the `X-Org-Slug` header (HTTP) or the `org_context()` async-context-manager (background jobs).

### Identity & access

Users, orgs, memberships, sessions, OAuth + SAML SSO live in `domain/identity` + `domain/orgs`. `core/auth` owns the security middleware: every `/api/*` route declares its security via `Depends(require(action))` or `Depends(public_route)`; the middleware enforces `X-Org-Slug` resolution, sets contextvars (`org_id`, `user_id`, `actor_kind`, `actor_id`), and 500s the response if no route declared security. Sessions are opaque server-side rows (sha256-hashed tokens), `HttpOnly; SameSite=Lax; Secure`-flagged cookies, double-submit CSRF on mutations. SSO satisfaction tracked per-session per-org with an 8-hour TTL. Background jobs open `org_context(org_id, actor_kind, actor_id)` to set the same contextvars + OTel + structlog fields the HTTP middleware sets.

**Login flow:**

```
SPA   GET /api/auth/login?provider=github
   ─────────────────────────────────────► backend
                                          ├─ signs `state` (10m TTL)
                                          └─ 302 → GitHub authorize URL
GitHub  GET /api/auth/callback/github
   ─────────────────────────────────────► backend
                                          ├─ verify state signature
                                          ├─ exchange code → ProviderProfile
                                          ├─ orchestrator: existing identity → return user
                                          │   email-match no-link → auto-link, return user
                                          │   no match           → JIT create user
                                          │     (with invite → also create membership)
                                          ├─ TOTP step-up if user has verified secret
                                          │   AND provider mfa_satisfied=False
                                          ├─ sessions.create() (HttpOnly + CSRF cookies)
                                          ├─ audit_log emits `logged_in` per org
                                          └─ 303 → next path
```

**Session lifecycle:** rotate on role change + invite accept + SSO satisfaction. `sessions.revoke_all_for_user` on member removal + logout-all. Periodic cleanup (`domain/identity/scheduler`) purges expired sessions, expired invitations, unverified-TOTP secrets >24h, and audit rows older than `AUDIT_LOG_RETENTION` (30d).

**Contextvar propagation:** HTTP middleware sets `org_id_var` / `user_id_var` / `actor_kind_var` / `actor_id_var` per request; background jobs open `with org_context(...)`. `require_org_context()` raises in functions that read org-scoped tables without context. OTel spans + structlog log lines carry `yaaos.org_id` + `yaaos.actor_kind` everywhere.

Per-module deep dives: [`core_auth`](../apps/backend/docs/core_auth.md), [`domain_identity`](../apps/backend/docs/domain_identity.md), [`domain_orgs`](../apps/backend/docs/domain_orgs.md), [`plugins_github`](../apps/backend/docs/plugins_github.md), [`core_saml`](../apps/backend/docs/core_saml.md).

### Secrets at rest
All at-rest secrets go through [`core/secrets`](../apps/backend/docs/core_secrets.md) — a single Fernet wrapper resolving the master key from `YAAOS_TOTP_MASTER_KEY` (fallback `YAAOS_ENCRYPTION_KEY` in non-prod). Callers: `domain/identity/totp`, `domain/orgs/sso`, [`core/byok`](../apps/backend/docs/core_byok.md), and legacy plugin-settings tables. Plaintext crosses the boundary only at write (caller → encrypt) and at the specific call site that needs the decrypted value; never logged, never echoed in errors, never placed in audit payloads.

### Settings surface (M03)

`/orgs/{slug}/settings/{section}` consolidates every per-org knob into one shell with six sub-pages: `auth` (SSO + session-timeout override), `members`, `vcs`, `coding-agents`, `byok`, `audit`. Member role sees only the Members tab; Owner+Admin see all. The shell + tab nav live in `apps/web/src/domain/org_settings/`.

- **VCS**: one plugin per org, state on `orgs.vcs_plugin_id` + `orgs.vcs_settings`. The picker hits `GET /api/plugins/available?type=vcs`. GitHub's install handshake is driven by a dedicated `POST /api/github/install/start` (returns the state-signed github.com URL as JSON; the SPA navigates to it via `window.location.href`). The install callback writes back via `domain/orgs.set_vcs`. All mutations audit. The `VCSPlugin.install_url(org_id)` protocol method exists for future plugins that need a browser-redirect-only install with no signed state.
- **Coding Agents**: many installs per org in `org_coding_agents` keyed by `(org_id, plugin_id)`. The generic shell handles install/uninstall + the picker; per-plugin settings dispatch via a frontend registry (`coding_agents/plugin_registry.ts`). The `claude_code` plugin ships a bespoke settings UI (orchestrator + 1..8 sub-agents) reading defaults from `GET /api/claude_code/defaults` (request-time imports, never cached).
- **BYOK**: `core/byok` owns `byok_keys` per `(org_id, provider)`; plaintext is encrypted via `core/secrets`. Plugins register validators at boot (`core/byok.register_validator`) so `/api/api-keys/{provider}/validate` dispatches without `core/byok` importing plugins. The Anthropic key surfaces twice — once on the BYOK page and once embedded in the Claude Code settings page — both writing the same row.
- **Session-timeout override**: nullable `orgs.session_timeout_override` (minutes). The `require()` dep checks `last_seen_at + override` (falls back to `SESSION_IDLE_TIMEOUT` = 12h) on every org-scoped request and 401's `session_idle_expired` past the window.
- **Verified GitHub username**: `users.github_username` is a denorm written by the "Sign in with GitHub" login flow on every successful sign-in. Re-binding to a different GitHub handle is "sign in with GitHub again" — there's no separate verify-only endpoint.

### Dumb frontend
SPA renders data and dispatches actions. It does not compute verdicts, derive status, hold permissions, or own any rule the backend doesn't also enforce. FE validation is for UX immediacy; backend re-validates. See [`apps/web/docs/patterns.md`](../apps/web/docs/patterns.md).

## M05 — workspace agent + workflow engine

M05 reshapes how reviews actually execute. Three new concepts cross every app:

- **Workflow engine** (`core/workflow`) — typed `Workflow` definitions registered at startup, driven by three taskiq task bodies (`start_step`, `handle_agent_event`, `route_workflow`) over the existing `core/tasks` + `core/outbox` substrate. Workspace commands park in `awaiting_agent` and resume on the wire-protocol terminal event; workers never block on long-running agent work. Five workflows ship: `pr_review_v1`, `incremental_review_v1`, `verify_fix_v1`, `stale_check_v1`, `answer_question_v1`. Definitions in [`domain/reviewer/workflows/`](../apps/backend/app/domain/reviewer/workflows/); 13 commands across [`domain/reviewer/commands/`](../apps/backend/app/domain/reviewer/commands/) + [`core/workspace/commands.py`](../apps/backend/app/core/workspace/commands.py). See [`core_workflow.md`](../apps/backend/docs/core_workflow.md).
- **Workspace provider abstraction** (`core/workspace`) — two implementations behind one Protocol: `InMemoryWorkspaceProvider` (existing in-process) and `RemoteAgentWorkspaceProvider` (new — dispatches via the wire protocol to a customer-deployed Go agent). Per-org config selects the provider (`orgs.workspace_provider`). Single-flight claim via `try_claim`/`release_claim` enforces "one in-flight AgentCommand per workspace"; the failure-report-precedes-disposal invariant preserves `current_holder_workflow_id` across release so reconciliation lookups always resolve.
- **WorkspaceAgent** (`apps/agent/`) — customer-deployed Go binary that holds customer source code locally. Talks to the control plane via five HTTPS endpoints + one bidirectional WebSocket under `/api/v1/`:
  - `POST /api/v1/identity/exchange` — SigV4-signed STS → 24h bearer. Replays the customer's signed `GetCallerIdentity` against AWS STS (yaaos holds no AWS credentials — fly.io hosts the control plane; only outbound HTTPS to STS is needed), canonicalizes the returned ARN, matches against `orgs.registered_iam_arn`, cross-checks `orgs.aws_region`, issues a real bearer via the `bearer_tokens` ledger (sha256 hash stored; plaintext returned once). Per-IP + per-pod rate limits.
  - `POST /api/v1/agents/{id}/heartbeat` — liveness + workspace inventory reconciliation.
  - `POST /api/v1/agents/{id}/commands/claim` — long-poll for the next AgentCommand.
  - `POST /api/v1/workspaces/{id}/events` — workspace-state transitions.
  - `POST /api/v1/commands/{id}/events` — AgentCommand events; terminal events resume the workflow.
  - `WSS /api/v1/agents/{id}/activity` — bidirectional activity stream with `subscribe`/`unsubscribe` from the backend on the `0 → 1` / `1 → 0` UI-subscriber transitions (demand-pull: no activity flows when nobody's watching). See [`core_agent_gateway.md`](../apps/backend/docs/core_agent_gateway.md).

### End-to-end (current foundations)

1. GitHub webhook → `POST /api/intake/github_pr` ([`domain/intake/web.py`](../apps/backend/app/domain/intake/web.py)) verifies HMAC, dedups via `X-Github-Delivery`, calls `domain/tickets.create(type="pr_review", payload=…, idempotency_key=…)`, starts the workflow via `core/workflow.get_engine().start("pr_review_v1", ticket_id=…)`. The intake records the active `traceparent` so downstream tasks share the trace.
2. `route_workflow` task picks up; first step is `CheckShouldReview` (Local; admission gate). Then `ProvisionWorkspace` (Workspace category) — `start_step` parks the workflow in `awaiting_agent` and dispatches via either the in-process provider or `core/agent_gateway.enqueue_command` (for the remote agent).
3. Agent picks up the command via its long-poll loop; runs the operation; reports outcome via `POST /api/v1/commands/{id}/events`. Backend's `record_agent_event` validates the stale-claim guard, resolves the lookup chain `command_id → workspaces → current_holder_workflow_id`, enqueues `handle_agent_event` via the outbox.
4. `handle_agent_event` validates the pending command id, clears the claim, enqueues `route_workflow` which transitions to the next step (`CodeReview` → `PostFindings` → `CleanupWorkspace`).
5. Activity events from the workspace process flow over the WebSocket only when a UI tab is subscribed — `SubscriberRegistry` issues `subscribe` on the first SSE attach, `unsubscribe` when the last detaches.

### Persistence

New tables: `workflow_executions`, `pending_human_decisions`, `outbox_entries`, `workspace_agents`, `bearer_tokens`. Existing tables extended: `tickets` (type, idempotency_key, payload, current_workflow_execution_id), `workspaces` (provider, current_command_id, current_holder_workflow_id, max_idle_seconds), `orgs` (workspace_provider, registered_iam_arn, aws_region). Activity events are **never persisted** — they exist only in flight from WebSocket → `core/sse_pubsub` → SSE → UI. State of record stays in audit + workflow rows.

### M05 status

M05 ships **foundations across every phase**: schema + module surfaces + Pydantic types + wire protocol + Go skeleton + Dockerfile + docs. Integration follow-on still required for the full milestone-done bar — see [`plan/milestones/M05-workspace-agent/PHASES.md`](../plan/milestones/M05-workspace-agent/PHASES.md) for the per-phase deferral annotations and what remains.

## Stack at a glance

| Concern | Choice |
|---|---|
| Backend | Python 3.13, FastAPI |
| Frontend | Node 22, React + TanStack Router + TanStack Query + Tailwind |
| Data store | Postgres 16 |
| ORM / migrations | SQLAlchemy 2.0 async + Alembic (hand-edited) |
| Background work | `asyncio.create_task` via `core/primitives.spawn` |
| Config | pydantic-settings (boot) + DB rows (runtime) |
| API | REST + SSE |
| Tests | pytest, Vitest, Playwright |
| Telemetry | OpenTelemetry SDK → collector → sink |

Boot-time env vars: see [`apps/backend/docs/core_config.md`](../apps/backend/docs/core_config.md).
