# yaaos

**Yet Another Agent Orchestration Service.** Self-hosted, team-scale orchestrator that runs your coding agents against incoming work and posts the results back to the source.

## What yaaos is

yaaos is the orchestration layer. It receives a piece of work (today: a GitHub pull request), provisions an isolated workspace, hands the work to a coding agent of your choice, and posts the agent's structured output back to the source.

yaaos is **not** a coding agent. It does not review code. It does not write code. It does not call LLMs directly. Those jobs belong to the agent — yaaos provides the workflow around it.

## Principles

- **Orchestrator, not an agent.** Reviews, refactors, code writing — all delegated to a CLI agent (Claude Code today; Codex / Aider / others as plugins). yaaos's job is the workflow: webhook → ticket → workspace → agent → post-back. Zero LLM calls live in yaaos itself.
- **Bring your favourite coding agent.** Each agent is a plugin behind a small Protocol. Add a new agent by implementing `review(workspace, context)` / `reply(workspace, context)` — yaaos doesn't care which model or framework runs inside.
- **Configurable.** Agent personas (prompts), per-repo lessons, model API keys, webhook routing, time-control intervals, plugin selection — all editable at runtime via the UI or DB.
- **Workspaces are separable from the service.** A workspace is provisioned through a `WorkspaceProvider` plugin. Today: in-process tempdir + git clone. Tomorrow: Docker containers, Fly machines, K8s pods. The service Protocol doesn't change.
- **Every ticket gets its own fully isolated workspace.** One workspace per ticket, shared by every agent on that ticket. Provisioned once when the review batch starts; destroyed once every agent finishes. No cross-ticket state contamination.
- **Security by default.** Credentials encrypted at rest (Fernet, key in env). API tokens never written to a workspace's filesystem. HMAC verification on every inbound webhook. No shell-string subprocess invocations. Secrets never logged or audited.

## How it fits together (at a glance)

GitHub webhook → yaaos backend (FastAPI) → ticket + per-agent review jobs → workspace per job → coding-agent CLI subprocess → structured findings → posted back as a GitHub review. The React SPA gets live updates via SSE.

Full architecture: [`docs/system-architecture.md`](docs/system-architecture.md). Per-app + per-module docs: [`docs/README.md`](docs/README.md).

## Get started

See [`docs/setup.md`](docs/setup.md) — Docker stack, GitHub App creation, Anthropic key, local dev variant.

## Working in this repo

[`CLAUDE.md`](CLAUDE.md) holds the working rules: layering, doc discipline, test conventions, where to find what.
