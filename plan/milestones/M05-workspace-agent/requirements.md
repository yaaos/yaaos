# M05 Requirements — Workspace Agent

> What ships in M05 and what's deliberately cut. Read [README.md](README.md) first; see [architecture.md](architecture.md) for module-level detail and [implementation-plan.md](implementation-plan.md) for phasing.

## Why this milestone

yaaos needs to act on customer source code without that code crossing yaaos infrastructure. The workspace agent is the customer-deployed worker that holds the code, runs coding agents (Claude Code etc.) against it, and reports only findings + agent telemetry back. It is the realization of the trust boundary the architecture has assumed since the project began.

This milestone delivers:

- The Go agent (`apps/agent/`).
- New control-plane modules (`core/agent_gateway`, `core/workflow`, `core/tasks`, `core/outbox`, `core/sse_pubsub`). Existing modules extended: `domain/tickets`, `domain/intake`, `core/workspace`, `domain/coding_agent`, `domain/reviewer`.
- Extensions to existing modules (`core/workspace`, `domain/reviewer`, `domain/coding_agent`).
- A formalized Workflow + WorkflowCommand model that generalizes today's `review_job` pattern into a substrate that supports future workflows (investigation, planning, implementation, HITL).

## Scope at a glance

**M05 ships:**

- Two `WorkspaceProvider` implementations behind the same contract: `InMemoryWorkspaceProvider` (existing plugin, evolves; for dev/E2E/self-hosted) and `RemoteAgentWorkspaceProvider` (new; for prod multi-tenant via customer-deployed WorkspaceAgent).
- Per-org config selects the provider (`workspace_provider: in_memory | remote_agent`).
- Customer-deployed Go WorkspaceAgent (supervisor + per-workspace OS processes) running in ECS/Fargate.
- Five-endpoint long-poll HTTPS wire protocol between WorkspaceAgent and control plane, with sigv4-based identity exchange. AgentCommand kinds: `CreateWorkspace`, `WriteFiles`, `RefreshWorkspaceAuth`, `InvokeClaudeCode`, `CleanupWorkspace`. Per-CodingAgent invoker kinds (slot for future `InvokeCodex` etc.).
- CodingAgent isolation: path validation in Go + container filesystem read-only except `/var/agent/workspaces/` + `os.RLimit` per workspace process. No landlock/UID/namespaces in M05 (documented limitation).
- `core/workflow` engine on top of taskiq+Redis: workflows as typed data, three WorkflowCommand categories (Workspace, Local, HITL), three-tier retry, HITL primitives, append-steps escape hatch. **Three-task async event-driven model**: `start_step` (dispatches and exits), `handle_agent_event` (event-arrival → enqueues route), `route_workflow` (the WorkflowRouter pattern — decides next step).
- `domain/intake` with one intake type (`github_pr`) and one workflow definition (`pr_review_v1`).
- **Five ticket types + five workflows** — full migration of the CodingAgent Plugin Protocol's five task modes to WorkflowCommands. No legacy spawn()-based code paths for these modes survive M05.
  - `pr_review_v1`: `CheckShouldReview → ProvisionWorkspace → CodeReview → PostFindings → CleanupWorkspace`
  - `incremental_review_v1`: `CheckShouldReview → ProvisionWorkspace → IncrementalReview → PostFindings → CleanupWorkspace`
  - `verify_fix_v1`: `ProvisionWorkspace → VerifyFix → ResolveFinding → CleanupWorkspace`
  - `stale_check_v1`: `ProvisionWorkspace → StaleCheck → ArchiveStaleFindings → CleanupWorkspace`
  - `answer_question_v1`: `ProvisionWorkspace → AnswerQuestion → PostReply → CleanupWorkspace`
- End-to-end flow exercised against both providers: signals → tickets → workflows → workspaces → CodingAgent → outcomes.
- **Gen 1 → Gen 2 reviewer cutover.** `review_jobs` table dropped (no backfill, no compat). New `reviews` table populated by the workflow. Simplified `findings` table with cross-review dedup via `UNIQUE (pr_id, fingerprint)`. **No FindingState enum, no comment_threads/messages/ack-decisions tables.** All five task modes ship as WorkflowCommands (CodeReview, IncrementalReview, VerifyFix, StaleCheck, AnswerQuestion) — no legacy spawn()-based reviewer code remains. `queue.py` is fully dismantled.
- OTel tracing from webhook to PR comment, propagated through wire protocol and into workspace processes.
- **`docs/system-security.md`** (new) — describes the security posture **as it ships in M05**, nothing aspirational. Source: `plan/notes/security-posture.md`, slimmed during M05 closure to retain only the unfinished security agenda (not deleted).
- **RWX CI:** separate build target for `apps/agent/` (Go binary + Docker image) independent of `apps/backend/bin/ci`.
- **OTel SDK wired (no exporter yet).** Backend + Go agent both set up SDK + W3C trace context propagator. Spans created and discarded (no `SpanExporter`). traceparent flows across the wire; structlog correlates trace_id onto every log record. Adding an exporter later (Datadog etc.) is a single config change.
- **Phase 0a module-naming hygiene:** rename `domain/auth` → `domain/sessions`; merge `domain/byok` into `domain/orgs/byok_routes`; rename `plugins/in_process_workspace` → `plugins/in_memory_workspace`; document the no-collision rule in `apps/backend/docs/modularity.md`.

**M05 does not ship:**

- Workspace migration between WorkspaceAgents (workspaces are bound to their WorkspaceAgent for life).
- Other CodingAgent invokers (`InvokeCodex`, `InvokeAider`, etc.). Slot in the protocol; only `InvokeClaudeCode` implemented.
- **(Was deferred; now in M05 scope per locked decision.)** All five reviewer task modes (`CodeReview`, `IncrementalReview`, `VerifyFix`, `StaleCheck`, `AnswerQuestion`) ship as WorkflowCommands. No legacy spawn()-based reviewer code survives M05.
- Finding state machine (`FindingState`, `Acknowledgment`, `comment_threads`, `comment_messages`, etc.). Cross-review dedup is the only Gen 2 capability we keep.
- Workflow engine swap (taskiq + Redis is the choice; portable design preserved — `core/tasks` is the only module importing taskiq, so a swap is contained).
- Multi-VCS (GitHub only) support.
- Per-process sandbox hardening beyond the three M05 mechanisms (no landlock / seccomp / per-workspace UID / network namespaces).
- Git worktree cache (single shallow clone per workspace; reuse + caching deferred).
- Workspace reuse across workflow executions (single-use; relaxation is post-M05, schema is add-only-ready).
- HITL workflows (engine supports them; M05's five workflows are all linear — no HITL steps).
- Ticket-level retry policies (workflow-level retry covers M05 needs).
- Per-org concurrency caps / weighted scheduling / paid-tier differentiation. (Multi-tenancy resolved by async event-driven workflow model — workers don't block — but explicit per-org policies are post-M05.)
- Customer-facing metrics dashboards, log centralization, SIEM webhooks. (Customer-side observability resolved by existing audit log + customer ECS log capture; no new yaaos-side surface in M05.)
- Customer-hosted MCP proxy variant. (M05 ships yaaos-hosted MCP proxy only; customer-hosted is a future option.)
- Workflow-engine swap point. (taskiq + Redis is the choice; `core/tasks` wraps it as the only consumer.)

## Locked decisions

These shape the design; rationale in [architecture.md](architecture.md).

### Language, deployment, packaging

- **Go** for the agent. Single static binary, ~15MB container.
- **Public Docker image.** Customer pulls and runs in ECS/Fargate. No customer-side build/install.
- **Monorepo location:** `apps/agent/`.
- **API contract:** hand-written OpenAPI in `apps/backend/openapi/agent-api.yaml`. Pydantic codegen for backend; oapi-codegen for agent. Both regenerate in CI.

### Backend module map

| Module | Layer | Status | Responsibility |
|---|---|---|---|
| `core/agent_gateway` | core | new | Wire protocol (HTTPS + WebSocket). Only module that talks to remote WorkspaceAgents. |
| `core/workspace` | core | extended | Workspace lifecycle, provider abstraction, recovery policy. Owns workspace-lifecycle WorkflowCommands. |
| `core/workflow` | core | new | Engine mechanics only. Workflow data structure, WorkflowCommand interface, span propagation. Uses `core/tasks` for task scheduling. |
| `core/tasks` | core | new | Thin abstraction wrapping taskiq + Redis broker. Provides `@task` decorator, `enqueue(session=...)` (routes through `core/outbox` for DB atomicity), `TaskContext`, worker entrypoint. Hides taskiq imports from biz logic. |
| `core/outbox` | core | new | DB-atomic enqueue mechanism. `outbox_entries` table; `outbox.write(session, ...)` API; outbox drain worker that pushes ready entries to Redis. |
| `core/sse_pubsub` | core | new | Thin wrapper around Redis pub/sub. Used by `core/agent_gateway` to publish ActivityEvents; used by SSE handlers in `web.py` to subscribe per `workflow_execution_id`. |
| `domain/tickets` | domain | **existing, extended** | Already has ticket aggregate + state machine (`in_review|complete|abandoned`) + HTTP routes. M05 adds: `type` column, state-machine reconciliation with WorkflowExecution states, `idempotency_key` column, `current_workflow_execution_id` FK, `create(type, payload, idempotency_key)` method. |
| `domain/intake` | domain | **existing, extended** | Already routes VCS events + filters + dedup + PR metadata sync. M05 adds: post-routing `domain/tickets.create()` call + `core/workflow.start(workflow_name, ticket_id)`. **Does NOT own workflow definitions** — those live with their owning domain module. |
| `domain/coding_agent` | domain | existing, evolves | Shared Claude Code invocation machinery + cross-task prompt fragments. ActivityEvent pre-rendering layer enforces "metadata only — never source content" invariant. |
| `domain/reviewer` | domain | existing, evolves | `CheckShouldReview` + `CodeReview` + `PostFindings` WorkflowCommands; review-specific finding interpretation. Owns `domain/reviewer/admission.py` (the admission pipeline: schema gate, off-diff drop, severity threshold, nit cap, fingerprint dedup, cross-file dedup, top-10 cap — pure function called by `CodeReview`). Owns `domain/reviewer/workflows/pr_review.py` (the `pr_review_v1` workflow definition). Registers with `core/workflow` at startup. |

### Concepts

- **Entity model.** Intake → Ticket → Workflow Execution → WorkflowCommand → AgentCommand → Workspace. Agent represents the host (no Instance entity).
- **Two command layers, deliberately distinct.** `WorkflowCommand` (engine-level, three categories: Workspace / Local / HITL) and `AgentCommand` (wire-protocol, four kinds).
- **Workflows are typed data structures** stored at `domain/reviewer/workflows/` (for review-family) or under their owning domain module. Versioned (`pr_review_v1`, `incremental_review_v1`, etc.).
- **Workflow engine = taskiq+Redis as just a task scheduler.** Engine owns the state machine; taskiq runs one short-lived task at a time. Async event-driven model (workers don't block on AgentCommand completion).
- **Three-tier retry separation.** AgentCommand recovery → WorkflowCommand step retry → workflow-level transition.
- **Three distinct liveness signals.** Agent / Workspace / AgentCommand. Never conflated.
- **Three OTel span layers.** Workflow execution → step → AgentCommand. `traceparent` propagates across the wire and into workspace processes.

### Agent

- **Zero biz logic.** Every threshold, prompt, lesson, depth, timeout supplied by control plane.
- **OS-process isolation per workspace.** Supervisor spawns one OS process per workspace; IPC over stdin/stdout pipes (JSON-newline).

### Workspaces

- **Bound to their agent for life** (no migration in POC). TTL ≤ 1h (matches GitHub App installation-token lifetime).
- **Bound to exactly one workflow execution for M05.** Schema (`current_holder_workflow_id` nullable column) keeps future reuse relaxation add-only.
- **Disposable with recovery-first policy** — control plane tries known fixes (e.g. `RefreshWorkspaceAuth`) before dispose-and-replace.
- **Single-flight per workspace** — enforced in control plane (atomic claim on `current_command_id`) AND in agent (one command pipe per workspace process).
- **Failure report precedes disposal** — invariant.

### Protocol

- **Long-poll HTTPS, single egress, sigv4-based identity exchange** (Vault AWS auth pattern).
- **Five endpoints, four AgentCommand kinds** — see [architecture.md § Protocol shape](architecture.md#protocol-shape).
- **`traceparent` on every AgentCommand and AgentEvent.**

### Trust boundary

- Source code never leaves customer VPC.
- Only findings + structured supervisor telemetry + OTel spans cross.
- Workspace processes have no yaaos control plane credentials.

### Provider contract is uniform

- `InMemoryWorkspaceProvider` and `RemoteAgentWorkspaceProvider` implement the same protocol and enforce the same invariants (single-flight, recovery, lifecycle, failure-report-precedes-disposal).
- In-memory is never deleted — too useful for E2E tests. In prod it gets disabled at org-settings allowlist level eventually.

## Decisions locked (resolving open questions from earlier discussion)

- **Task queue choice:** taskiq + Redis broker. Wrapped by `core/tasks`; atomic-in-session enqueue via the outbox pattern in `core/outbox`. Workers run as a separate process (same Docker image, different entrypoint).
- **Redis added as infrastructure.** Real Redis docker container in local dev, testing, CI — same pattern as the existing Postgres container. No in-memory mocking. ECS task config for prod (ElastiCache or self-managed). Two uses: taskiq broker + SSE pub/sub for ActivityEvent streaming.
- **WebSocket for ActivityEvent streaming (`/v1/agents/{id}/activity`).** Separate from the HTTP long-poll command-and-control channel. Activity events are metadata-only — the CodingAgent pre-rendering layer enforces no source content crosses the trust boundary.
- **Session management pattern (project-wide, adopted in M05 Phase 0):** transactional service functions take a **required** `session: AsyncSession` parameter and never commit. Orchestrators (endpoint handlers, task bodies) own the session boundary — open it, call services, commit. Existing code (notably `core/audit_log`'s optional-session pattern) is refactored to match. Documented in `apps/backend/docs/patterns.md`.
- **Workspace provisioning:** fresh per ticket (no reuse). Worktree caching deferred.
- **Workspace TTL ceiling:** 1h.
- **`RefreshWorkspaceAuth`** kept (used for in-flight recovery and pre-emptive refresh).
- **Single-flight enforcement:** dual — control plane via `current_command_id` row lock + agent via per-workspace command pipe.
- **Module dependency direction:** `domain/reviewer` depends on `core/workflow` (not the other way). Workflow definitions live in `domain/reviewer/workflows/` (the domain module that owns its WorkflowCommands). `domain/intake` routes intake signals to the right ticket type + workflow name but doesn't host workflow definitions.
- **Intake registry shape:** internal to `domain/intake`, not pluggable from other modules.
- **AgentCommand vs WorkflowCommand naming:** locked. Disambiguated by layer noun.
- **Per-AgentCommand restart safety:** all four current kinds are restart-safe at the user-visible level (table in [architecture.md](architecture.md)).

## Strategic gaps — all resolved in this milestone

The four strategic gaps originally deferred from the design round have all been resolved by the audit walkthrough. See [architecture.md § Open questions — strategic gaps](architecture.md#open-questions--strategic-gaps) for the locked outcomes:

1. **Image + protocol versioning.** ✅ Locked. ECR Public Gallery. `1.x ↔ /v1` locked relationship. `/v1` AgentCommand set frozen at M05's set. Optional-field-additions only within `/v1`. No capabilities; no minimum-version floor; no force-upgrades. New CodingAgent kinds (Codex etc.) require `/v2`. Major versions coexist indefinitely. See [architecture.md § Image + protocol versioning](architecture.md#image--protocol-versioning-locked).
2. **Multi-tenancy + fairness.** ✅ Resolved by architecture, not by policy. No per-org caps in M05 (premature for POC). The async event-driven workflow model (`start_step` + `handle_agent_event` + `route_workflow`) means workers never block on AgentCommand completion — 1-2 worker instances can support tens of thousands of in-flight workflows since most sit in `awaiting_agent` with zero worker cost. Hot customers can't realistically saturate the worker pool with workflow-router work. Per-org caps drop in as an additive feature later if needed.
3. **Customer-side observability + audit.** ✅ Resolved with no new M05 deliverables. Coverage comes from pre-existing infrastructure: (a) yaaos's existing audit log UI (from M02) renders new M05 audit kinds automatically; (b) the existing ticket UI extends to show workflow execution state; (c) WorkspaceAgent emits structured stdout logs that customer's ECS captures into their own observability stack. No log centralization, no metrics dashboards, no bulk export, no SIEM webhooks in M05.
4. **MCP proxy interaction details.** ✅ Locked. yaaos-hosted (M04 default, unchanged). Per-`workflow_execution_id` bearer minted by `domain/mcp_proxy.mint_token` at first `InvokeClaudeCode` dispatch; included in AgentCommand payload; workspace process writes `.mcp.json`. Token TTL = `workflow_max_wall_seconds + 1h buffer`; no mid-workflow refresh. **MCP traffic flows through yaaos in-memory but is never persisted** (only audit metadata stored, no raw args or response bodies). Customer-hosted MCP proxy is a future option. See [architecture.md § MCP proxy interaction details](architecture.md#mcp-proxy-interaction-details-locked).

## Customer onboarding (locked)

End-to-end for a new customer setting up M05 workspace agents.

**Pre-existing flow** (M01–M04, unchanged): sign up org → install yaaos GitHub App → set BYOK Anthropic key → (optional) connect Linear/Notion service accounts.

**New M05 onboarding step:**

1. Owner navigates to **Org Settings → Workspaces**.
2. Empty state. Click "Create Workspace."
3. Choose provider: **In Memory** (yaaos-hosted dev/test) or **Remote** (customer-deployed WorkspaceAgent on their ECS).
4. **In Memory:** single-click save. `org_settings.workspace_provider = 'in_memory'`. Workspaces run on yaaos infrastructure.
5. **Remote:** two-panel form.
   - **"What yaaos provides"** (read-only): public Docker image URI, yaaos API endpoint URL, ECS task definition snippet, link to full setup docs.
   - **"What you provide"** (form): customer's IAM role ARN. Optional friendly name, optional AWS region (informational).
   - Save → sets `org_settings.workspace_provider = 'remote_agent'` and `org_settings.registered_iam_arn`.
6. Customer SRE follows setup docs in their AWS account: creates IAM role (`ecs-tasks.amazonaws.com` trust + Secrets Manager read), creates ECS task definition from the snippet, deploys ECS service.
7. **Connection status panel** on the Workspaces page polls every ~3s and shows:
   - 🔴 **Not connected** — no pods have connected yet.
   - 🟡 **Connection lost** — pods previously connected, no heartbeat in > 90s.
   - 🟢 **Connected** — at least one pod heartbeating. Detail line: "1 pod connected. Last heartbeat: 12s ago."
   No button — passive status indicator that updates live.
8. PR webhooks now route through this WorkspaceAgent.

**Identity auth:** sigv4 STS GetCallerIdentity (Vault AWS auth pattern). Customer's IAM role trusts `ecs-tasks.amazonaws.com` only — no yaaos-specific trust policy. Yaaos validates incoming agents by replaying the signed STS call.

**Setup automation:** manual docs only in M05 (no Terraform module, no CLI, no wizard). Terraform module is a future nice-to-have.

**Horizontal scaling = multi-pod, not multi-config.** Customer's ECS service runs with `desired_count = N` — same IAM role, N identical pods. Each pod generates its own `agent_pod_id` at startup, does identity exchange independently, becomes its own `workspace_agents` row. Backend's provisioning policy picks least-loaded reachable pod when creating a new workspace. **No multi-config UX in M05.** Customer Owner registers one ARN; ECS handles the scaling.

**Reset workspace setup action:** Owner-only. Clears `registered_iam_arn` + sets provider back to empty. Existing pod connections become orphaned (workspace_agents rows go unreachable via heartbeat-loss path).

**New audit kinds** (in addition to those locked earlier): `workspace.configured` (Owner set provider), `workspace.reset` (Owner cleared config). Per-pod connection events use the existing `workspace_agent.connected` / `workspace_agent.lost` kinds.

## Source

Strategic discussion captured iteratively. `plan/notes/security-posture.md` is reconciled with this milestone — at M05 closure it gets split (shipped items move into `docs/system-security.md`; remaining future items stay in the note). The note is not deleted — it captures the unfinished security agenda for future milestones.
