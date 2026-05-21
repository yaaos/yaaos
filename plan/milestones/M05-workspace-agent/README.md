# M05 — Workspace Agent

> Customer-deployed worker that hosts isolated workspaces and runs coding agents (Claude Code today; others later) against customer code, on customer infrastructure. Plus a generalized Workflow + WorkflowCommand model in the control plane that subsumes today's `review_job` and supports future investigation / planning / implementation / HITL workflows.

## Status

`[planned]` — design complete; ready for autonomous execution. All twelve audit topics resolved (workflow model, reviewer cutover, cancellation, session refactor, admission placement, workspace protocol, streaming, versioning, multi-tenancy, observability, MCP proxy, onboarding). Phase decomposition locked in [PHASES.md](PHASES.md).

## Reading order

1. [requirements.md](requirements.md) — what M05 ships and what's cut. Scope + locked decisions + non-goals + open questions.
2. [architecture.md](architecture.md) — module layout, data model, lifecycles, protocol, the Workflow + WorkflowCommand model, workflow execution model, trust boundary, tracing.
3. [implementation-plan.md](implementation-plan.md) — phased build order.

## Autonomous execution

Once the strategic gaps are resolved and PHASES.md is fleshed out, autonomous execution follows the same shape as prior milestones:

- [START_HERE.md](START_HERE.md) — ritual, decision protocol, definition of done.
- [PHASES.md](PHASES.md) — ledger; checkboxes are source of truth.
- [DECISIONS.md](DECISIONS.md) — append-only log of low-certainty calls.

## Scope at a glance

- **Five new core modules:** `core/agent_gateway` (wire protocol), `core/workflow` (engine), `core/tasks` (taskiq+Redis wrapper), `core/outbox` (atomic DB-write + enqueue), `core/sse_pubsub` (Redis pub/sub for activity streaming).
- **Two domain modules extended (existing):** `domain/tickets`, `domain/intake`.
- **Existing modules evolve:** `core/workspace`, `core/audit_log`, `domain/coding_agent`, `domain/reviewer`.
- **Project-wide pattern shift:** transactional service functions take a required `session: AsyncSession` parameter and never commit. Refactor of existing code lands as Phase 0; documented in `apps/backend/docs/patterns.md`.
- **taskiq + Redis** as the task queue, wrapped by `core/tasks` so the rest of the codebase doesn't import taskiq directly. Atomic DB-write + enqueue via `core/outbox` (outbox pattern). Workers run as a separate process (same Docker image, different entrypoint). Redis also serves `core/sse_pubsub` for activity streaming.
- **Two provider implementations behind one contract:** `InMemoryWorkspaceProvider` and `RemoteAgentWorkspaceProvider`. Per-org configurable.
- **One end-to-end flow:** GitHub webhook → ticket → workflow → workspace → Claude Code → findings posted on PR. Same E2E suite runs against both providers.
- **OTel tracing** end-to-end, propagated through the wire protocol and into workspace processes.
- **New `docs/system-security.md`** describing the M05 security posture as shipped.

## What's locked

See [requirements.md § Locked decisions](requirements.md#locked-decisions). Short version:

- **Language:** Go for the agent. **Deployment:** public Docker image; customer runs in ECS/Fargate.
- **Entity model:** Intake → Ticket → Workflow Execution → WorkflowCommand → AgentCommand → Workspace.
- **Two command layers** (`WorkflowCommand`, `AgentCommand`) — deliberately distinct.
- **Three-tier retry separation:** AgentCommand recovery → WorkflowCommand step retry → workflow transition.
- **Workspaces bound to their agent for life; bound to exactly one workflow execution; TTL ≤ 1h.**
- **Disposable workspaces with recovery-first policy.** Single-flight per workspace. Failure-report-precedes-disposal invariant.
- **Trust boundary:** source code never leaves customer VPC; workspace processes have no yaaos control plane credentials.

## What's not yet decided

Two layers of open questions in [requirements.md](requirements.md):

- **Strategic gaps** (each deserving its own design round): image + protocol versioning, multi-tenancy + fairness, customer-side observability + audit, MCP proxy interaction details.
- **Implementation TBDs**: OpenAPI schemas, Claude Code invocation specifics, IPC framing details, sigv4 verifier library choice, etc. — resolve during implementation.
