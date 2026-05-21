# M05 — phase ledger

> Source of truth for what's done. Edit the checkboxes; do not delete entries.

**Status:** preliminary. Phases are sketched from [implementation-plan.md](implementation-plan.md) but the decomposition will be refined once the strategic gaps in [requirements.md](requirements.md) are resolved. Do not begin autonomous execution against this ledger until those gaps are closed.

## Phase 0a — module-naming hygiene

- [x] Rename `domain/auth` → `domain/sessions`. Update all import sites.
- [x] Merge `domain/byok` into `domain/orgs/byok_routes.py`. Delete `domain/byok` directory. Update ~5 import sites. URLs unchanged.
- [x] Rename `plugins/in_process_workspace` → `plugins/in_memory_workspace`. Update all import sites.
- [x] Rename `apps/backend/docs/domain_auth.md` → `domain_sessions.md`; move BYOK doc content into `domain_orgs.md`; delete `domain_byok.md`; rename `plugins_in_process_workspace.md` → `plugins_in_memory_workspace.md`.
- [x] Add "no module-name collisions across core/domain/plugins" rule to `apps/backend/docs/modularity.md`.
- [x] Run `apps/backend/bin/sync_modules`; tach happy.
- [x] All existing CI green.

## Phase 0 — required-session pattern + refactor existing code

Pure plumbing change. No behavior change. Lands before any new M05 modules so they're built on the new convention.

- [x] `apps/backend/docs/patterns.md` — new "Session management + atomicity" section with the required-session rule and one short example.
- [x] `core/audit_log.audit()` refactored: required session, no commit, optional-session branch deleted.
- [x] All callers of `audit()` updated to pass session explicitly.
- [x] Refactor every transactional service function in the codebase to take required `session: AsyncSession` and not commit. Grep audit: `grep -rn "session: AsyncSession | None\|session is None" apps/backend/app/` returns zero hits in service modules.
- [x] Add semgrep rule `apps/backend/.semgrep/no_optional_session.yaml` (ERROR severity). `bin/ci` semgrep step picks it up.
- [x] Document convention in `apps/backend/docs/patterns.md`: type signature self-documenting (orchestrators don't take session; transactional services require it). No `_owns_session` suffix. Read-only services follow the same rule.
- [x] Endpoint handlers + `spawn()` task bodies are the orchestrating layer: they open `db_session()`, call services, commit.
- [x] Functions that legitimately own their own session (fire-and-forget maintenance, periodic tasks) are clearly named or live in entrypoint modules; documented in patterns.md.
- [x] Tests updated to pass session fixtures explicitly.
- [x] All existing CI green post-refactor: `apps/backend/bin/ci`, `apps/web/bin/ci`, `apps/e2e/bin/ci`.

## Phase 0b — scaffolding

- [ ] Migration `0XX_create_all_m05` registered. taskiq uses Redis (no Postgres tables for queue itself; only our `outbox_entries` table needs migration).
- [ ] Tables created: `tickets` (with new `type` column), `workflow_executions` (with `pending_agent_command_id` + `cancel_requested`), `pending_human_decisions`, `workspaces` (extended schema), `workspace_agents`, `outbox_entries`, `reviews`, `findings` (simplified). `review_jobs` dropped.
- [ ] **Redis service added** to `docker/docker-compose.yml` and `docker/docker-compose.test.yml`. CI brings Redis up; tests assume Redis available (no mocking).
- [ ] New backend modules skeletoned: `core/agent_gateway`, `core/workflow`, `core/tasks`, `core/outbox`, `core/sse_pubsub`, `domain/intake` (extended), `domain/tickets` (extended).
- [ ] `core/tasks` scaffold: taskiq broker configured with Redis (`REDIS_URL` from settings); `@task` decorator wrapping taskiq; `enqueue(task, args, *, session)` routes through `core/outbox` for atomic-in-session enqueue; `TaskContext` dataclass; worker entrypoint at `apps/backend/bin/worker` (runs taskiq workers + outbox drain in same process).
- [ ] `apps/backend/docs/core_tasks.md` doc skeleton.
- [ ] Per-module doc skeletons in `apps/backend/docs/` for each new module.
- [ ] `core/workspace` extended skeleton: `WorkspaceProvider` Protocol declared / refined.
- [ ] `apps/agent/` Go module skeletoned (`cmd/agent/`, `internal/supervisor/`, `internal/workspace/`, `internal/ipc/`, `internal/identity/`).
- [ ] `apps/backend/openapi/agent-api.yaml` skeleton.
- [ ] `docs/setup.md` updated with M05 dev-story note (agent + worker process).
- [ ] `apps/backend/docs/patterns.md` updated with new patterns (WorkflowCommand interface, workspace provider contract, `core/tasks` usage).
- [ ] `apps/backend/bin/ci` exits 0; tach happy with new modules.

## Phase 0c — OTel SDK wiring (no exporter)

- [ ] Install `opentelemetry-sdk`, `opentelemetry-instrumentation-fastapi`, `opentelemetry-instrumentation-asyncpg`.
- [ ] Extend `core/observability.configure()`: `TracerProvider` (no exporter), `TraceContextTextMapPropagator` set as global.
- [ ] FastAPI + asyncpg auto-instrumentation wired through `core/observability`.
- [ ] structlog processor injects `trace_id` + `span_id` onto every log record.
- [ ] In-memory `SpanExporter` fixture for tests.
- [ ] `apps/backend/docs/core_observability.md` documents the conventions + the "no exporter in prod yet" note.
- [ ] All existing CI green.

## Phase 1 — `core/workflow` engine (async event-driven model)

- [ ] `Workflow` + `Step` Pydantic data structures.
- [ ] `WorkflowCommand` interface with `category` (Workspace/Local/HITL), `execute()` returning `Outcome`.
- [ ] `Outcome` types: success-with-outputs, failure-with-reason, hitl-pending-with-question.
- [ ] `append_steps` mechanism.
- [ ] `WorkflowEngine` class with registries + `start()`.
- [ ] **Three `core/tasks` tasks:** `start_step`, `handle_agent_event`, `route_workflow`.
- [ ] `start_step` branches on Command category: Workspace dispatches + exits (`state=awaiting_agent`); Local runs inline; HITL writes pending decision.
- [ ] `handle_agent_event` triggered by `core/agent_gateway`; validates `pending_agent_command_id` match; enqueues `route_workflow`.
- [ ] `route_workflow` persists outcome, applies retry budget, evaluates transitions.
- [ ] State machine includes `awaiting_agent` state + `pending_agent_command_id` column on `workflow_executions`.
- [ ] Event-to-workflow lookup chain in `core/agent_gateway` (`agent_command_id → workspaces → current_holder_workflow_id`).
- [ ] Atomic state transitions + outbox enqueue in single Postgres transaction.
- [ ] Tier-2 retry per step policy.
- [ ] Tier-3 transition on retry exhaustion.
- [ ] Cancellation (Floor 2): `cancel_requested` check; cancel during `awaiting_agent` waits for event then routes cleanup.
- [ ] HITL pause + resume API.
- [ ] OTel span propagation: workflow span + child spans per task.
- [ ] Tests: Local-only workflow; Workspace step async cycle; failure + retry; HITL pause + resume; append_steps; backend restart with `awaiting_agent` workflows; cancellation during `awaiting_agent`; idempotent duplicate event handling.
- [ ] **Async-model load test:** 100 simultaneous workflows dispatching long-running AgentCommands all dispatch within < 1s wall time (verifies workers don't block).

## Phase 2 — extend `domain/tickets` + `domain/intake`

- [ ] `domain/tickets` extensions: add `type` column (default `'pr_review'` for existing rows), `idempotency_key text unique`, `current_workflow_execution_id uuid` nullable FK. Reconcile state machine (existing `in_review|complete|abandoned` mapped to new `running|done|cancelled`; new `pending` + `failed` added). New method `create(type, payload, idempotency_key)`.
- [ ] Intake type registry (internal to `domain/intake`).
- [ ] `github_pr` intake type registered.
- [ ] Webhook endpoint `POST /api/intake/{type}` — verifies, dedups, creates ticket, starts workflow, returns 200.
- [ ] Tests: signature failure → 401; duplicate → idempotent 200; happy path → ticket + workflow execution + enqueued task.

## Phase 3 — `core/workspace` extensions + `InMemoryWorkspaceProvider`

- [ ] `WorkspaceProvider` Protocol finalized.
- [ ] Workspace records + lifecycle state machine in `core/workspace`.
- [ ] Single-flight enforcement via atomic `current_command_id` claim.
- [ ] Recovery policy registry (initial: `auth_expired → RefreshWorkspaceAuth`).
- [ ] Workspace-lifecycle WorkflowCommands: `CreateWorkspace`, `CleanupWorkspace`, `RefreshWorkspaceAuth`.
- [ ] `InMemoryWorkspaceProvider` implementation: spawns subprocesses for git + Claude Code in-process; enforces all invariants.
- [ ] Workspace-to-workflow binding via `current_holder_workflow_id`.
- [ ] Cleanup failsafes: TTL sweep, idle timeout, reconciliation hooks.
- [ ] Failure-report-precedes-disposal invariant enforced.
- [ ] Tests: provision/use/cleanup; single-flight; recovery; failure-report; TTL expiry; cleanup idempotency.

## Phase 4 — `domain/coding_agent` + `domain/reviewer` evolution (ALL five task modes as WorkflowCommands)

- [ ] `domain/coding_agent` builds the `invocation` block of `InvokeClaudeCode` AgentCommand payloads; per-mode prompt configuration.
- [ ] `domain/reviewer/admission.py` — extract gate logic from `queue.py:769-834` + `aggregate.py:post_process_raw_findings` into a single pure function.
- [ ] **Workspace WorkflowCommands (5):** `CodeReview`, `IncrementalReview`, `VerifyFix`, `StaleCheck`, `AnswerQuestion`. Each invokes the matching coding_agent method.
- [ ] **Local WorkflowCommands:** `CheckShouldReview`, `PostFindings`, `ResolveFinding`, `ArchiveStaleFindings`, `PostReply`.
- [ ] **Five workflow definitions in `domain/reviewer/workflows/`:** `pr_review_v1`, `incremental_review_v1`, `verify_fix_v1`, `stale_check_v1`, `answer_question_v1`.
- [ ] All workflows + commands register with `core/workflow` at startup.
- [ ] **`domain/reviewer/queue.py` dismantled:** `schedule_review`, `_run_review_job_inner`, `_inflight_tasks`, `cancel_pending`, inline admission filters — all removed. File deleted at end of phase. No spawn()-based reviewer code remains.
- [ ] `review_jobs` table dropped (per Topic 2 lock).
- [ ] Tests: E2E for each of the 5 workflows against `InMemoryWorkspaceProvider`. Span linkage assertion. Admission gates tested. Cross-review fingerprint dedup test.

## Phase 5 — `core/agent_gateway` + wire protocol

- [ ] OpenAPI spec finalized: five endpoints + AgentCommand union + AgentEvent schemas + `traceparent` + errors.
- [ ] Pydantic codegen on backend side; oapi-codegen on Go side. CI regenerates both.
- [ ] `core/agent_gateway` implementation: per-agent in-memory queue, long-poll, identity exchange (placeholder verifier), heartbeat with inventory ingestion, event ingestion.
- [ ] Stale-claim guard: `410 Gone` on attempt mismatch.
- [ ] Tests: long-poll 204 / 200; heartbeat reconciliation; event routing.

## Phase 6 — Go agent (supervisor + workspace processes)

- [ ] Supervisor subcommand: identity exchange, long-poll workers, command routing, heartbeat loop, disk janitor.
- [ ] Workspace subcommand: command pipe reader, event pipe writer, clone, Claude Code invocation, cleanup.
- [ ] IPC framing library in `internal/ipc/`: JSON-newline messages, partial-read handling, error envelopes.
- [ ] `os/exec`-based workspace spawning; pipes wired; SIGTERM-with-grace then SIGKILL.
- [ ] Workspace exit handling: terminal event emitted on its behalf if not already emitted.
- [ ] Startup reconciliation: inventory `/var/agent/workspaces/`, report in first heartbeat.
- [ ] Wall-clock timeout per AgentCommand.
- [ ] Secret-redaction wrapper type; logging discipline enforced.
- [ ] Tests: IPC framing unit tests; integration test against fake backend running full CreateWorkspace → WriteFiles → InvokeClaudeCode → CleanupWorkspace cycle.
- [ ] **Go OTel SDK wired (no exporter):** `go.opentelemetry.io/otel`, `propagation.TraceContext` set globally, supervisor extracts `traceparent` from AgentCommand payloads + WebSocket messages, per-operation spans, workspace process inherits via env vars.
- [ ] In-memory exporter for Go tests; trace-continuity test (supervisor extracts and emits child span).

## Phase 7 — `RemoteAgentWorkspaceProvider` + identity exchange

- [ ] `RemoteAgentWorkspaceProvider` in `core/workspace`: dispatches via `core/agent_gateway`.
- [ ] Real STS verifier in `core/agent_gateway`: replays signed STS, extracts ARN, looks up registered customer.
- [ ] Customer ARN registration UI in Org Settings (provider type selection + ARN entry).
- [ ] Provisioning policy: least-loaded reachable agent.
- [ ] Tests: same E2E as Phase 4, against `RemoteAgentWorkspaceProvider` (docker-compose with Go agent + fake STS).

## Phase 8 — span propagation across the wire

- [ ] `traceparent` header threaded through every wire request (AgentCommand payloads + WebSocket activity messages).
- [ ] Supervisor exports `TRACEPARENT` env to workspace process on spawn.
- [ ] Workspace process exports same env to Claude Code subprocess.
- [ ] E2E assertion: one trace ID covers `webhook → ... → terminal outcome` across both providers, for all five workflows.

## Phase 8b — Activity streaming (CodingAgent → UI) with demand-pull

- [ ] **Bidirectional WebSocket endpoint** `WSS /v1/agents/{id}/activity` on `core/agent_gateway`.
- [ ] uvicorn ping/pong configured (`--ws-ping-interval=30 --ws-ping-timeout=10`) for ALB idle-timeout survival.
- [ ] Auth on WebSocket upgrade: bearer in `Authorization` header; `4401` close on invalid.
- [ ] WebSocket message protocol implemented: `subscribe`/`unsubscribe` from backend; `activity_batch` from agent.
- [ ] WorkspaceAgent supervisor maintains `subscribed_workspaces: Set` in-memory; batches events at ~250ms.
- [ ] `core/agent_gateway` tracks `subscriber_counts: Map[workflow_execution_id, int]`; SSE handler `0→1` triggers subscribe, `1→0` triggers unsubscribe.
- [ ] WebSocket reconnect handler re-derives + re-sends subscriptions for active SSE subscribers.
- [ ] `domain/coding_agent` ActivityEvent pre-renderer audited: metadata only, no source content.
- [ ] In-memory provider: taskiq worker publishes directly to `core/sse_pubsub` (no WebSocket wire).
- [ ] Tests: activity stream end-to-end against both providers; demand-pull (no events without subscriber); WebSocket reconnect; trust-boundary (no source content in ActivityEvent payloads).

## Phase 9 — packaging + release

- [ ] Dockerfile for `apps/agent/` producing a static-binary image.
- [ ] Public image registry decision recorded (TBD strategic gap — see requirements).
- [ ] Image tagging strategy recorded.
- [ ] `apps/agent/docs/README.md` with deployment guide.
- [ ] Local dev story documented: `docker-compose up` with backend + agent + fake STS.

## Phase 10 — docs + completeness audit + CI green

- [ ] Per-module docs: `core_agent_gateway.md`, `core_workflow.md`, `domain_ticket.md`, `domain_intake.md`. Updates to `core_workspace.md`, `domain_reviewer.md`, `domain_coding_agent.md`.
- [ ] `docs/system-architecture.md` workspace-agent section added.
- [ ] **`docs/system-security.md` written — only what's shipped in M05** (sections: trust boundaries, control plane security, agent + workspace security, wire protocol security, data at rest, threat model, cross-references). Every section's content is backed by a real code path; no aspirational content.
- [ ] **`plan/notes/security-posture.md` slimmed**: split items into "shipped → moved to docs/" vs "still future → stays in note". Note retains only the unfinished security agenda; not deleted.
- [ ] `docs/glossary.md` entries: Intake, Ticket, Workflow, WorkflowCommand, AgentCommand, Workspace, Agent.
- [ ] `apps/backend/docs/patterns.md`: workflow-command discipline, single-flight pattern, failure-report-precedes-disposal invariant.
- [ ] Completeness audit: walk every section of `requirements.md`; prove each requirement shipped.
- [ ] Provider parity audit: same E2E suite passes against both providers.
- [ ] Trace-linkage audit: trace ID continuous from webhook to PR comment.
- [ ] Cleanup-failsafes audit: fault-injection tests for each of the 7 failsafes.
- [ ] Full CI green: `apps/backend/bin/ci`, `apps/web/bin/ci`, `apps/agent/bin/ci`, `apps/e2e/bin/ci` all exit 0 on fresh checkout.

## Handoff

- [ ] Tick M05 in `AUTONOMOUS_RUN.md`.
- [ ] `/loop clear`.
- [ ] Output final summary including `DECISIONS.md` contents.
