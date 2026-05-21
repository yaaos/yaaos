# M05 implementation plan

> Phased build order. Read [requirements.md](requirements.md) and [architecture.md](architecture.md) first.

**Status:** preliminary. M05 is still in the requirements phase — the phase decomposition below is a working sketch. Strategic gaps (versioning, multi-tenancy, customer-side observability, MCP proxy details) need their own design rounds before this plan is locked.

## Phase 0a — module-naming hygiene + modularity rule documented

Small standalone refactor. Lands before Phase 0.

- **Rename `domain/auth` → `domain/sessions`.** Updates all import sites (likely tens of files — every protected endpoint).
- **Merge `domain/byok` into `domain/orgs`.** The 134 lines of BYOK routes move into `domain/orgs/byok_routes.py`. `domain/byok` directory deleted. Imports updated (~5 sites). Existing routes (`/api/byok/{provider}` etc.) keep their URLs — only the implementation file moves.
- **Rename `plugins/in_process_workspace` → `plugins/in_memory_workspace`.** Matches the `WorkspaceProvider` enum value (`in_memory`). Updates all import sites (handful).
- Rename `apps/backend/docs/domain_auth.md` → `domain_sessions.md`. Move BYOK content from `domain_byok.md` into `domain_orgs.md`; delete `domain_byok.md`. Rename `plugins_in_process_workspace.md` → `plugins_in_memory_workspace.md`.
- Add to `apps/backend/docs/modularity.md`: **"Never reuse a module name across `core/`, `domain/`, `plugins/`."** Document the rationale.
- Run `apps/backend/bin/sync_modules`.
- All existing CI green.
- Note: `core/auth` and `core/byok` keep their names. `domain/auth` and `plugins/in_process_workspace` are renamed; `domain/byok` is dissolved into `domain/orgs`.

## Phase 0 — required-session pattern adoption + refactor existing code

Lands **first** because the new modules in subsequent phases all assume the new convention. Pure refactor — no behavior change.

- New section in `apps/backend/docs/patterns.md`: "Session management + atomicity." Documents the required-session rule with one short example. Lists the exception ("functions that own their own session must be named `_owns_session` or live in clearly-marked entrypoints").
- `core/audit_log.audit()` — drop the optional-session branch, require session, never commit. Update all callers (most already pass session; the few that don't get updated).
- `core/database`: confirm `db_session()` is the standard session-opener; document any naming convention shift.
- Refactor every transactional service function in the codebase to take a required `session: AsyncSession` parameter and not commit. Grep targets: `async with .*session()`, `await s.commit()`, etc. Common refactor sites identified from existing code: `domain/reviewer/queue.py` (e.g., `schedule_review`, `cancel_inflight_reviews`), `core/workspace` lifecycle functions, audit-call sites, any background-task entrypoints (`spawn()` callsites).
- Endpoint handlers and `spawn()` task bodies become the orchestrating layer: open `db_session()`, call services, commit.
- Tests updated to pass session fixtures explicitly instead of relying on services to open their own.
- All existing CI green (`apps/backend/bin/ci`, `apps/web/bin/ci`, `apps/e2e/bin/ci`) — refactor is no-behavior-change so tests should pass post-refactor with minimal updates.
- **Semgrep rule** added at `apps/backend/.semgrep/no_optional_session.yaml`. Patterns: `session: AsyncSession | None = ...` (in def/async def signatures) and `if session is None:`. Severity ERROR. Existing semgrep step in `bin/ci` picks it up automatically.
- Audit: `grep -rn "session is None\|session: AsyncSession | None" apps/backend/app/` returns zero hits in service modules after this phase. Semgrep enforces the rule going forward.
- **Convention documented:** type signature is self-documenting — orchestrators (endpoints, `spawn()` task bodies, `core/tasks` task bodies, periodic-task entrypoints) don't take a `session` parameter and own the transaction. Transactional services (everything else) require `session: AsyncSession` and never commit. No `_owns_session` suffix needed.
- **Read-only services follow the same rule:** required `session`, never commit (no writes to commit anyway). Enables snapshot-consistent read-then-write in caller's transaction.

## Phase 0b — scaffolding

- Single named migration `014_create_all_m05` registered in `core/database/service.py:_MIGRATIONS`.
- New tables (see [architecture.md § Data model](architecture.md#data-model)): `tickets`, `workflow_executions`, `pending_human_decisions`, `workspaces` (extended), `workspace_agents`, `outbox_entries`, `reviews`, `findings`. `review_jobs` table dropped (Gen 1 cutover, no backfill).
- **Redis added as infrastructure.** Real Redis service in `docker/docker-compose.yml` + `docker/docker-compose.test.yml`. Backend reads `REDIS_URL` from settings. CI brings up Redis container; tests assume Redis available (no in-memory mocking).
- New modules: `core/agent_gateway`, `core/workflow`, `core/tasks`, `core/outbox`, `core/sse_pubsub` (skeletons + per-module doc skeletons in `apps/backend/docs/`).
- **Existing modules to extend** (not new): `domain/intake`, `domain/tickets`. M05 adds new public methods + schema columns; does not rewrite existing logic. See [architecture.md § M05 extension of existing domain/intake + domain/tickets](architecture.md#m05-extension-of-existing-domainintake--domaintickets).
- `core/tasks` scaffold: taskiq `Broker` configured with `REDIS_URL`. `@task` decorator wrapping taskiq's. `enqueue(task, args, *, session: AsyncSession, queue=...)` API — internally calls `core/outbox.write(session, kind="taskiq_enqueue", payload=...)`. `TaskContext` dataclass (session + traceparent + attempt + job_id). Worker entrypoint at `apps/backend/bin/worker` that runs taskiq workers + outbox drain in the same process.
- `core/outbox` scaffold: `outbox_entries` table written via `core/database` migration. `outbox.write(session, kind, payload)` primitive. Outbox drain loop (~100ms polling) reads undispatched rows and pushes to Redis (or other targets) by `kind`. Periodic prune of dispatched rows.
- `core/sse_pubsub` scaffold: `publish(channel, event)` + `subscribe(channel) -> AsyncIterator` against Redis pub/sub. Channel naming: `activity:{workflow_execution_id}`.
- Extend `core/workspace` skeleton: `WorkspaceProvider` Protocol declared (or refined if already present); `InMemoryWorkspaceProvider` + `RemoteAgentWorkspaceProvider` stubbed.
- New go module skeleton: `apps/agent/` with `cmd/agent/`, `internal/supervisor/`, `internal/workspace/`, `internal/ipc/`, `internal/identity/`. `go.mod` set up.
- `apps/backend/openapi/agent-api.yaml` skeleton.
- **RWX CI config:** separate build target for `apps/agent/` (Go binary + Docker image) independent of `apps/backend/bin/ci`. New `apps/agent/bin/ci` script.
- `docs/setup.md` updated: M05 adds Redis container + WorkspaceAgent + separate worker process; local dev uses `in_memory` provider via docker-compose.
- `apps/backend/docs/patterns.md` updated with "WorkflowCommand interface" + "workspace provider contract" + "core/tasks usage" + "core/outbox pattern" sections.
- `apps/backend/docs/core_tasks.md` and `apps/backend/docs/core_outbox.md` doc skeletons.

## Phase 0c — OTel wiring (backend SDK, no exporter)

OTel is NOT currently wired in `core/observability/`. Phase 1's span work depends on it.

- Install dependencies: `opentelemetry-sdk`, `opentelemetry-instrumentation-fastapi`, `opentelemetry-instrumentation-asyncpg`, `opentelemetry-propagator-b3` (optional), `opentelemetry-api`.
- Extend `core/observability.configure()`: create `TracerProvider` (no exporter), register `TraceContextTextMapPropagator` as global.
- FastAPI + asyncpg auto-instrumentation wired through `core/observability` so they're configured uniformly.
- Add structlog processor that injects `trace_id` + `span_id` from active span context onto every log record.
- Test helper: in-memory `SpanExporter` fixture for assertions.
- Document in `apps/backend/docs/core_observability.md`: how to open spans, naming conventions, "no exporter in prod yet — hook one up when ready."
- Run `apps/backend/bin/ci`; assert all existing tests still pass post-wiring.

## Phase 1 — `core/workflow` engine (async event-driven model)

The engine that everything else hangs off. Land first.

- `Workflow` and `Step` Pydantic data structures (per [architecture.md § Workflow + WorkflowCommand model](architecture.md#workflow--workflowcommand-model)).
- `WorkflowCommand` interface with `kind`, `category` (Workspace/Local/HITL), `restart_safe`, `inputs_schema`, `outputs_schema`, `execute()`.
- `Outcome` types (success, failure, hitl-pending) + `append_steps` mechanism.
- `WorkflowEngine` class: register workflows, register WorkflowCommands, `start(workflow_name, ticket_id)`.
- **`core/tasks` integration: THREE tasks registered:**
  - `start_step(exec_id, step_id, attempt, inputs, traceparent)` — dispatches based on Command category (Workspace dispatches AgentCommand and exits; Local runs inline and enqueues `route_workflow`; HITL writes pending decision and exits).
  - `handle_agent_event(exec_id, agent_command_id, outcome_label, outputs, traceparent)` — triggered by `core/agent_gateway` when AgentCommand terminal event arrives; clears `pending_agent_command_id`, enqueues `route_workflow`.
  - `route_workflow(exec_id, completed_step_id, outcome_label, outputs, traceparent)` — persists outcome, evaluates transitions, enqueues next `start_step` or marks terminal.
- **State machine implementation:** including `awaiting_agent` state with `pending_agent_command_id` field. Atomic state-change + outbox-enqueue in single transaction.
- **Event-to-workflow lookup chain** in `core/agent_gateway`: `agent_command_id → workspaces.current_command_id → workspaces.current_holder_workflow_id → workflow_execution`. Enqueues `handle_agent_event` via `core/outbox`. Validates `pending_agent_command_id` match before enqueueing.
- HITL primitives: `pending_human_decisions` writes, `awaiting_human` state, `resume(workflow_execution_id, response)` API.
- Three-tier retry: tier-2 (step retry per policy) and tier-3 (transition on exhaustion) in the engine. Tier-1 (AgentCommand recovery in `core/workspace`) is its concern.
- Cancellation (Floor 2): `cancel_requested` flag check in `route_workflow`. Cancel during `awaiting_agent` waits for terminal event, then transitions to cleanup path.
- Span propagation: workflow span on start, step span per `start_step`, child spans on `handle_agent_event` and `route_workflow`, persistence in `otel_trace_context`.
- Unit tests:
  - Local-only workflow runs to completion (no agent dispatch).
  - Workspace step: `start_step` exits in `awaiting_agent`; simulated event triggers `handle_agent_event`; workflow advances.
  - Step failure → retry → fail_workflow.
  - HITL pause + resume.
  - `append_steps` inserts at front.
  - Backend-restart resume: kill worker mid-workflow; verify pending `core/tasks` task picked up and `awaiting_agent` workflows resume on event arrival.
  - Cancellation during `awaiting_agent`: cancel + event → cleanup path.
  - Stale event handling: event arrives for a workflow that already advanced; `handle_agent_event` exits cleanly.

**Key tests for the async model specifically:**
- Worker doesn't block on long-running AgentCommands (assert via: spawn 100 workflows simultaneously, all dispatch AgentCommands that "complete" after 10 seconds; assert all 100 dispatch within < 1 second total wall time).
- Lookup-chain integrity: agent_command_id → workspace → workflow execution.
- Idempotent event handling: send the same event twice; second is a no-op.

## Phase 2 — extend existing `domain/tickets` + `domain/intake`

Both modules already exist; this phase adds M05's capabilities without rewriting the existing logic.

### `domain/tickets` extensions

- Migration adds: `type text not null` column (default `pr_review` for existing rows), `idempotency_key text unique` column, `current_workflow_execution_id uuid` nullable FK.
- State machine reconciliation: rename / merge states per [architecture.md § state machine reconciliation](architecture.md#m05-extension-of-existing-domainintake--domaintickets). New states `pending` and `failed` added.
- Add `domain/tickets.create(type, payload, idempotency_key) -> ticket_id` method. Existing creation paths (used by current intake) get updated to call through this method.
- Existing HTTP routes continue to work.
- Tests: ticket creation with type + dedup on idempotency_key; existing audit and listing endpoints still pass post-state-rename.

### `domain/intake` extensions

- Existing VCS event routing + filters + parsing unchanged.
- The point where existing code today creates a `review_job` (Gen 1) or kicks off a review: replace with `domain/tickets.create(type="pr_review", payload=..., idempotency_key=...)` + `core/workflow.start("pr_review_v1", ticket_id=...)`.
- Existing `@yaaos rereview` parsing continues to drive ticket creation (same flow, just routed through the new path).
- Tests: signature failure → 401 (existing); duplicate webhook → idempotent ticket (new); successful intake creates ticket + workflow execution + enqueues `route_workflow` task via the outbox.

## Phase 3 — `core/workspace` extensions + InMemoryWorkspaceProvider

Provider contract lands first; everything downstream depends on it.

- `WorkspaceProvider` Protocol finalized.
- Workspace records, lifecycle state machine in `core/workspace`. Single-flight enforcement (atomic `current_command_id` claim). Recovery policy registry (initial: `auth_expired → RefreshWorkspaceAuth`).
- Workspace-lifecycle WorkflowCommands implemented in `core/workspace`: `CreateWorkspace`, `CleanupWorkspace`, `RefreshWorkspaceAuth`. Each issues internal "AgentCommand to provider" calls and awaits terminal events.
- `InMemoryWorkspaceProvider` implementation: spawns subprocesses for git + Claude Code locally inside the backend container. Enforces all invariants (single-flight, recovery, failure-report-precedes-disposal).
- Workspace-to-workflow binding via `current_holder_workflow_id`.
- Cleanup failsafes: TTL sweep, idle timeout, reconciliation hooks (no remote agent yet but the API exists).
- Tests: provision/use/cleanup happy path; single-flight enforcement (second concurrent dispatch returns busy); recovery (forced auth_expired → refresh applied → original re-dispatched); failure-report-precedes-disposal (forced workspace failure → terminal event observed before disposal); TTL expiry triggers cleanup; cleanup is idempotent.

## Phase 4 — `domain/coding_agent` + `domain/reviewer` evolution (ALL FIVE task modes as WorkflowCommands)

Scope: full migration of the five CodingAgent Plugin Protocol task modes into the WorkflowEngine. **No legacy spawn()-based reviewer code remains after this phase.**

### `domain/coding_agent` extensions

- Shared CodingAgent invocation machinery (builds the `invocation` block of `InvokeClaudeCode` AgentCommand payloads). Used by all five task-mode WorkflowCommands.
- Per-mode prompt templates (or one generic with mode-aware directive parameter).

### `domain/reviewer/admission.py` — new consolidated module

Extract gate logic from existing `queue.py:769-834` + `aggregate.py:post_process_raw_findings`. Single pure function:

```
admit_findings(drafts: list[FindingDraft], ctx: AdmissionContext) -> list[FindingDraft]
```

Filters: schema gate, off-diff drop, severity threshold, nit cap (5), within-review fingerprint dedup, cross-file dedup, top-10 cap. Called by `CodeReview` + `IncrementalReview` WorkflowCommands.

### `domain/reviewer/workflows/` — five workflow definitions

- `pr_review_v1.py` — `CheckShouldReview → ProvisionWorkspace → CodeReview → PostFindings → CleanupWorkspace`
- `incremental_review_v1.py` — `CheckShouldReview → ProvisionWorkspace → IncrementalReview → PostFindings → CleanupWorkspace`
- `verify_fix_v1.py` — `ProvisionWorkspace → VerifyFix → ResolveFinding → CleanupWorkspace`
- `stale_check_v1.py` — `ProvisionWorkspace → StaleCheck → ArchiveStaleFindings → CleanupWorkspace`
- `answer_question_v1.py` — `ProvisionWorkspace → AnswerQuestion → PostReply → CleanupWorkspace`

All register with `core/workflow` at startup.

### `domain/reviewer/commands.py` — WorkflowCommand implementations

Workspace (call CodingAgent):
- `CodeReview` — uses `coding_agent.review()`
- `IncrementalReview` — uses `coding_agent.incremental_review()`
- `VerifyFix` — uses `coding_agent.verify_fix()`
- `StaleCheck` — uses `coding_agent.stale_check()`
- `AnswerQuestion` — uses `coding_agent.answer_question()`

Local (post-processing):
- `CheckShouldReview` — PR draft / skip-label / external-contributor / org-config gating
- `PostFindings` — posts newly-raised findings to GitHub via VCS module; idempotent on `(finding_id, external_thread_id)`
- `ResolveFinding` — for verify_fix outcomes; flips finding to resolved via existing finding logic
- `ArchiveStaleFindings` — for stale_check outcomes
- `PostReply` — for answer_question outcomes; posts CodingAgent's reply as a thread message

### `domain/reviewer/queue.py` — dismantled

The existing `queue.py` is the legacy spawn()-based orchestrator. After Phase 4, **nothing in the codebase invokes it.** Its responsibilities are absorbed:
- `schedule_review` → replaced by `domain/intake.create_ticket + core/workflow.start`.
- `_run_review_job_inner` → distributed across the WorkflowCommand implementations.
- `_inflight_tasks` registry + `cancel_pending` → replaced by `core/workflow`'s `cancel_requested` mechanism (Floor 2).
- Admission inline logic → moved to `domain/reviewer/admission.py`.
- queue.py file itself deleted at end of Phase 4. Imports updated.

### Existing `review_jobs` table — dropped

Per Topic 2 lock: drop the table. No backfill. New `reviews` + `findings` tables (per architecture data model) populated by the new flows.

### Tests

- E2E for each of the five workflows against `InMemoryWorkspaceProvider`. Each tests: intake → ticket → workflow → CodingAgent → outcome (PR comment posted / finding resolved / reply posted / etc.).
- Span linkage assertion: one trace covers webhook → terminal across all five.
- Admission gate tests (per-gate, in isolation).
- Cross-review fingerprint dedup test (run pr_review twice on same PR; second posts no duplicates).

## Phase 5 — `core/agent_gateway` + wire protocol

The wire side, exercised initially with a Go agent stub.

- OpenAPI spec finalized: all five endpoints + AgentCommand discriminated union + AgentEvent schemas + traceparent fields + error envelopes.
- Pydantic codegen on backend side; oapi-codegen on Go side. CI regeneration step.
- `core/agent_gateway` implementation: per-agent in-memory command queue, long-poll endpoint, identity exchange endpoint (placeholder STS verifier — real one in Phase 7), heartbeat endpoint with inventory ingestion, event endpoints.
- Stale-claim guard: 410 on attempt mismatch.
- Tests: long-poll behavior (returns 204 on timeout, 200 on command arrival within window); heartbeat reconciliation (agent reports unknown workspace → control plane responds with cleanup); event ingestion routes to `core/workspace`.

## Phase 6 — Go WorkspaceAgent — supervisor + workspace processes + OTel SDK

- Supervisor subcommand: identity exchange at startup, long-poll workers, command routing, heartbeat loop, disk janitor.
- Workspace subcommand: command pipe reader, event pipe writer, clone, Claude Code invocation, cleanup.
- IPC framing library in `internal/ipc/`: JSON-newline messages, partial-read handling, error envelopes.
- `os/exec`-based workspace spawning. Pipe wiring. SIGTERM-with-grace then SIGKILL on supervisor-driven termination.
- Process supervision: workspace exit code → terminal event emitted on its behalf if not already emitted.
- Startup reconciliation: inventory `/var/agent/workspaces/`, report in first heartbeat.
- Wall-clock timeout per AgentCommand.
- Logging: structured JSON; secret-redaction wrapper type from day one.
- **OTel SDK setup (Go side, no exporter — same pattern as backend Phase 0c):**
  - `go.opentelemetry.io/otel` SDK installed; `TracerProvider` with no exporter configured.
  - `propagation.TraceContext` set as global text-map propagator.
  - Supervisor: extracts `traceparent` from inbound AgentCommand payloads + WebSocket activity messages, creates child spans under that parent.
  - Workspace process: inherits `TRACEPARENT` / `TRACESTATE` env vars at spawn; exports them to Claude Code subprocess too.
  - Per-operation spans: claim, dispatch, event-forward, clone, invoke, cleanup.
  - Exporter intentionally left unconfigured for M05 — customer SREs (or yaaos team) configure `OTEL_EXPORTER_OTLP_ENDPOINT` when ready to ship to Datadog or similar.
  - In Go tests: in-memory exporter for span assertions.
- Tests: unit tests for IPC framing; integration tests that spawn the supervisor against a fake backend and run CreateWorkspace → WriteFiles → InvokeClaudeCode → CleanupWorkspace cycle; OTel trace continuity test (supervisor extracts and emits child span).

## Phase 7 — RemoteAgentWorkspaceProvider + identity exchange + onboarding UI

- `RemoteAgentWorkspaceProvider` implementation in `core/workspace`: dispatches via `core/agent_gateway` to the agent that owns the workspace. Awaits terminal event (async event-driven model — `start_step` exits, `handle_agent_event` fires when terminal arrives).
- Real STS verifier in `core/agent_gateway`: replays signed STS request, extracts ARN, looks up registered customer.
- **Org Settings → Workspaces page** (`apps/web/src/domain/org_settings/workspaces/`):
  - Empty state with "Create Workspace" button.
  - Provider selection (In Memory / Remote) on first creation.
  - For Remote: two-panel form (what yaaos provides / what customer provides) with IAM role ARN input. Single ARN per org.
  - **Connection status panel** (passive, no button): polls `GET /api/workspaces/connection_status` every ~3s. Shows red/yellow/green based on aggregated pod heartbeat state. Detail line shows pod count + last heartbeat age.
  - "Reset workspace setup" action (Owner-only, with confirmation).
- Backend endpoints:
  - `PUT /api/org_settings/workspace` (Owner/Admin) — set provider + (for remote) `registered_iam_arn`. Idempotent.
  - `DELETE /api/org_settings/workspace` (Owner/Admin) — reset to empty.
  - `GET /api/workspaces/connection_status` — returns `{state, pod_count, latest_heartbeat_at}` aggregating `workspace_agents` rows for the org.
- Audit kinds: `workspace.configured` (provider set), `workspace.reset` (provider cleared). Per-pod events use `workspace_agent.connected` / `workspace_agent.lost`.
- Provisioning policy: least-loaded reachable pod (used when ECS desired_count > 1).
- Setup docs in `docs/setup.md` extended: IAM role trust + permissions policy templates, full ECS task definition JSON, env var reference, CloudWatch log group setup.
- Tests: end-to-end PR review flow against `RemoteAgentWorkspaceProvider` using a locally-spawned Go agent (in docker-compose). Same E2E test as Phase 4, parameterized over provider. UI test: register agent, "Test connection" polling green path + timeout warning path.

## Phase 8 — span propagation across the wire

- `traceparent` header threaded through every wire request.
- Supervisor exports `TRACEPARENT` env to workspace process on spawn.
- Workspace process exports same env to Claude Code subprocess.
- Span linkage assertions in E2E: one trace ID covers `webhook → ... → comment posted`.

## Phase 8b — Activity streaming (CodingAgent → UI) with demand-pull

- New **bidirectional** WebSocket endpoint `WSS /v1/agents/{id}/activity` on `core/agent_gateway`. Backend (Python): FastAPI/Starlette native. Agent (Go): `github.com/coder/websocket`.
- uvicorn ping/pong configured (`--ws-ping-interval=30 --ws-ping-timeout=10`) to survive AWS ALB 60s idle timeout. Captured in `docs/setup.md`.
- Auth on WebSocket upgrade: bearer token in `Authorization` header; reject with `4401` on invalid.
- WebSocket message protocol:
  - Backend → WorkspaceAgent: `{type: "subscribe", workspace_id}` / `{type: "unsubscribe", workspace_id}`.
  - WorkspaceAgent → backend: `{type: "activity_batch", workspace_id, events: [...]}`.
- WorkspaceAgent supervisor: maintains `subscribed_workspaces: Set[workspace_id]`. Receives ActivityEvents from workspace process pipes, drops events whose workspace_id is not subscribed, batches subscribed ones at ~250ms, sends as `activity_batch` frames.
- `core/agent_gateway` subscriber tracking: `subscriber_counts: Map[workflow_execution_id, int]`. SSE handler increments on connect / decrements on disconnect. Transitions `0→1` and `1→0` drive `subscribe` / `unsubscribe` sends down the WebSocket.
- `core/agent_gateway` ingests `activity_batch` frames → calls `core/sse_pubsub.publish("activity:{workflow_execution_id}", event)`.
- SSE handler in `web.py` subscribes to `core/sse_pubsub`, forwards to client.
- WebSocket reconnect handling: on new connection from same agent, re-derive subscriptions and re-send subscribe messages. Clean up per-connection state on disconnect.
- `domain/coding_agent` ActivityEvent pre-rendering: audit + enforce "metadata only — never source content" invariant. Add a sanitization layer if needed.
- In-memory provider: workspace process child of taskiq worker; the demand-pull logic still applies — taskiq worker publishes to `core/sse_pubsub` only when there's a live subscriber. No WebSocket wire (in-process).
- Tests: end-to-end activity stream against both providers; trust-boundary test (asserts no source content in any ActivityEvent payload); demand-pull test (assert no activity flows until SSE subscriber attaches; flows stop when SSE detaches); WebSocket reconnect test (forced disconnect, verify subscriptions rebuilt).

## Phase 9 — packaging + release

- Dockerfile for `apps/agent/` producing a static-binary image (~15MB).
- Public image registry decision (TBD — see strategic gaps).
- Image tagging strategy (TBD).
- `apps/agent/docs/README.md` with deployment guide.
- Local dev story documented: `docker-compose up` to bring up backend + agent + fake STS.

## Phase 10 — documentation + cleanup + final verification

- Per-module docs: `apps/backend/docs/core_agent_gateway.md`, `core_workflow.md`, `domain_ticket.md`, `domain_intake.md`. Updates to `core_workspace.md`, `domain_reviewer.md`, `domain_coding_agent.md`.
- `docs/system-architecture.md`: workspace agent section (entity diagram, trust boundary, lifecycle, OTel propagation).
- **`docs/system-security.md` (new) — describes ONLY what is shipped as of M05.** No aspirational content, no "future hardening" promises for things not in the code. Sections:
  - Trust boundaries (as actually enforced by the M05 code paths)
  - Control plane security (auth, authorization, secret storage, audit log, inbound + outbound auth, DB access — all as implemented)
  - Agent + workspace security (trust model, identity establishment, process isolation via path validation + container RO fs + `os.RLimit`, workspace process surface, subprocess containment, secret-handling discipline, failure-report invariant — all as implemented)
  - Wire protocol security (TLS, sigv4, long-poll model, traceparent propagation, stale-claim guard — as implemented)
  - **Data flow + retention boundaries** — what crosses yaaos in-memory but is never persisted. Explicitly: MCP request/response bodies flow through yaaos's proxy in memory but are NEVER persisted (only audit metadata — tool name, args_hash, result_summary — is stored). Customer source code never crosses at all.
  - Data security at rest (as implemented)
  - Threat model (against the implemented surface)
  - Cross-references
  Each section's content must be backed by a real code path. If a security property exists only in the plan, it does not appear here.
- **`plan/notes/security-posture.md` stays in place, slimmed down.** During M05 closure, walk the existing note and split items into two buckets: (a) "shipped in M05 → goes to `docs/system-security.md`", (b) "still future, deferred past M05 → stays in the note." After the split, the note retains only future-facing items (per-workspace UID + landlock + seccomp, per-workspace egress restrictions, customer-side audit surface, multi-tenancy fairness, etc.). The note is NOT deleted — it captures the unfinished security agenda for future milestones.
- `docs/glossary.md`: Intake, Ticket, Workflow, WorkflowCommand, AgentCommand, Workspace, Agent.
- `apps/backend/docs/patterns.md`: workflow-command discipline, single-flight enforcement pattern, failure-report-precedes-disposal invariant.
- Full CI green (`apps/backend/bin/ci`, `apps/web/bin/ci`, `apps/e2e/bin/ci`, plus a new `apps/agent/bin/ci` for the Go side).
- Completeness audit: every section of `requirements.md`, prove it shipped.

## Dependency order

```
0a → 0 → 0b → 0c → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 8b → 9 → 10
```

Phase 0a (module-naming hygiene) lands first — small, pure rename(s) + plugin rename + modularity rule, low risk, but every other phase imports from the renamed modules. Phase 0 (required-session refactor) lands second — pure plumbing change, no behavior change, but the rest of M05 assumes it. Phase 0b (scaffolding) brings in Redis, `core/tasks` + `core/outbox` + `core/sse_pubsub` + new modules + taskiq setup + the new `tickets.type` column + the dropped `review_jobs` table. **Phase 0c (OTel SDK wiring, no exporter)** is a prerequisite for all span emission in Phase 1+. Phase 8b (activity streaming) layers on top of Phase 7 (remote provider live) — exercises the WebSocket activity channel.

Phases 1–4 are backend-only (build the engine + intake + provider contract + reviewer reshape, validate against the InMemoryWorkspaceProvider). The full PR-review flow runs end-to-end against `in_memory` by the end of Phase 4 — that's the first milestone-shape validation point.

Phases 5–7 add the wire protocol and the Go agent. Phase 7 is the second validation point: same flow, real wire, real Go process.

Phases 8–10 are span propagation, packaging, docs.

## Cross-cutting through every phase

- TDD: failing test first.
- Triplet tests on protected endpoints (M02 pattern).
- Per-phase doc updates in the same commit as code.
- `apps/backend/bin/sync_modules` after any module interface change.
- Per-phase commit + ledger tick (once execution scaffolding lands).

## Risks (engineering)

- **The two-provider contract drift problem.** If `in_memory` is allowed to bend rules ("it's just a dev tool"), tests pass but the remote provider hits invariants that were never really enforced. Mitigation: same E2E suite runs against both providers; CI fails if either diverges.
- **Pipe-IPC edge cases.** Workspace processes can crash mid-write, can emit unexpected stdout, can deadlock if the supervisor stops reading. The framing library + supervised reads need thorough tests; otherwise we get rare prod hangs.
- **OTel context propagation gaps.** Easy to drop the context at a process or worker boundary. Linkage assertion in E2E catches drops but only for the happy path.
- **Task-queue abstraction integrity.** We use taskiq via the `core/tasks` wrapper; the rest of the codebase doesn't import taskiq directly. Code review discipline: any new direct `import taskiq` outside `core/tasks` requires justification. Any feature beyond `enqueue` + `retry on hard crash` requires extending `core/tasks`'s typed API rather than reaching past it.
- **Migration scope of existing review_job rows.** If there are many in-flight ones at cutover, the migration needs care. Probably small enough at POC stage that "cancel + restart" is fine.
- **Existing `core_workspace` shape.** We've deferred reading the current code. The extension strategy may turn out to require restructuring (renames, public-interface changes). Surface during Phase 3.
