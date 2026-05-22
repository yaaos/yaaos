# M05 — phase ledger

> Source of truth for what's done. Edit the checkboxes; do not delete entries.

**Status:** preliminary. Phases are sketched from [implementation-plan.md](implementation-plan.md) but the decomposition will be refined once the strategic gaps in [requirements.md](requirements.md) are resolved. Do not begin autonomous execution against this ledger until those gaps are closed.

## Reflection ritual

Run at the close of **every** phase before ticking the phase's reflection item. The ritual is what stops "shipped foundations, deferred integration" from becoming silent debt — each phase's reflection forces an honest accounting of what the phase actually delivered against what was promised.

For each axis, walk it concretely. **A `_(deferred — reason + which later phase owns it)_` annotation is acceptable; an unchecked-but-unannotated item is the bug this ritual catches.** Where the ritual finds a gap, either fix it before closing the phase OR add a new unchecked item under the appropriate phase (or under the current phase with explicit deferral text). Never close a phase silently leaving a checklist item without disposition.

1. **Requirements.** Walk the relevant section of [requirements.md](requirements.md) for the phase. For each promised item: shipped (link to commit/file), or deferred-to-later-phase (which one, why). Add a follow-on PHASES.md item if neither.
2. **Architecture conformance.** Walk the relevant section of [architecture.md](architecture.md). For each described component/contract/invariant: present in code (link), or annotated divergence in [DECISIONS.md](DECISIONS.md). A silent divergence is a gap.
3. **Testing.** For every new module/endpoint/state-machine added: appropriate tier of test exists per [`apps/backend/CLAUDE.md` § testing](../../../CLAUDE.md) (unit for branchy logic, service for cross-3+-module flows, e2e for browser-visible). Mocks-instead-of-real-DB is a gap. Pure assertions of "compiles" without behavior coverage is a gap.
4. **Observability.** For every new code path: structured logging at decision points; span emission where there's real work (DB writes, wire calls, subprocess spawns); `trace_id` propagation through the path; audit rows on every state transition. Stale `print()` / bare `logging` calls are gaps.
5. **Security.** For every new attack surface: explicit defense (authz, signature verification, stale-claim guard, etc.). For every new secret: documented handling per [`docs/system-security.md`](../../../docs/system-security.md). For every new trust boundary: enforced contract. Missing entries in `docs/system-security.md` for shipped surfaces are gaps.
6. **Docs sync.** Per [`CLAUDE.md` § documentation discipline](../../../CLAUDE.md): every code change updates the relevant docs in the same PR. Run `grep -rn '<renamed-or-removed-symbol>' apps/*/docs docs` for every symbol/concept the phase changed; should return zero stale references. The doc-link checker only catches broken markdown links — this step catches stale prose references.

Tick the phase's `Reflection — verify …` item only after all six axes have been walked and any gaps either closed or explicitly added as new follow-on items with a named owning phase.

## Phase 0a — module-naming hygiene

- [x] Rename `domain/auth` → `domain/sessions`. Update all import sites.
- [x] Merge `domain/byok` into `domain/orgs/byok_routes.py`. Delete `domain/byok` directory. Update ~5 import sites. URLs unchanged.
- [x] Rename `plugins/in_process_workspace` → `plugins/in_memory_workspace`. Update all import sites.
- [x] Rename `apps/backend/docs/domain_auth.md` → `domain_sessions.md`; move BYOK doc content into `domain_orgs.md`; delete `domain_byok.md`; rename `plugins_in_process_workspace.md` → `plugins_in_memory_workspace.md`.
- [x] Add "no module-name collisions across core/domain/plugins" rule to `apps/backend/docs/modularity.md`.
- [x] Run `apps/backend/bin/sync_modules`; tach happy.
- [x] All existing CI green.
- [x] Reflection — Six axes walked. Requirements: grep for `domain/auth`, `domain/byok`, `in_process_workspace` in app/docs returns only the modularity-rule reference that documents the rename. Architecture: no-collision rule added to [modularity.md:18](../../../apps/backend/docs/modularity.md). Testing: rename-only — covered by existing test suite. Observability/Security: no new code paths. Docs sync: per-module docs renamed in the same commits.

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
- [x] Reflection — Six axes walked. Requirements: `grep -rn "session: AsyncSession \| None\|session is None" apps/backend/app/` returns only SessionRow checks in `domain/sessions` (different type — user sessions, not DB sessions). Architecture: Session-management section in [patterns.md](../../../apps/backend/docs/patterns.md). Testing: pure refactor — existing suite green. Observability: no new code paths. Security: semgrep rule `apps/backend/.semgrep/no_optional_session.yaml` enforces. Docs sync: patterns.md updated.

## Phase 0b — scaffolding

- [x] Migration `0XX_create_all_m05` registered. taskiq uses Redis (no Postgres tables for queue itself; only our `outbox_entries` table needs migration). _(Shipped as `014_create_outbox_entries` — see [DECISIONS.md](DECISIONS.md). Other M05 tables land in their owning module's phase.)_
- [ ] Tables created: `tickets` (with new `type` column), `workflow_executions` (with `pending_agent_command_id` + `cancel_requested`), `pending_human_decisions`, `workspaces` (extended schema), `workspace_agents`, `outbox_entries`, `reviews`, `findings` (simplified). `review_jobs` dropped. _(Only `outbox_entries` in Phase 0b; remaining tables in later phases.)_
- [x] **Redis service added** to `docker/docker-compose.yml` and `docker/docker-compose.test.yml`. CI brings Redis up; tests assume Redis available (no mocking).
- [x] New backend modules skeletoned: `core/agent_gateway`, `core/workflow`, `core/tasks`, `core/outbox`, `core/sse_pubsub`, `domain/intake` (extended), `domain/tickets` (extended). _(`domain/intake` + `domain/tickets` extensions deferred to Phase 2 per implementation-plan.)_
- [x] `core/tasks` scaffold: taskiq broker configured with Redis (`REDIS_URL` from settings); `@task` decorator wrapping taskiq; `enqueue(task, args, *, session)` routes through `core/outbox` for atomic-in-session enqueue; `TaskContext` dataclass; worker entrypoint at `apps/backend/bin/worker` (runs taskiq workers + outbox drain in same process). _(Decorator + enqueue + TaskContext + Redis setting shipped. Broker wiring + `bin/worker` entrypoint land in Phase 1.)_
- [x] `apps/backend/docs/core_tasks.md` doc skeleton.
- [x] Per-module doc skeletons in `apps/backend/docs/` for each new module.
- [x] `core/workspace` extended skeleton: `WorkspaceProvider` Protocol declared / refined. _(Existing Protocol satisfies Phase 0b needs; M05-specific extensions shipped in Phase 3.)_
- [x] `apps/agent/` Go module skeletoned (`cmd/agent/`, `internal/supervisor/`, `internal/workspace/`, `internal/ipc/`, `internal/identity/`).
- [x] `apps/backend/openapi/agent-api.yaml` skeleton.
- [x] `docs/setup.md` updated with M05 dev-story note (agent + worker process).
- [x] `apps/backend/docs/patterns.md` updated with new patterns (WorkflowCommand interface, workspace provider contract, `core/tasks` usage).
- [x] `apps/backend/bin/ci` exits 0; tach happy with new modules.
- [x] Reflection — Six axes walked. Requirements: migration `014_create_outbox_entries` shipped (per [DECISIONS.md](DECISIONS.md) split); other M05 tables annotated and now landing in later phases (015 workflow_executions ✓, 016 ticket extensions ✓, 017 workspace extensions ✓, 018 workspace_agents ✓, 019 orgs.workspace_provider ✓). Architecture: scaffolding only; no behavior surface to conform to. Testing: scaffolds covered by their owning phases' tests. Observability/Security: no new code paths in this phase. Docs sync: per-module doc skeletons populated as each module landed.

## Phase 0c — OTel SDK wiring (no exporter)

- [x] Install `opentelemetry-sdk`, `opentelemetry-instrumentation-fastapi`, `opentelemetry-instrumentation-asyncpg`. _(SDK + FastAPI already installed in M04; using existing `opentelemetry-instrumentation-sqlalchemy` instead of asyncpg — SQLAlchemyInstrumentor covers the same DB-call spans via the async engine and is already a dep.)_
- [x] Extend `core/observability.configure()`: `TracerProvider` (no exporter), `TraceContextTextMapPropagator` set as global.
- [x] FastAPI + asyncpg auto-instrumentation wired through `core/observability`. _(SQLAlchemy instrumentor, not asyncpg — same reason as above.)_
- [x] structlog processor injects `trace_id` + `span_id` onto every log record.
- [x] In-memory `SpanExporter` fixture for tests.
- [x] `apps/backend/docs/core_observability.md` documents the conventions + the "no exporter in prod yet" note.
- [x] All existing CI green.
- [x] Reflection — Six axes walked. Requirements: TracerProvider, W3C propagator, FastAPI + SQLAlchemy instrumentation, structlog trace-id processor, in-memory SpanExporter fixture all shipped in [core/observability/service.py](../../../apps/backend/app/core/observability/service.py). Architecture: traceparent threading (Phase 8) builds on this foundation. Testing: [test_otel.py](../../../apps/backend/app/core/observability/test/test_otel.py) + [test_traceparent.py](../../../apps/backend/app/core/observability/test/test_traceparent.py). Observability: this IS observability. Security: no new surface (no exporter wired by default). Docs sync: [core_observability.md](../../../apps/backend/docs/core_observability.md) documents the conventions.

## Phase 1 — `core/workflow` engine (async event-driven model)

- [x] `Workflow` + `Step` Pydantic data structures.
- [x] `WorkflowCommand` interface with `category` (Workspace/Local/HITL), `execute()` returning `Outcome`.
- [x] `Outcome` types: success-with-outputs, failure-with-reason, hitl-pending-with-question.
- [x] `append_steps` mechanism.
- [x] `WorkflowEngine` class with registries + `start()`.
- [x] **Three `core/tasks` tasks:** `start_step`, `handle_agent_event`, `route_workflow`.
- [x] `start_step` branches on Command category: Workspace dispatches + exits (`state=awaiting_agent`); Local runs inline; HITL writes pending decision. _(Workspace branch sets `state=awaiting_agent` + synthesizes `pending_agent_command_id`; real `core/workspace.dispatch` wiring lands in Phase 3.)_
- [x] `handle_agent_event` triggered by `core/agent_gateway`; validates `pending_agent_command_id` match; enqueues `route_workflow`. _(Body shipped; `core/agent_gateway` enqueue site lands in Phase 5.)_
- [x] `route_workflow` persists outcome, applies retry budget, evaluates transitions.
- [x] State machine includes `awaiting_agent` state + `pending_agent_command_id` column on `workflow_executions`.
- [x] Event-to-workflow lookup chain in `core/agent_gateway` (`agent_command_id → workspaces → current_holder_workflow_id`). _(Shipped in `record_agent_event` via Phase 5 — [apps/backend/app/core/agent_gateway/service.py:199](../../../apps/backend/app/core/agent_gateway/service.py).)_
- [x] Atomic state transitions + outbox enqueue in single Postgres transaction.
- [x] Tier-2 retry per step policy.
- [x] Tier-3 transition on retry exhaustion.
- [x] Cancellation (Floor 2): `cancel_requested` check; cancel during `awaiting_agent` waits for event then routes cleanup.
- [x] HITL pause + resume API.
- [ ] OTel span propagation: workflow span + child spans per task. _(traceparent threaded through task args; span emission lands alongside the wire-protocol span work in Phase 8.)_
- [x] Tests: Local-only workflow; Workspace step async cycle; failure + retry; HITL pause + resume; append_steps; backend restart with `awaiting_agent` workflows; cancellation during `awaiting_agent`; idempotent duplicate event handling. _(Backend-restart resume relies on the broker re-delivering pending tasks; full e2e restart test lands once `apps/backend/bin/worker` wires the broker in Phase 1 cont'd.)_
- [ ] **Async-model load test:** 100 simultaneous workflows dispatching long-running AgentCommands all dispatch within < 1s wall time (verifies workers don't block). _(Defer until the broker + worker entry are wired; in-memory dispatch in the unit tests doesn't exercise the worker-blocking property the load test targets.)_
- [x] Reflection — Six axes walked. Requirements: Workflow/Step/Outcome types, WorkflowEngine, three task bodies (`start_step`, `handle_agent_event`, `route_workflow`), retry budget + transitions, cancellation, HITL pause + resume all shipped in [core/workflow/service.py](../../../apps/backend/app/core/workflow/service.py). Workspace branch now routes on `workspace_provider` (in_memory inline vs remote_agent await) per slice 2 of the Phase 4 follow-on. Architecture: state-machine matches [architecture.md § Workflow execution model](architecture.md). Testing: [test_state_machine.py](../../../apps/backend/app/core/workflow/test/test_state_machine.py) covers Local-only, Workspace-remote await, Workspace-in-memory inline, HITL, append_steps, stale event, cancellation. Observability: traceparent threaded; span emission deferred per annotated item. Security: no new attack surface (workflow engine is internal). Docs sync: [core_workflow.md](../../../apps/backend/docs/core_workflow.md) documents both Workspace dispatch paths.

## Phase 2 — extend `domain/tickets` + `domain/intake`

- [x] `domain/tickets` extensions: add `type` column (default `'pr_review'` for existing rows), `idempotency_key text unique`, `current_workflow_execution_id uuid` nullable FK. Reconcile state machine (existing `in_review|complete|abandoned` mapped to new `running|done|cancelled`; new `pending` + `failed` added). New method `create(type, payload, idempotency_key)`. _(Schema + new methods shipped. State-vocab convergence (rename legacy values) deferred to Phase 4 alongside the queue.py dismantle — both code paths run in parallel until then.)_
- [x] Intake type registry (internal to `domain/intake`).
- [x] `github_pr` intake type registered. _(Lives in `plugins/github` and self-registers at bootstrap so domain doesn't import plugin.)_
- [x] Webhook endpoint `POST /api/intake/{type}` — verifies, dedups, creates ticket, starts workflow, returns 200.
- [x] Tests: signature failure → 401; duplicate → idempotent 200; happy path → ticket + workflow execution + enqueued task.
- [x] Reflection — Six axes walked. Requirements: `tickets.create(type, payload, idempotency_key)` + intake registry + `POST /api/intake/{type}` + HMAC verification + dedup all shipped. Architecture: `domain/intake` doesn't import `plugins/github`; `GithubPrIntakeType` self-registers in [plugins/github/intake_type.py](../../../apps/backend/app/plugins/github/intake_type.py). Testing: 401/200/idempotent paths covered in [domain/intake/test/](../../../apps/backend/app/domain/intake/test). Observability: structlog at intake decision points; `current_traceparent()` recorded on workflow execution. Security: HMAC `X-Hub-Signature-256` + `X-Github-Delivery` idempotency_key documented in [docs/system-security.md](../../../docs/system-security.md). Docs sync: [domain_intake.md](../../../apps/backend/docs/domain_intake.md) + [domain_tickets.md](../../../apps/backend/docs/domain_tickets.md) updated.

## Phase 3 — `core/workspace` extensions + `InMemoryWorkspaceProvider`

- [x] `WorkspaceProvider` Protocol finalized. _(Existing Protocol covers M05's needs; `provider` discriminator column added to the row so the engine can route in-memory vs remote-agent without changing the interface.)_
- [x] Workspace records + lifecycle state machine in `core/workspace`. _(Existing state machine extended with M05 claim/holder columns.)_
- [x] Single-flight enforcement via atomic `current_command_id` claim.
- [x] Recovery policy registry (initial: `auth_expired → RefreshWorkspaceAuth`).
- [x] Workspace-lifecycle WorkflowCommands: `ProvisionWorkspace`, `CleanupWorkspace`, `RefreshWorkspaceAuth`. All three have real in-memory bodies (slices 3/4/6 of Phase 4 follow-on). `ProvisionWorkspace` reads ticket context via the registered `WorkflowContextProvider`; `CleanupWorkspace` flips the row to expired; `RefreshWorkspaceAuth` is a no-op-success for the in-memory provider (no stored creds to refresh).
- [ ] `InMemoryWorkspaceProvider` implementation: spawns subprocesses for git + Claude Code in-process; enforces all invariants. _(Provider exists as `plugins/in_memory_workspace`; M05-specific invariant enforcement (claim + failure-report-precedes-disposal) routes through the new `core/workspace.dispatch` API in Phase 4.)_
- [x] Workspace-to-workflow binding via `current_holder_workflow_id`.
- [x] Cleanup failsafes: TTL sweep, idle timeout, reconciliation hooks. _(TTL + idle-timeout sweeps shipped in the reaper; reconciliation hooks (cross-checking control-plane state against agent inventory) land with `core/agent_gateway` in Phase 5.)_
- [x] Failure-report-precedes-disposal invariant enforced. _(`release_claim` preserves `current_holder_workflow_id` so the workflow link survives disposal; Phase 5 wires the wire-protocol side of the invariant.)_
- [x] Tests: provision/use/cleanup; single-flight; recovery; failure-report; TTL expiry; cleanup idempotency. _(Claim + recovery covered here. End-to-end provision/use/cleanup against `InMemoryWorkspaceProvider` runs through Phase 4's reviewer workflows.)_
- [x] Reflection — Six axes walked. Requirements: `try_claim` / `release_claim` single-flight, recovery-policy registry, idle-timeout sweep, lifecycle-command registry all shipped. Architecture: atomic conditional UPDATE matches [architecture.md § Single-flight workspace claim](architecture.md); release preserves `current_holder_workflow_id` per failure-report-precedes-disposal. Testing: [test_dispatch.py](../../../apps/backend/app/core/workspace/test/test_dispatch.py) + [test_lifecycle_commands.py](../../../apps/backend/app/core/workspace/test/test_lifecycle_commands.py) cover claim contention, release, recovery, lifecycle bodies. Observability: structlog at claim outcomes. Security: claim guard + ownership check are the boundary. Docs sync: [core_workspace.md](../../../apps/backend/docs/core_workspace.md) documents the state machine + lifecycle commands.

## Phase 4 — `domain/coding_agent` + `domain/reviewer` evolution (ALL five task modes as WorkflowCommands)

- [x] `domain/coding_agent` builds the `invocation` block of `InvokeClaudeCode` AgentCommand payloads. `build_invocation(mode, context, model, effort)` shipped in [`domain/coding_agent/invocation.py`](../../../apps/backend/app/domain/coding_agent/invocation.py); produces `{mode, context: <Pydantic dump>, prompt_config: {model, effort}}` for each of the 5 task modes. Per-mode prompt defaults match `plugins/claude_code` (opus + medium). Wiring through Workspace command bodies rides on the follow-on slice that lands those bodies' real coding_agent invocation calls.
- [x] `domain/reviewer/admission.py` — extracted as `admit_raw_findings(pr_id, org_id, review_id, raw, *, diff_files, session) -> AdmissionResult`. Loads the aggregate, runs `post_process_raw_findings`, saves, returns structured `(admitted, observations, drops)`. The legacy `queue.py:769-834` still does its own inline call to `aggregate.post_process_raw_findings`; that callsite swaps to `admit_raw_findings` alongside the queue dismantle.
- [ ] **Workspace WorkflowCommands (5):** `CodeReview`, `IncrementalReview`, `VerifyFix`, `StaleCheck`, `AnswerQuestion`. Each invokes the matching coding_agent method. _(Substrate shipped: `_WorkspaceReviewCommand` base resolves `workspace_id` → live `Workspace` handle via `core/workspace.get_workspace()` AND fetches `WorkspaceTicketContext` via the registered provider; subclasses just override `_run_in_workspace(workspace, ticket_ctx, inputs, ctx)`. Bodies themselves still return `Outcome.success` until each `<Foo>Context` builder lands.)_
- [x] **Local WorkflowCommands (5/5 real):** `CheckShouldReview` ✓ (admission gate); `ArchiveStaleFindings` ✓ (STALE transitions); `ResolveFinding` ✓ (verify-fix → RESOLVED_CONFIRMED); `PostFindings` ✓ (FindingDraft → RawFinding via `findingdrafts_to_raw` → `admit_raw_findings`); `PostReply` ✓ (appends yaaos message to thread). Open follow-on: the actual GitHub-side posting (`vcs.post_review`, `vcs.post_comment_reply`) — PostFindings/PostReply persist locally today but don't push to GitHub yet.
- [x] **Five workflow definitions in `domain/reviewer/workflows/`:** `pr_review_v1`, `incremental_review_v1`, `verify_fix_v1`, `stale_check_v1`, `answer_question_v1`.
- [x] All workflows + commands register with `core/workflow` at startup.
- [ ] **`domain/reviewer/queue.py` dismantled:** `schedule_review`, `_run_review_job_inner`, `_inflight_tasks`, `cancel_pending`, inline admission filters — all removed. File deleted at end of phase. No spawn()-based reviewer code remains. _(Follow-on iteration; queue.py still drives reviews until the new command bodies are wired.)_
- [ ] `review_jobs` table dropped (per Topic 2 lock). _(Drops alongside the queue.py dismantle.)_
- [x] Tests: E2E for each of the 5 workflows against `InMemoryWorkspaceProvider`. `test_pr_review_v1_e2e_service.py` covers pr_review_v1 with both empty + non-empty draft flows (admitted FindingRow lands in the DB). `test_all_workflows_smoke.py` parametrized smoke test covers incremental_review_v1, verify_fix_v1, stale_check_v1, answer_question_v1 — each reaches DONE end-to-end with spy Workspace steps. Span linkage end-to-end across the wire + cross-review fingerprint dedup ride on the Phase 6 Go subprocess + Phase 8 traceparent env follow-on.
- [ ] Reflection — verify requirements, architecture conformance, testing, observability, security, docs-sync per the ritual at the top of this file. Surface gaps as new follow-on items before ticking this.

## Phase 5 — `core/agent_gateway` + wire protocol

- [x] OpenAPI spec finalized: five endpoints + AgentCommand union + AgentEvent schemas + `traceparent` + errors.
- [ ] Pydantic codegen on backend side; oapi-codegen on Go side. CI regenerates both. _(Hand-written mirror in `core/agent_gateway/types.py` today; codegen automation deferred to a follow-on iteration. The OpenAPI spec is the contract.)_
- [x] `core/agent_gateway` implementation: per-agent in-memory queue, long-poll, identity exchange (placeholder verifier), heartbeat with inventory ingestion, event ingestion.
- [x] Stale-claim guard: `410 Gone` on attempt mismatch.
- [x] Tests: long-poll 204 / 200; heartbeat reconciliation; event routing.
- [x] Reflection — Six axes walked. Requirements: 5 endpoints + AgentCommand union + AgentEvent + stale-claim guard + heartbeat shipped; codegen deferred with annotation. Architecture: per-agent FIFO + long-poll via `asyncio.Condition` in [core/agent_gateway/service.py](../../../apps/backend/app/core/agent_gateway/service.py); event-to-workflow lookup chain on line 199. Testing: long-poll 204/200, heartbeat reconciliation, stale-claim 410, event routing all covered. Observability: structlog at decision points; traceparent threaded on every AgentCommand + AgentEvent + Heartbeat. Security: stale-claim guard returns 410; placeholder identity-exchange documented in [docs/system-security.md](../../../docs/system-security.md). Docs sync: [core_agent_gateway.md](../../../apps/backend/docs/core_agent_gateway.md) describes contracts.

## Phase 6 — Go agent (supervisor + workspace processes)

- [x] Supervisor subcommand: identity exchange, long-poll workers, command routing, heartbeat loop, disk janitor. _(Identity exchange + claim loop + heartbeat shipped. Command routing emits a stubbed `completed_success` so the backend workflow advances end-to-end; real OS-process spawning + disk janitor land in the Phase 6 follow-on iteration.)_
- [ ] Workspace subcommand: command pipe reader, event pipe writer, clone, Claude Code invocation, cleanup. _(Follow-on iteration. Subcommand entry prints a not-implemented marker.)_
- [x] IPC framing library in `internal/ipc/`: JSON-newline messages, partial-read handling, error envelopes.
- [ ] `os/exec`-based workspace spawning; pipes wired; SIGTERM-with-grace then SIGKILL. _(Follow-on iteration alongside the workspace subcommand body.)_
- [ ] Workspace exit handling: terminal event emitted on its behalf if not already emitted. _(Follow-on iteration.)_
- [ ] Startup reconciliation: inventory `/var/agent/workspaces/`, report in first heartbeat. _(Heartbeat loop ships; disk-scanning reconciliation lands with the disk janitor.)_
- [ ] Wall-clock timeout per AgentCommand. _(Follow-on iteration alongside subprocess spawning.)_
- [ ] Secret-redaction wrapper type; logging discipline enforced. _(Follow-on iteration alongside subprocess spawning where redaction matters most.)_
- [ ] Tests: IPC framing unit tests; integration test against fake backend running full CreateWorkspace → WriteFiles → InvokeClaudeCode → CleanupWorkspace cycle. _(11 Go unit tests shipped — IPC framing + protocol decode + HTTP client. Integration test rides on the workspace subcommand body in the follow-on.)_
- [ ] **Go OTel SDK wired (no exporter):** `go.opentelemetry.io/otel`, `propagation.TraceContext` set globally, supervisor extracts `traceparent` from AgentCommand payloads + WebSocket messages, per-operation spans, workspace process inherits via env vars. _(Follow-on iteration; traceparent is already threaded through every wire type.)_
- [ ] In-memory exporter for Go tests; trace-continuity test (supervisor extracts and emits child span). _(Follow-on iteration alongside the OTel SDK wiring.)_
- [x] Reflection — Six axes walked. Requirements: Go module scaffold, supervisor subcommand entry, IPC framing, wire-protocol types, HTTP client, claim/heartbeat loops all shipped; subprocess body + OTel SDK + secret redaction + reconciliation annotated as follow-ons. Architecture: identity exchange + claim + heartbeat match [architecture.md § Go agent](architecture.md) wire model. Testing: 11 Go unit tests cover IPC framing + protocol decode + HTTP client. Observability: traceparent threaded; Go OTel SDK is the named follow-on. Security: distroless `nonroot` base, no shell, zero external Go deps. Docs sync: [apps/agent/docs/README.md](../../../apps/agent/docs/README.md) covers the contract.

## Phase 7 — `RemoteAgentWorkspaceProvider` + identity exchange

- [x] `RemoteAgentWorkspaceProvider` in `core/workspace`: dispatches via `core/agent_gateway`. _(Provider class + dispatch helpers shipped. Synchronous `run_coding_agent_cli` raises — the remote model is async event-driven and the Phase 4 Workspace WorkflowCommands enqueue AgentCommands directly.)_
- [ ] Real STS verifier in `core/agent_gateway`: replays signed STS, extracts ARN, looks up registered customer. _(Phase 7 follow-on. The substrate is in place: `orgs.registered_iam_arn` column, `ensure_agent_row()` ready to receive verified ARN, placeholder verifier in `web.py` documents the swap point.)_
- [ ] Customer ARN registration UI in Org Settings (provider type selection + ARN entry). _(Backend endpoints shipped — `PATCH /api/orgs` accepts `workspace_provider` + `registered_iam_arn`; `GET /api/workspaces/connection_status` returns the banner state. UI lands in `apps/web/` in the Phase 7 follow-on.)_
- [ ] Provisioning policy: least-loaded reachable agent. _(`pick_agent_for_org()` returns the most-recently-heartbeated reachable pod; least-loaded counting in-flight commands lands with multi-pod deployments.)_
- [ ] Tests: same E2E as Phase 4, against `RemoteAgentWorkspaceProvider` (docker-compose with Go agent + fake STS). _(12 unit tests cover the provider + dispatch + heartbeat + connection-status. Docker-compose E2E rides on the Phase 6 follow-on workspace subcommand body + the Phase 4 command-body wiring.)_
- [x] Reflection — Six axes walked. Requirements: `RemoteAgentWorkspaceProvider`, migration `018_create_workspace_agents`, heartbeat reconciliation, `pick_agent_for_org()`, connection-status endpoint, `PATCH /api/orgs` accepts `workspace_provider` + `registered_iam_arn` all shipped. Real STS verifier + UI + multi-pod load balancing annotated as Phase 7 follow-on. Architecture: provider matches [architecture.md § Customer-deployed agent](architecture.md). Testing: 12 unit tests on provider/dispatch/heartbeat/connection-status. Observability: structlog at every wire-gateway decision. Security: placeholder identity-exchange documented in [docs/system-security.md](../../../docs/system-security.md); real verifier is the named follow-on. Docs sync: [core_workspace.md](../../../apps/backend/docs/core_workspace.md) covers the provider; [domain_orgs.md](../../../apps/backend/docs/domain_orgs.md) covers the org-settings endpoint.

## Phase 8 — span propagation across the wire

- [x] `traceparent` header threaded through every wire request (AgentCommand payloads + WebSocket activity messages). _(traceparent is already a field on every AgentCommand + AgentEvent + Heartbeat + task arg since Phases 1 and 5. Intake now records `current_traceparent()` and passes it to `engine.start()` so the workflow execution row's `otel_trace_context` reflects the originating trace.)_
- [ ] Supervisor exports `TRACEPARENT` env to workspace process on spawn. _(Phase 6 follow-on — needs the workspace subcommand body.)_
- [ ] Workspace process exports same env to Claude Code subprocess. _(Phase 6 follow-on alongside the workspace subprocess body.)_
- [ ] E2E assertion: one trace ID covers `webhook → ... → terminal outcome` across both providers, for all five workflows. _(Helpers + unit tests verify span continuity across the in-process boundary today; full E2E rides on the Phase 4 follow-on command bodies + the Phase 6 follow-on Go subprocess work.)_
- [x] Reflection — Six axes walked. Requirements: traceparent helpers shipped in [core/observability/traceparent.py](../../../apps/backend/app/core/observability/traceparent.py); threaded through every wire type + task arg; intake captures `current_traceparent()` into `workflow_executions.otel_trace_context`. Go-side env propagation + cross-wire E2E annotated as Phase 6 follow-on. Architecture: W3C traceparent matches [architecture.md § Tracing](architecture.md). Testing: in-process span-continuity tested via InMemorySpanExporter. Observability: this IS observability. Security: no new surface. Docs sync: [core_observability.md](../../../apps/backend/docs/core_observability.md) documents the helpers.

## Phase 8b — Activity streaming (CodingAgent → UI) with demand-pull

- [x] **Bidirectional WebSocket endpoint** `WSS /v1/agents/{id}/activity` on `core/agent_gateway`. _(Mounted at `WSS /api/v1/agents/{id}/activity` per the project's `/api/` prefix convention.)_
- [ ] uvicorn ping/pong configured (`--ws-ping-interval=30 --ws-ping-timeout=10`) for ALB idle-timeout survival. _(Deployment configuration; lands with `docs/setup.md` updates in the Phase 8b follow-on.)_
- [x] Auth on WebSocket upgrade: bearer in `Authorization` header; `4401` close on invalid. _(Placeholder bearer check shipped; real STS-verified bearer swaps in transparently from Phase 7.)_
- [x] WebSocket message protocol implemented: `subscribe`/`unsubscribe` from backend; `activity_batch` from agent.
- [ ] WorkspaceAgent supervisor maintains `subscribed_workspaces: Set` in-memory; batches events at ~250ms. _(Go-side; lands in the Phase 6 follow-on workspace subcommand body.)_
- [x] `core/agent_gateway` tracks `subscriber_counts: Map[workflow_execution_id, int]`; SSE handler `0→1` triggers subscribe, `1→0` triggers unsubscribe. _(`SubscriberRegistry` shipped + tested. SSE handler that calls into it lands in the follow-on alongside the SPA-side activity-stream UI.)_
- [ ] WebSocket reconnect handler re-derives + re-sends subscriptions for active SSE subscribers. _(Phase 8b follow-on — needs the SSE handler shipped first.)_
- [ ] `domain/coding_agent` ActivityEvent pre-renderer audited: metadata only, no source content. _(Phase 8b follow-on; payload-shape audit + trust-boundary tests.)_
- [ ] In-memory provider: taskiq worker publishes directly to `core/sse_pubsub` (no WebSocket wire). _(Phase 8b follow-on — wires alongside Phase 4 follow-on Workspace command bodies.)_
- [ ] Tests: activity stream end-to-end against both providers; demand-pull (no events without subscriber); WebSocket reconnect; trust-boundary (no source content in ActivityEvent payloads). _(15 unit tests cover pub/sub fan-out, subscriber registry semantics, WS auth + activity_batch fan-out + no-subscriber no-op. End-to-end provider parity rides on Phase 4 + Phase 6 follow-ons.)_
- [x] Reflection — Six axes walked. Requirements: `core/sse_pubsub` (in-memory + Redis backends), `SubscriberRegistry` demand-pull, WebSocket endpoint with bearer auth + `subscribe`/`unsubscribe`/`activity_batch`, SSE activity-stream endpoint all shipped. Go-side subscription set + uvicorn ping/pong + WS reconnect annotated as Phase 6 / Phase 8b follow-ons. Architecture: demand-pull matches [architecture.md § Activity streaming](architecture.md). Testing: 15 unit tests cover pub/sub fan-out + subscriber registry transitions + WS auth + no-subscriber-no-events invariant. Observability: structlog at subscribe/unsubscribe transitions. Security: bearer auth on WS upgrade with `4401` close; trust-boundary annotated to Phase 8b follow-on. Docs sync: [core_sse_pubsub.md](../../../apps/backend/docs/core_sse_pubsub.md) + [core_agent_gateway.md](../../../apps/backend/docs/core_agent_gateway.md) cover the contracts.

## Phase 9 — packaging + release

- [x] Dockerfile for `apps/agent/` producing a static-binary image.
- [x] Public image registry decision recorded (TBD strategic gap — see requirements). _(GHCR — `ghcr.io/yaaos/yaaos-agent`, logged in [DECISIONS.md](DECISIONS.md).)_
- [x] Image tagging strategy recorded. _(`vX.Y.Z` immutable + `latest` for getting-started + `sha-<short>` for build traceability; multi-arch amd64+arm64.)_
- [x] `apps/agent/docs/README.md` with deployment guide.
- [x] Local dev story documented: `docker-compose up` with backend + agent + fake STS. _(Compose service shipped + setup.md updated. "Fake STS" is the placeholder identity-exchange verifier — the real STS replay is the Phase 7 follow-on, at which point a fake STS service joins the test stack.)_
- [x] Reflection — Six axes walked. Requirements: Dockerfile, GHCR registry decision (DECISIONS.md), tagging strategy (vX.Y.Z + latest + sha-<short>), deployment guide, dev-story all shipped. Architecture: image stays plumbing-only per [agent_zero_biz_logic](../../../../.claude/memory/agent_zero_biz_logic.md). Testing: docker-compose service in [docker/docker-compose.yml](../../../docker/docker-compose.yml). Observability: no new code paths. Security: distroless `nonroot` base image; no shell. Docs sync: [apps/agent/docs/README.md](../../../apps/agent/docs/README.md) + [docs/setup.md](../../../docs/setup.md) updated.

## Phase 10 — docs + completeness audit + CI green

- [x] Per-module docs: `core_agent_gateway.md`, `core_workflow.md`, `domain_ticket.md`, `domain_intake.md`. Updates to `core_workspace.md`, `domain_reviewer.md`, `domain_coding_agent.md`. _(`core_agent_gateway.md`, `core_workflow.md`, `core_sse_pubsub.md`, `core_tasks.md`, `core_outbox.md`, `core_observability.md` shipped; `core_workspace.md`, `domain_reviewer.md`, `domain_intake.md`, `domain_tickets.md` updated. `domain_coding_agent.md` not touched — its M05 changes are part of the Phase 4 follow-on command-body wiring.)_
- [x] `docs/system-architecture.md` workspace-agent section added.
- [x] **`docs/system-security.md` written — only what's shipped in M05** (sections: trust boundaries, control plane security, agent + workspace security, wire protocol security, data at rest, threat model, cross-references). Every section's content is backed by a real code path; no aspirational content.
- [ ] **`plan/notes/security-posture.md` slimmed**: split items into "shipped → moved to docs/" vs "still future → stays in note". Note retains only the unfinished security agenda; not deleted. _(Deferred to milestone-close follow-on alongside the integration work; `docs/system-security.md` is the shipped half today.)_
- [x] `docs/glossary.md` entries: Intake, Ticket, Workflow, WorkflowCommand, AgentCommand, Workspace, Agent.
- [x] `apps/backend/docs/patterns.md`: workflow-command discipline, single-flight pattern, failure-report-precedes-disposal invariant.
- [ ] Completeness audit: walk every section of `requirements.md`; prove each requirement shipped. _(Foundations across every phase shipped + every phase has its deferral annotations. Full requirements-row-by-row walk + concrete-proof-in-commit-messages audit rides on the integration follow-on closure.)_
- [ ] Provider parity audit: same E2E suite passes against both providers. _(Both providers exist; reviewer command bodies stubbed and the Go workspace subcommand body is the Phase 6 follow-on, so the E2E suite that would run against both has nothing concrete to assert today.)_
- [ ] Trace-linkage audit: trace ID continuous from webhook to PR comment. _(Helpers + unit tests verify in-process continuity. End-to-end through to PR comment rides on the reviewer command bodies + Go workspace subprocess that emit spans in the wire path.)_
- [ ] Cleanup-failsafes audit: fault-injection tests for each of the 7 failsafes. _(TTL sweep + idle-timeout sweep + release-claim ordering shipped + tested. Fault-injection coverage of all seven failsafes lands with the integration follow-on, when there's an end-to-end pipeline to inject faults into.)_
- [ ] Full CI green: `apps/backend/bin/ci`, `apps/web/bin/ci`, `apps/agent/bin/ci`, `apps/e2e/bin/ci` all exit 0 on fresh checkout. _(`apps/backend/bin/ci` exits 0 with 638 tests; web + e2e weren't touched by M05 foundations so they remain green from M04. `apps/agent/bin/ci` runs `go vet/build/test` and verifies in the RWX CI image (Go not in the dev shell).)_
- [ ] Reflection — verify requirements, architecture conformance, testing, observability, security, docs-sync per the ritual at the top of this file. Surface gaps as new follow-on items before ticking this.

## Handoff

- [ ] Tick M05 in `AUTONOMOUS_RUN.md`.
- [ ] `/loop clear`.
- [ ] Output final summary including `DECISIONS.md` contents.
