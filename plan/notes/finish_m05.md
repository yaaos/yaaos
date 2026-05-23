# Finish M05

Captured 2026-05-23. M05's [PHASES.md](../milestones/M05-workspace-agent/PHASES.md) is fully `[x]`'d, but several boxes are checked-with-deferral-annotations (`_(deferred — Phase N follow-on)_` inline). This note collects the real outstanding work, ordered by impact, so we can plan a proper close-out pass.

## Big ones — customer-blocking

### 1. Go agent's workspace subprocess body

**Status:** Phase 6 follow-on, never landed.

Today the Go supervisor stub-emits `completed_success` instead of executing AgentCommands. Per [PHASES.md:147](../milestones/M05-workspace-agent/PHASES.md#L147):

> Command routing emits a stubbed `completed_success` so the backend workflow advances end-to-end; real OS-process spawning + disk janitor land in the Phase 6 follow-on iteration.

Concretely missing:
- **`InvokeClaudeCode` subprocess body** — the workspace subcommand has the IPC scaffolding (slice 62) and shells out for git clone (slice 69) and cleanup (slice 65), but the actual Claude Code invocation inside the workspace process is not wired.
- **Disk janitor** — orphan workspace dirs accumulate in `/var/agent/workspaces/`; no sweeper.
- **Backend-side `forgotten_workspaces` reclaim** — supervisor reports orphans on first heartbeat (slice 71) but the backend doesn't act on the list to RemoveAll on disk.

**Impact:** A real customer's `apps/agent` container wouldn't actually run Claude Code. Service tests pass because they inject synthetic AgentEvents; the wire shape is right, the executor is empty.

**Size:** Multi-day. Most of what's left of the Go agent.

---

### 2. Real STS verifier wired into `/identity/exchange`

**Status:** Phase 7 follow-on. Code exists, not called.

[`core/agent_gateway/sts_verifier.py`](../../apps/backend/app/core/agent_gateway/sts_verifier.py) implements the Vault AWS-auth replay pattern with 13 passing tests. It's just not plugged into the endpoint. `/identity/exchange` still uses the placeholder verifier ("any non-empty bearer passes"), per [PHASES.md:181](../milestones/M05-workspace-agent/PHASES.md#L181).

What landing it means:
- `/identity/exchange` calls `replay_caller_identity` on the agent's signed STS payload.
- Extracts ARN, looks up `orgs.registered_iam_arn`, rejects mismatches with 401.
- Adds the ARN-mismatch service test.

**Impact:** Security blocker for prod. Today anyone can claim to be any agent.

**Size:** Half a day. The verifier code is done.

---

### 3. SPA activity-stream UI + SSE-to-SubscriberRegistry handler

**Status:** Phase 8b follow-on.

Backend pieces shipped: WebSocket endpoint, pub/sub, `SubscriberRegistry`, reconnect replay. The SSE handler that should call `register_subscriber`/`unregister_subscriber` to drive demand-pull isn't wired. Per [PHASES.md:184](../milestones/M05-workspace-agent/PHASES.md#L184):

> `SubscriberRegistry` shipped + tested. SSE handler that calls into it lands in the follow-on alongside the SPA-side activity-stream UI.

Concretely missing:
- The per-workflow SSE endpoint (`GET /api/workflows/{id}/activity`) that bridges `core/sse_pubsub` → SSE.
- The SPA-side `EventSource` consumer that renders the activity stream.
- WebSocket reconnect handler on the Go side (slice 84 covers the backend side).

**Impact:** User-visible feature gap. Activity tab in the SPA is empty.

**Size:** ~1 day. Self-contained.

---

## Smaller ones — quality / ops

### 4. Lesson-in-prompt port to M05 `CodeReview`

The old `queue.py` path loaded org lessons into the Claude Code prompt. The M05 `CodeReview` WorkflowCommand doesn't. Documented as M06 follow-on but never picked up.

**Size:** ~2 hours.

### 5. `incremental.py` onto the engine path

Per [PHASES.md:131](../milestones/M05-workspace-agent/PHASES.md#L131):

> `incremental.py` is now self-contained ... it stays as the auto-incremental runner pending a follow-on that moves it onto the `incremental_review_v1` engine path.

**Size:** ~half a day.

### 6. Cleanup failsafes 5, 6, 7

Failsafes 1–4 covered by [`test_reaper_failsafes.py`](../../apps/backend/app/core/workspace/test/test_reaper_failsafes.py). Per [PHASES.md:211](../milestones/M05-workspace-agent/PHASES.md#L211):

> Failsafes 5 (disk sweep), 6 (agent-loss recovery), 7 (audit-trail audit row per transition) are Go-side / cross-system and land alongside the Phase 6 follow-on workspace subprocess body.

- **5 — Disk sweep:** Go-side. Periodic `os.ReadDir(/var/agent/workspaces/)`, compare to known set, RemoveAll the diff.
- **6 — Agent-loss recovery:** Cross-system. When `workspace_agents.last_heartbeat_at` is stale beyond threshold, mark all workspaces held by that agent as EXPIRED so the engine can route cleanup elsewhere.
- **7 — Audit row per workspace transition:** Today some transitions emit audit rows, not all. Walk the state machine, ensure every transition has an `audit_log` write.

Depends on #1 being real first.

**Size:** ~1 day combined.

### 7. Trace propagation across the Go subprocess hop

TRACEPARENT env passing is wired (slice 64 backend→supervisor, slice 73 workspace→Claude Code). Only matters once #1 lands; the chain is incomplete without a real subprocess body in between.

**Size:** Already done in code; just needs #1 to validate.

---

## Defer-or-cut — keep parked unless we hit them

### 8. OpenAPI codegen automation

Drift-detection in CI (slices 66+67) walks the spec against Python types + Go types. Catches schema/code mismatches before they ship. Full codegen would be ergonomics, not correctness.

**Owner:** Post-M05 dev-ergonomics backlog. Defensible to never do.

### 9. Async-model load test

100 simultaneous workflows dispatching in <1s wall time. Service-tier tests prove the dispatch SHAPE (start_step returns after enqueue, not blocking on AgentCommand). A load test would prove the throughput NUMBERS.

**Owner:** Post-M05 perf backlog. Run before promoting to prod traffic.

### 10. Full docker-compose E2E with Go agent + fake STS

Service-tier `test_pr_review_v1_runs_end_to_end_remote_agent` covers the workflow-shape parity. This would add wire-shape regression coverage against the actual Go binary.

**Owner:** Post-M05 integration backlog. Valuable if customer-side bugs expose drift the service tests can't catch.

### 11. Go fake-backend integration test

Single-process integration covering CreateWorkspace → InvokeClaudeCode → CleanupWorkspace through a fake backend. Per-component tests cover each link today (19 Go test files). A combined test would re-cover the same paths.

**Owner:** Post-M05 backlog. Defensible to skip.

---

## Suggested close-out order

1. **Item 2 (STS verifier wiring)** — half a day, security-relevant, code already exists. Easy win.
2. **Item 3 (SPA activity UI + SSE handler)** — user-visible, self-contained, ~1 day.
3. **Item 4 (lesson-in-prompt)** — ~2 hours, restores M01 behavior parity.
4. **Item 5 (incremental.py onto engine)** — ~half day, removes the last legacy-runner module.
5. **Item 1 (Go subprocess body)** — multi-day, biggest commitment. Could split into: real `InvokeClaudeCode` first (unblocks the rest), then disk janitor + reclaim.
6. **Items 6, 7** — ride on Item 1.

Items 8–11 stay parked unless they bite.

## Cross-references

- [plan/milestones/M05-workspace-agent/PHASES.md](../milestones/M05-workspace-agent/PHASES.md) — the original ledger with inline deferral annotations.
- [plan/milestones/M05-workspace-agent/COMPLETENESS_AUDIT.md](../milestones/M05-workspace-agent/COMPLETENESS_AUDIT.md) — walks every requirements.md row with concrete proof.
- [plan/milestones/M05-workspace-agent/DECISIONS.md](../milestones/M05-workspace-agent/DECISIONS.md) — locked decisions still in force.
