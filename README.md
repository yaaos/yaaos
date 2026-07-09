**Yet Another Agent Orchestration Service** 

# yaaos is a software factory

> *A software factory is an agentic system that receives work — a PR, a ticket, a spec — and autonomously produces reviewed, tested, shipped software. Humans supervise at the level of intent and review, not keystrokes, and a learning loop makes the factory better with every run.*

Software factories are the emerging shape of AI-native engineering, and everyone is building one — Factory.ai, Augment Code, Devin, OpenHands. They all ask you to adopt *their* coding agent. yaaos doesn't. You bring the agent your team already uses — Claude Code today, with Codex, Aider, and others as plugins — and yaaos wraps it in a fully configurable, skill-based pipeline: intake, workspaces, review, posting, lessons. The factory is new; the agent isn't.

That choice matters more than it sounds. AI adoption inside a company usually starts with individual devs, then teams converge on prompts, skills, and conventions around a specific agent. Other factories ask you to throw that away and learn a new agent with new behavior. yaaos drives the agent your devs already use, with the prompts your team already wrote.

# The product

yaaos is a full SDLC software factory — code review, feature work, ops troubleshooting. The shape is the same regardless of workflow: work arrives (a PR opens, a ticket is filed), yaaos spins up a workspace, hands the work to a coding agent, and posts the result back where your team already looks.

A few principles drive the design.

**Product principles**

- **A factory, not an agent.** Reviews, refactors, and code writing are delegated to a CLI agent — Claude Code today, with Codex, Aider, and others available as plugins. yaaos owns the factory around the agent, not the intelligence inside it.
- **Bring your favorite coding agent and your own account.** You bring the API key, and your prompts go straight to the agent — yaaos doesn't sit in the middle.
- **Integrates into your stack.** GitHub is wired up today. Notion, Linear, and other tools your team already lives in will land as plugins over time.
- **Workspaces in your cloud.** Workspaces run on infrastructure you own and configure, so your code never leaves your VPC.
- **Configurable.** yaaos ships with opinions on how SDLC workflows should run, but none of them are locked in. You can shape the workflow around how your team actually works.
- **Security is a first-class concern.** Your source code is your IP, and in an era where AI tools routinely ship code off to third-party clouds, knowing exactly where it goes matters more than ever. We treat security as a central design constraint, not a checkbox. Details below.

# Security & compliance

This section is the CISO summary. Full details, code paths, and threat model in [`docs/system-security.md`](docs/system-security.md).

## Architecture in brief

Three components, two trust zones.

**Control plane** — the yaaos backend, hosted by us on fly.io. Web UI, workflow engine, audit log, plugin registries. Holds no customer source code.

**Workspace agent** — a Go binary the customer deploys on any Linux host inside their own AWS account that can assume their registered IAM role — typically ECS, EKS, or EC2. Long-polls the control plane for work, clones repos, spawns workspace processes, runs the coding agent CLI, and posts results back. Outbound TLS only; no inbound ports. This is the only component that ever touches source code.

**Coding agent CLI** — Claude Code today; Codex, Aider, and others as plugins. Runs as a subprocess inside a workspace, driven by the agent. Talks to its provider (Anthropic, OpenAI, etc.) with the customer's BYOK API key.

**Execution flow.** A PR is opened → GitHub webhook hits the control plane → control plane creates a ticket and queues an `AgentCommand` → the customer's workspace agent picks it up via long-poll → agent clones the repo into a fresh workspace → agent invokes the coding agent CLI → results stream back as `AgentEvents` → control plane posts findings to the PR. The workspace is torn down after each run.

**Key property:** source code lives only inside the workspace agent's environment, on infrastructure the customer controls. The control plane never sees it.

## Control plane security

- **Sessions.** OAuth login issues session + CSRF cookies.
- **Authorization.** Per-action role policy. Sensitive mutations (IAM ARN, region) are restricted to org admins.
- **MFA + SSO.** TOTP per-user; SAML SSO per-org for enterprise tenants (coming soon!).
- **Secrets at rest.** Fernet-encrypted columns for BYOK provider keys, SAML SP private keys, TOTP secrets, and OAuth refresh tokens. Master key in env, never in the DB. Session bearers stored as sha256 hashes only.
- **Platform secrets.** GitHub App private key and webhook secret live in env vars, not the database.
- **Webhook integrity.** Inbound GitHub webhooks are HMAC-verified against a shared secret; unsigned or tampered payloads are rejected before any application logic runs.

## Workspace security

- **IAM-anchored identity.** Each customer registers an IAM role ARN. The agent assumes that role inside the customer's AWS account; the backend replays the agent's sigv4-signed `GetCallerIdentity` against AWS STS to verify identity. Signed request URLs validated against a regex of known AWS STS hostnames. yaaos never trusts the agent's own ARN claim.
- **Replay protection.** Each agent identity signature is bound to the specific yaaos deployment it was produced for, so a valid signature from one yaaos installation cannot be replayed against another.
- **OS-process isolation.** One OS process per workspace, IPC over stdio. Container filesystem read-only except `/var/agent/workspaces/`. No landlock / seccomp / per-workspace UID today — single-tenant assumption inside the customer's own environment. Runs as an unprivileged user.
- **Zero business logic in the agent.** Every threshold, prompt, lesson, depth, and timeout is supplied by the control plane per-command. The agent ships repo clone + IPC framing + subprocess management; nothing about review behavior lives in the customer's deployed binary.
- **Workspaces in your VPC.** The agent runs on any Linux host in your AWS account (ECS, EKS, EC2, or similar) configured with the registered IAM role. Outbound TLS to the control plane only; no inbound TCP, no exposed ports. Source code never leaves your network perimeter.
- **No secrets on disk.** API keys reach the coding agent subprocess only via environment variables, never as CLI arguments (which would be visible in `ps`) and never written to disk.

## Data in transit

- **TLS everywhere.** All control-plane traffic is HTTPS / WSS. The agent only opens outbound TLS; there is no inbound listener.
- **Bearer scope.** Identity-exchange bearers are scoped to a single agent instance (`agent_id`), 1-hour TTL, rotated before expiry.

## Compliance & vendors

- **Audit log.** Every state change — user mutations, agent actions, workflow transitions — writes a typed, durable `audit_log` row attributed to a known actor. 90-day retention. Surfaced in-product per org so admins can answer "who did what, when" without filing a support ticket.
- **Data residency.** Workspaces and source code live entirely in the customer's AWS account. The control plane is hosted on fly.io, and all yaaos-side data (Postgres, Redis, telemetry) is stored in US regions.
- **Sub-processors.** All vendors below are SOC 2 compliant. All yaaos staff accounts on these services have 2FA enabled.

  | Purpose | Vendor | Data handled |
  |---|---|---|
  | Compute | [fly.io](https://fly.io) | Control plane app + worker processes |
  | WAF / edge | [Cloudflare](https://cloudflare.com) | Inbound HTTP traffic, TLS termination |
  | Email | [Resend](https://resend.com) | Transactional email (invites, notifications) |
  | Redis | [Upstash](https://upstash.com) | Session cache, pub/sub, ephemeral queues |
  | Postgres | [Neon](https://neon.tech) | Primary datastore — users, tickets, audit log, encrypted secrets |
  | Observability | [Dash0](https://dash0.com) | Traces, metrics, logs (PII-scrubbed at the exporter) |
- **Responsible disclosure.** Report suspected vulnerabilities to [admin@yaaos.dev](mailto:admin@yaaos.dev). We respond within one business day.

# Links
See https://yaaos.dev for the website and https://app.yaaos.dev for the app.
