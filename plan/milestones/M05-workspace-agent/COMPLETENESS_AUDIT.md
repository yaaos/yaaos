# M05 Completeness audit

> Walks each promise in [requirements.md](requirements.md). For every commitment: ✅ shipped (with proof), ❌ deferred (with named owner phase / follow-on). The audit confirms no item was silently dropped.

## Scope at a glance

### "M05 ships" list — line by line

| # | Requirement | Status | Proof / owner |
|---|---|---|---|
| 1 | Two `WorkspaceProvider` impls behind same contract | ✅ | `InMemoryWorkspaceProvider` (`apps/backend/app/plugins/in_memory_workspace/`), `RemoteAgentWorkspaceProvider` (`apps/backend/app/core/workspace/remote_provider.py`) |
| 2 | Per-org config selects provider | ✅ | `orgs.workspace_provider` column (migration 019); `PATCH /api/orgs` accepts `workspace_provider` + `registered_iam_arn`; engine routes on it |
| 3 | Customer-deployed Go WorkspaceAgent (supervisor + per-workspace OS processes) | 🟡 | Supervisor + claim/heartbeat loops shipped in `apps/agent/`; per-workspace OS subprocess body deferred (Phase 6 follow-on) |
| 4 | Five-endpoint long-poll HTTPS wire + sigv4 identity exchange | 🟡 | Endpoints + AgentCommand union + AgentEvent shipped (`core/agent_gateway/web.py`); STS verifier is placeholder (Phase 7 follow-on) |
| 5 | AgentCommand kinds: CreateWorkspace, WriteFiles, RefreshWorkspaceAuth, InvokeClaudeCode, CleanupWorkspace | ✅ | `core/agent_gateway/types.py` discriminated union |
| 6 | CodingAgent isolation: path validation + read-only FS + os.RLimit | 🟡 | Path validation in `plugins/in_memory_workspace`; `os.RLimit` + read-only-FS for the Go subprocess body rides on Phase 6 follow-on |
| 7 | `core/workflow` engine on taskiq+Redis with three task bodies | ✅ | `start_step` + `handle_agent_event` + `route_workflow` in `core/workflow/service.py`. WorkflowCommand categories (Workspace/Local/HITL) implemented; three-tier retry; Tier-1 recovery; append_steps; HITL pause+resume. |
| 8 | `domain/intake` with `github_pr` type + `pr_review_v1` workflow | ✅ | `plugins/github/intake_type.py` registers the type; `domain/intake/web.py` routes `POST /api/intake/{type}`; `pr_review_v1` in `domain/reviewer/workflows/` |
| 9 | Five ticket types + five workflows — full migration to WorkflowCommands | 🟡 | 5 workflow definitions shipped (`pr_review_v1`, `incremental_review_v1`, `verify_fix_v1`, `stale_check_v1`, `answer_question_v1`). 5/5 Local command bodies real (CheckShouldReview, ArchiveStaleFindings, ResolveFinding, PostFindings, PostReply). 0/5 Workspace reviewer bodies wired to real `coding_agent.<method>` calls (substrate ready — `_WorkspaceReviewCommand` base + `build_invocation` — Phase 4 follow-on owns the wiring). |
| 10 | End-to-end flow exercised against both providers | 🟡 | `test_pr_review_v1_e2e_service.py` covers in_memory provider end-to-end with FindingRow persistence + stub VCS post. RemoteAgent E2E rides on Phase 6 Go subprocess. |
| 11 | Gen 1 → Gen 2 reviewer cutover. `review_jobs` dropped. New `reviews` table. Simplified `findings`. `queue.py` fully dismantled. | 🟡 | New `reviews` + `findings` tables populated by the workflow path; admission module owns the conversion + persist pipeline. `queue.py.schedule_review` has zero production callers (slice 30 migrated `/api/reviewer/rereview`). File deletion + `review_jobs` table drop deferred — `_run_review_job_inner` still alive for 3 legacy tests; legacy SPA endpoints still read `review_jobs`. |
| 12 | OTel tracing from webhook to PR comment | 🟡 | traceparent threaded through every wire type + task arg + intake → `workflow_executions.otel_trace_context`. In-process span continuity tested via InMemorySpanExporter. Go-side env propagation to workspace + Claude Code subprocess rides on Phase 6 + Phase 8 follow-on. |
| 13 | `docs/system-security.md` (new) | ✅ | Shipped at repo root. Sections: trust boundaries, control plane security, agent + workspace security, wire protocol security, data at rest, threat model. |
| 14 | RWX CI: separate build target for `apps/agent/` | ✅ | `apps/agent/bin/ci` runs `go vet/build/test`; verifies in RWX (Go not on local dev shell — expected per deployment guide). |
| 15 | OTel SDK wired (no exporter yet) | ✅ | `core/observability.configure()` installs TracerProvider + W3C TraceContext propagator + FastAPI/SQLAlchemy instrumentation + structlog trace_id processor. No exporter wired (Datadog etc. is a single config change). |
| 16 | Phase 0a module-naming hygiene | ✅ | `domain/auth` → `domain/sessions`; `domain/byok` → `domain/orgs/byok_routes`; `plugins/in_process_workspace` → `plugins/in_memory_workspace`; no-collision rule in `apps/backend/docs/modularity.md`. |

### "M05 does not ship" list

All correctly excluded. None of these were silently added:

- Workspace migration between agents ❌ correctly not shipped
- Other CodingAgent invokers (InvokeCodex etc.) ❌ correctly not shipped
- Per-process sandbox beyond the three M05 mechanisms ❌ correctly not shipped
- Git worktree cache ❌ correctly not shipped
- Workspace reuse across executions ❌ correctly not shipped
- HITL workflows (engine supports, M05 workflows are linear) ✅ engine supports; no HITL workflow shipped (as agreed)
- Ticket-level retry policies ❌ correctly not shipped
- Per-org concurrency caps ❌ correctly not shipped
- Customer-facing metrics dashboards ❌ correctly not shipped
- Customer-hosted MCP proxy variant ❌ correctly not shipped
- Workflow-engine swap point ❌ correctly not shipped (`core/tasks` is the only consumer)

## Locked decisions

### Language, deployment, packaging

| Decision | Status |
|---|---|
| Go for the agent | ✅ shipped (`apps/agent/`) |
| Public Docker image | ✅ Dockerfile + GHCR tagging decision logged in [DECISIONS.md](DECISIONS.md) |
| Monorepo location: `apps/agent/` | ✅ |
| OpenAPI contract; Pydantic codegen backend, oapi-codegen agent | 🟡 hand-written OpenAPI spec shipped at `apps/backend/openapi/agent-api.yaml`; codegen automation deferred (Phase 5 follow-on per annotation) |

### Backend module map

All 7 modules shipped and accounted for:

| Module | Status | Proof |
|---|---|---|
| `core/agent_gateway` | ✅ new | `core/agent_gateway/` |
| `core/workspace` | ✅ extended | New: `dispatch.py`, `remote_provider.py`, `workflow_context.py`, `commands.py` |
| `core/workflow` | ✅ new | `core/workflow/service.py` + types |
| `core/tasks` | ✅ new | `core/tasks/` — wraps taskiq with outbox-atomic enqueue |
| `core/outbox` | ✅ new | `core/outbox/` — drain worker shipped |
| `core/sse_pubsub` | ✅ new | In-memory + Redis backends |
| `domain/tickets` | ✅ extended | `type`, `payload`, `idempotency_key`, `current_workflow_execution_id` columns; `tickets.create(type, payload, idempotency_key)` + `get_workspace_ticket_context()` |
| `domain/intake` | ✅ extended | `POST /api/intake/{type}` + `IntakeType` registry |
| `domain/coding_agent` | ✅ extended | `build_invocation` shipped; per-mode body wiring pending Workspace command bodies (Phase 4 follow-on) |
| `domain/reviewer` | 🟡 evolves | `domain/reviewer/admission.py` shipped (extraction complete); 5/5 Local bodies real; Workspace bodies substrate-only; `queue.py` file still alive (annotated deferral) |

### Concepts

| Decision | Status |
|---|---|
| Entity model: Intake → Ticket → WorkflowExecution → WorkflowCommand → AgentCommand → Workspace | ✅ |
| Two command layers (WorkflowCommand engine-level / AgentCommand wire) | ✅ |
| Workflows as typed data (`domain/reviewer/workflows/`) | ✅ 5 workflow definitions; versioned |
| Workflow engine = taskiq+Redis as scheduler, engine owns state machine, async event-driven | ✅ Three-task split (`start_step` / `handle_agent_event` / `route_workflow`); workers don't block |
| Three-tier retry | ✅ Tier-1 recovery insertion (slice 7); Tier-2 step retry; Tier-3 transition fallback |
| Three distinct liveness signals (Agent / Workspace / AgentCommand) | ✅ All three exist and are never conflated |
| Three OTel span layers | 🟡 traceparent threaded in-process; cross-wire span emission for Workflow → Step → AgentCommand rides on Phase 6 Go OTel SDK follow-on |

### Agent

| Decision | Status |
|---|---|
| Zero biz logic | ✅ All policy comes from control plane payloads |
| OS-process isolation per workspace | 🟡 Supervisor shipped; per-workspace subprocess body rides on Phase 6 follow-on |

### Workspaces

| Decision | Status |
|---|---|
| Bound to agent for life; TTL ≤ 1h | ✅ TTL enforced by reaper |
| Bound to one workflow execution | ✅ `workspaces.current_holder_workflow_id` column |
| Disposable with recovery-first policy | ✅ `register_recovery_policy(auth_expired → RefreshWorkspaceAuth)`; engine inserts recovery before retry (slice 7) |
| Single-flight per workspace (control plane) | ✅ `try_claim()` atomic UPDATE in `core/workspace/dispatch.py` |
| Single-flight (agent side) | 🟡 supervisor's claim loop enforces; per-workspace command pipe rides on Phase 6 subprocess body |
| Failure report precedes disposal | ✅ `release_claim` preserves `current_holder_workflow_id` |

### Protocol

| Decision | Status |
|---|---|
| Long-poll HTTPS, single egress | ✅ |
| sigv4 identity exchange | 🟡 placeholder in `agent_gateway/web.py`; real STS verifier is Phase 7 follow-on |
| Five endpoints, four AgentCommand kinds | ✅ (5 AgentCommand kinds actually — CreateWorkspace, WriteFiles, RefreshWorkspaceAuth, InvokeClaudeCode, CleanupWorkspace) |
| `traceparent` on every AgentCommand + AgentEvent | ✅ |

### Trust boundary

| Decision | Status |
|---|---|
| Source code never leaves customer VPC | ✅ enforced by architecture: in_memory provider in-process; remote_agent provider dispatches over wire with metadata-only payloads |
| Only findings + telemetry + spans cross | ✅ |
| Workspace processes have no control-plane credentials | 🟡 confirmed in the architectural design; full implementation rides on Phase 6 subprocess body |

### Provider contract is uniform

| Decision | Status |
|---|---|
| Same protocol + invariants | ✅ Both providers implement `WorkspaceProvider`; single-flight enforced uniformly |
| In-memory never deleted | ✅ Still registered as plugin |

## Decisions locked (the second locked-decisions section)

| Decision | Status |
|---|---|
| Task queue: taskiq + Redis, wrapped by `core/tasks` + outbox | ✅ |
| Redis as infrastructure (real container, no mocking) | ✅ |
| WebSocket for ActivityEvent streaming | ✅ `WSS /api/v1/agents/{id}/activity` |
| Session management pattern (required session, no-commit) | ✅ Phase 0 swept all transactional services + semgrep rule enforces |
| Workspace provisioning fresh per ticket | ✅ |
| Workspace TTL ceiling 1h | ✅ |
| `RefreshWorkspaceAuth` kept | ✅ Real body shipped (slice 6) |
| Single-flight: dual enforcement | ✅ control-plane side; agent-side rides on Phase 6 |
| Module dependency: `domain/reviewer` depends on `core/workflow` | ✅ tach.toml enforces |
| Intake registry internal to `domain/intake` | ✅ |
| AgentCommand vs WorkflowCommand naming | ✅ |
| Per-AgentCommand restart safety | ✅ |

## Strategic gaps (all 4 resolved)

| Gap | Resolution status |
|---|---|
| 1. Image + protocol versioning | ✅ Locked in architecture; `/v1` namespace, GHCR registry, vX.Y.Z + latest + sha tagging — per DECISIONS.md |
| 2. Multi-tenancy + fairness | ✅ Resolved by architecture (async event-driven workflow model — workers don't block on AgentCommands) |
| 3. Customer observability + audit | ✅ Existing audit log UI + ticket UI extend; structured Go stdout logs captured by customer's ECS |
| 4. MCP proxy interaction details | ✅ Per-workflow_execution_id bearer; yaaos-hosted; no mid-workflow refresh |

## Customer onboarding (locked)

The 8-step flow:

| Step | Status |
|---|---|
| 1-2. Owner navigates to Org Settings → Workspaces | 🟡 backend ready; SPA UI rides on Phase 7 follow-on |
| 3-4. In-memory choice | ✅ default; `PATCH /api/orgs` accepts the setting |
| 5. Remote choice + ARN entry form | 🟡 backend endpoint shipped; UI rides on Phase 7 follow-on |
| 6. Customer SRE ECS setup | ✅ docs in `apps/agent/docs/README.md` |
| 7. Connection status panel polling | 🟡 `GET /api/workspaces/connection_status` shipped; UI rides on Phase 7 follow-on |
| 8. PR webhooks route through WorkspaceAgent | ✅ engine routes on `workspace_provider` |

## Honest M05 readiness call

**Shipped: substrate for the architectural vision + the in_memory provider's complete end-to-end pipeline.** Backend tests (730 passing) prove the new path works through admission, GitHub posting, and cleanup. Phase 4 reflection ticked with all six axes walked.

**Deferred (with explicit owner phase / slice):**
- ~~5 Workspace reviewer command bodies' real `coding_agent` invocation~~ — **shipped (Phase 4 follow-on slice 33).** Each of `CodeReview`, `IncrementalReview`, `VerifyFix`, `StaleCheck`, `AnswerQuestion` now invokes the matching `coding_agent.<method>` against the resolved workspace; `testing/fake_coding_agent` provides a standalone `FakeCodingAgentPlugin` for service tests that register a coding agent on the fly.
- `queue.py` file deletion + `review_jobs` table drop (annotated; needs legacy-test migration + legacy-SPA-endpoint rewire)
- Phase 6 Go workspace subprocess body + OTel SDK + secret redaction
- Phase 7 STS verifier + Org Settings UI + provisioning policy
- Phase 8 Go-side traceparent env propagation
- Phase 8b SPA-side activity-stream consumer + WS reconnect + uvicorn ping/pong
- Phase 10 remaining audits (security-posture slim) — ~~cleanup failsafes~~ **shipped (slices 34+35)**: ten fault-injection tests in `app/core/workspace/test/test_reaper_failsafes.py` covering destroy-retry + idle-timeout + close_workspace idempotency + startup_recovery. ~~provider parity~~ **shipped (slice 36)**: `pr_review_v1` walks to DONE under both `in_memory` and `remote_agent` provider routings; the remote-agent test simulates each Workspace-step terminal AgentEvent via `_advance_pending_agent_event`. ~~trace linkage~~ **shipped (slice 37)**: all three workflow task bodies (start_step, route_workflow, handle_agent_event) wrap in `with_remote_parent_span`; `test_trace_linkage.py` asserts every emitted span shares the upstream trace_id across the worker boundary.

**Bottom line**: per the [Definition-of-done](START_HERE.md), M05 is not closed — 31 unchecked items remain in PHASES.md, each carrying a deferral annotation that names its owning follow-on. This audit catalogs each one and confirms no requirement was silently dropped.
