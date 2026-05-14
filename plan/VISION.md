# Vision

> Long-horizon view of yaaof. Two pages max (~700 words).
> Audience: me + future collaborators landing cold.
> Update rarely — only when the answer to "what is this thing" changes.

**yaaof turns Linear/Jira tickets, Slack threads, and operational alerts into reviewed, tested, ready-to-merge pull requests — a team-scale agent orchestration service for engineering teams of 2–100 who want Composio-style coding agents running as a shared, configurable team service rather than a per-developer CLI.**

## Problem & user

Teams of 2–100 developers are stuck between two bad options. The current wave of coding agents — Claude Code, Codex, Aider, Composio's orchestrator — are individual-developer tools: every engineer installs them on their own laptop, runs them against their own clone, and the rest of the team has no visibility, no shared budget, no consistent review standards. The team-scale alternatives are heavyweight platforms aimed at 100+ engineer enterprises, with the price tag and rollout cost to match. The team in the middle wants to dispatch a coding agent from a Linear ticket without context-switching, watch it work in a place everyone can see, have review agents gate its output, and pull a human in only when the agents are stuck. That product doesn't exist yet.

## What it is

yaaof is a self-hosted service that any team member can trigger from where they already work. Tickets in Linear/Jira, threads in Slack, or operational alerts dispatch a coding agent that opens a worktree, writes code, and runs tests in an ephemeral environment it spins up on demand. Configurable review agents — architecture, security, and any custom ones the team defines — gate the resulting PR; their feedback flows back to the coding agent until tests pass and reviews clear. Humans see one shared dashboard, get pinged in Slack when attention is needed, and approve the merge. Agents on both sides remember feedback so the team's preferences accumulate as institutional memory. Every step — intake source, agent roster, model choice, review policy, merge gate, notification routing — is configurable; sensible defaults let a team get started in an hour.

## Principles

- **Opinionated defaults, configurable everywhere.** A team dispatches their first agent in under an hour using stock settings. As needs sharpen, every default is replaceable — intake source, agent roster, model, review policy, merge gate, notification routing — without forking the codebase.
- **One shared view, not many private ones.** Most actions are visible to the whole team by default. Filtering replaces partitioning.
- **Humans set policy; agents execute it.** Whether a PR auto-merges or waits for human approval is a configuration choice, not a hard-coded rule. The default favors human approval.
- **Memory is institutional.** Lessons learned by one agent on one ticket apply to the whole team's future work, not just the user who triggered the job.
- **Every action is auditable.** Every step an agent takes — prompt, tool call, file change, test result, review verdict — is captured in a human-readable timeline on the ticket. If you can't answer "why did the agent do that," we've built it wrong.
- **Composable agents, not a frozen pipeline.** Adding a new review agent — accessibility, performance, custom domain rules — is a config change, not a code change.
- **Self-hosted.** The team owns its data, its agents, its model API keys, and its budget.

## Shape

- **Intake adapters** — Linear, Jira, Slack, generic webhook for ops alerts. New adapters pluggable.
- **Coding agents** — multi-model, worktree-isolated, able to request ephemeral test environments. Model choice configurable per job class.
- **On-demand test environments** — unit, service, and full e2e; provisioned per job, torn down after.
- **Review agents** — built-in (architecture, security, style) plus fully custom; defined and reordered via config.
- **Feedback loop** — review output piped back to the coding agent until pass or human escalation. Loop limits configurable.
- **Memory layer** — feedback from agents and humans persisted as durable, team-wide lessons applied to future jobs.
- **Auth & permissions** — login, two roles (admin / member); most things visible to all members.
- **GitHub integration** — repo access, branch/PR mechanics, CI awareness.
- **Shared dashboard** — every job visible to everyone; filter to "mine."
- **Per-ticket audit log** — human-readable timeline of every agent action, decision, and artifact, linkable and shareable.
- **Observability** — metrics, logs, and traces for the service itself: success rate, time-to-merge, cost-per-ticket, human-override rate, agent failures.
- **Slack routing** — by default pings the ticket reporter; routing rules configurable.
- **Budget** — one team-wide pool with per-user attribution; hard and soft caps configurable.
- **Admin configuration** — review agents, intake sources, policies, budgets, models, prompts.

## Non-goals

- **Per-developer laptop CLI.** That's Composio's space; we're the team service.
- **Hard-coded workflows.** If a team can't replace a step, we've built it wrong.
- **Enterprise compliance certifications.** SSO/SAML, SOC2, FedRAMP, immutable audit logs — not the market.
- **Owning the IDE.** Engineers keep their editor of choice. yaaof talks via tickets, Slack, and PRs.
