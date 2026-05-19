# Roadmap

> Near-term execution. Usually just the next milestone + a backlog.
> Long-horizon picture lives in [VISION.md](VISION.md).

## Current milestone

### M01 — Code Review Loop  `[planned]`
Three specialist review agents (architecture, security, style) review every PR on configured repos, accept human feedback, and remember per-repo lessons. → [details](milestones/M01-code-review/README.md)

### M02 — Users, orgs, auth  `[done]`
Real users, multi-org tenancy, GitHub OAuth + SAML SSO, opaque sessions, three-role permissions, polymorphic audit log. → [details](milestones/M02-auth/README.md)

## Backlog

- **Long-running invocation supervisor (M02+)** — when implementer agents arrive (dozens of minutes to hours per invocation), yaaos needs a real supervisor for that work: separate worker process (FastAPI restarts don't kill in-flight work), heartbeat watchdog (kill silent jobs + clean up workspaces), concurrency limits (cap N simultaneous implementers), durable queue beyond the cap, checkpoint/resume after crash, cross-process cancellation. Likely lives in a new module (`core/invocations` or `core/agent_supervisor`) — invocation-shaped, not generic-task-shaped. M01 deliberately skips this: review work is minutes-long and crash-recovery via re-review on next push is acceptable.
- **Long-lived workspaces + agent handoff protocol (M02+/M03+)** — workspaces become ticket-owned environments that survive multiple implementer ↔ reviewer rounds (hours to days), not invocation-ephemeral tempdirs. Adds: `tickets.workspace_id` linkage, claim/release Protocol on `core/workspace` alongside today's `with_workspace`, a separate workflow-state dimension orthogonal to environmental state, lease watchdog (tied to the invocation supervisor's heartbeat), structured agent-handoff artifacts in DB (`workspace_artifacts` or similar), `plugins/docker_workspace` for real isolation + crash-survivable container/volume persistence, and a workflow orchestrator (likely `domain/workflow`) that drives the implementer ↔ reviewer loop. M01 design respects forward-compatibility constraints documented in [milestones/M01-code-review/internals/workspace.md § Forward compatibility](milestones/M01-code-review/internals/workspace.md#forward-compatibility-long-lived-workspaces-m02).
