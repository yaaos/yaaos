**Yet Another Agent Orchestration Service** 

# Why yaaos?

A few products in adjacent spaces. Baz, CodeRabbit, Qodo do AI-powered code reviews. Devin and OpenHands are autonomous coding agent platforms. Competing with frontier coding agents (Claude Code, Codex) is a losing proposition, so yaaos doesn't. It's a coding-agent-agnostic orchestration platform — roughly what Devin and OpenHands do, but BYO coding agent. Pick whichever one is best this month. yaaos runs it.

# The product

yaaos aims to be a full SDLC workflow solution — code review, feature work, ops troubleshooting. Today it does code reviews. The rest comes next.

**Product principles**

- **Orchestrator, not an agent.** Reviews, refactors, code writing — all delegated to a CLI agent (Claude Code today; Codex / Aider / others as plugins). yaaos's job is the workflow: webhook → ticket → workspace → agent → post-back. Zero LLM calls live in yaaos itself.
- **Bring your favourite coding agent & your own account.** Each agent is a plugin behind a small Protocol. Add a new agent by implementing `review(workspace, context)` / `reply(workspace, context)` — yaaos doesn't care which model or framework runs inside.
- **Integrates into your stack.** GitHub today. Notion, Linear, and others as plugins.
- **Configurable.** Agent personas (prompts), per-repo lessons, model API keys, webhook routing, time-control intervals, plugin selection — all editable at runtime via the UI or DB.
- **Workspaces in your cloud.** You configure your own workspaces on your own infrastructure and your code never leaves your VPC.
- **Security is a first class concern.** See the security section.

# Security & compliance
TBD

See https://yaaos.cloud for the website and https://app.yaaos.cloud for the app