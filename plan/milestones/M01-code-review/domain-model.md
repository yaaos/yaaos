# M01 — Domain Model (planned)

> The abstract / code-level domain model: entities, value objects, aggregates, services, and the ubiquitous language.
> Companion to [data-model.md](data-model.md) (the persistence side). **Read this first**; data-model.md is downstream.
> Source of truth for the vocabulary every module — and every conversation — must use.

## Ubiquitous language

These are the words yaaof uses about itself in code, in commits, in PR descriptions, in this doc set. Don't drift. If you want a different word for one of these, propose updating the language first.

| Term | Meaning |
|---|---|
| **Ticket** | Unit of work yaaof tracks. In M01, every ticket originates from a GitHub PR. |
| **Pull Request (PR)** | VCS-side artifact yaaof mirrors. A ticket may reference one PR; M01 always does. |
| **Repo** | A code repository on the allowlist; yaaof acts only on PRs from listed repos. |
| **Agent** | A configurable reviewer (architecture, security, style). Has a prompt and a coding-agent plugin id (which CLI runs it). |
| **Lesson** | A human-supplied piece of feedback persisted per-repo to influence future reviews. |
| **Review** | A bundle of findings posted on a PR by one agent. Has a verdict. |
| **Finding** | A single piece of agent feedback: file/line + severity + title + body. |
| **Review Job** | One agent's attempt to review one PR. Has a state machine. |
| **Verdict** | An agent's stance: APPROVED / CHANGES_REQUESTED / COMMENT. Derived from finding severities. |
| **Actor** | Who did a thing: a GitHub user, an agent, or the system. |
| **Audit Entry** | Immutable record of an action that happened. |
| **Webhook Event** | Inbound notification from a VCS plugin (PR opened, commit pushed, etc.). |

## Entities

Identity persists through state changes. Each is referenced by yaaof UUID internally.

| Entity | Identity | Mutable attributes |
|---|---|---|
| **Ticket** | UUID | status, title, description (synced from source) |
| **PullRequest** | UUID; also addressed as `(plugin_id, external_id)` | shas, draft/ready, state, last_synced_at |
| **Repo** | UUID; also `(plugin_id, external_id)` | language_hint, status |
| **Agent** | UUID + name | prompt_text, coding_agent_plugin_id, agent_config |
| **Lesson** | UUID | title, body, source_pr_url |
| **ReviewJob** | UUID | status, started_at, completed_at, error_message, review_external_id |
| **WebhookEvent** | external `source_event_id` | processed_at |
| **AuditEntry** | UUID | none — immutable once written |
| **Workspace** | UUID | status (creating/active/expired/destroying/destroyed/destroy_failed), plugin_state, destroyed_at, destroy_attempts |

## Value objects

Immutable; equal by attributes; no identity.

### Identifiers
- `RepoIdentifier { plugin_id, external_id }`
- `PRIdentifier { plugin_id, external_id }`

### From `domain/vcs`
- `Diff { raw, files: list[FileSummary] }`
- `FileSummary { path, status, old_path?, additions, deletions }`
- `Finding { file?, line_start?, line_end?, severity, title, body, rationale?, snippet?: list[FindingSnippetLine], applied_lesson_ids: list[UUID] }`
- `FindingSnippetLine { line_number, kind ∈ context | add | del, text }`
- `Review { state, summary_body?, findings: list[Finding] }`
- `Comment { external_id, body, file?, line?, posted_at, in_reply_to? }`

### Enums
- `Severity` ∈ `must-fix` / `nit` / `suggestion` / `info`
- `TicketStatus` ∈ `open` / `in_review` / `complete` / `abandoned`
- `PRState` ∈ `open` / `closed` / `merged`
- `ReviewVerdict` ∈ `APPROVED` / `CHANGES_REQUESTED` / `COMMENT`
- `ReviewJobStatus` ∈ `queued` / `running` / `posted` / `failed` / `skipped` / `cancelled`
- `ActorKind` ∈ `github_user` / `agent` / `system`

### Cross-cutting
- `Actor { kind: ActorKind, login?, agent_id? }` — who did a thing
- `AgentSpec { name, prompt_text, coding_agent_plugin_id, agent_config: dict }` — the invocable shape of an agent. Defined in `core/coding_agent`; subclassed by `domain/reviewer.ReviewerAgent` (and future `domain/implementer.ImplementerAgent`) to add persistence fields (id, org_id, timestamps, is_built_in).
- `AgentPrompt { system_instruction, diff, lessons, language, prior_agent_comments, pr_title, pr_body, output_schema }` — what `reviewer` assembles and passes to `core/coding_agent.invoke()`. The agent CLI receives this as its prompt; it does its own LLM calls + tool use to produce the structured output. **Frozen ReviewJob snapshot** comprises: the AgentSpec, the AgentPrompt, and the workspace's checkout sha — captured at ReviewJob start time and recorded in `audit_entries` (kind=`review_job.prompt_sent`).
- `AgentInvocationResult { parsed: T, status, raw_output, tokens_in?, tokens_out?, cost_usd?, latency_ms, error_message? }` — what a `CodingAgentPlugin.invoke()` returns. `parsed` is the agent's structured output coerced to the schema yaaof requested (typically a `FindingList`). Token / cost fields are best-effort (only populated if the CLI reports them). Defined in `core/coding_agent`.
- `WorkspaceSpec { repo, sha, branch_name?, resource_caps, network_policy }` — what's needed to provision a workspace. Lives in `core/workspace`.
- `ResourceCaps { cpu_count, memory_mb, wallclock_seconds, disk_mb }` — limits enforced by workspace plugins.
- `NetworkPolicy` — enum: `deny_all` / `github_only` / `allow_all`. M01 in_process_workspace ignores this (no real isolation); future plugins enforce.

## Aggregates

Each aggregate is its own transactional unit. The root is the only entity referenced from outside the aggregate.

| Aggregate | Root | Invariants the root enforces |
|---|---|---|
| Ticket | Ticket | status transitions are valid |
| PullRequest | PullRequest | shas non-empty when state is `open`/`closed`; `(plugin_id, external_id)` unique |
| Repo | Repo | `(plugin_id, external_id)` unique; can't be active without a plugin install |
| Agent | Agent | prompt_text non-empty; `coding_agent_plugin_id` references a registered plugin; `agent_config` validates against that plugin's `validate_config` |
| Lesson | Lesson | body ≤ 1000 chars; belongs to an active Repo |
| ReviewJob | ReviewJob | state-machine transitions valid; refers to an existing Agent + PR |
| WebhookEvent | WebhookEvent | source_event_id is the unique idempotency key |
| AuditEntry | AuditEntry | immutable once created |
| Workspace | Workspace | status transitions valid (`creating → active → expired/destroying → destroyed/destroy_failed`); `plugin_state` populated by activation time; `destroyed_at` set when terminal |

**Aggregate sizing principle:** keep aggregates small. Cross-aggregate invariants (e.g., "at most one in-flight ReviewJob per PR per agent") are enforced by **domain services**, not by stuffing entities into one big root.

## Domain services

Operations that span multiple aggregates. These are stateless functions in their owning modules.

| Service | What it does | Spans |
|---|---|---|
| **ReviewWorkflow** | For a Ticket: load PR + Repo, fetch Agents, fetch Lessons-for-Repo, fetch Diff via VCS, build the AgentPrompt, create a ReviewJob per agent, provision a Workspace via `core/workspace`, invoke the coding-agent CLI via `core/coding_agent.invoke`, collect Findings, post Review via VCS, append AuditEntries. | Ticket, PullRequest, Repo, Agent, Lesson, ReviewJob, Workspace, AuditEntry |
| **Intake** | Receive WebhookEvent → verify signature → upsert PullRequest → upsert/create Ticket → schedule ReviewJobs (subject to filtering rules). | WebhookEvent, PullRequest, Ticket, ReviewJob |
| **PerPRQueueDiscipline** | Enforces "at most one in-flight ReviewJob per PR per agent" by cancelling prior in-flight jobs before queueing a new one. Owned by `reviewer`. | ReviewJob (cross-aggregate invariant) |
| **WorkspaceReaper** | Periodic `async def` loop in `core/workspace`, started in FastAPI's `lifespan`, that expires over-budget workspaces, retries plugin destroy on failure, and escalates `destroy_failed` rows. Runs every ~30s. | Workspace |
| **CodingAgentPlugin.invoke** | Spawns the agent CLI in the workspace, waits for completion, parses output into `AgentInvocationResult`. Implemented per-plugin (claude_code in M01; codex / aider / etc. later). | (uses Workspace) |
| **OnboardingStatus** | Compute "is yaaof ready to operate?" by querying plugin-side state. | (read-only across plugin aggregates) |
| **CatchUpPoller** | On startup, poll VCS for events missed during downtime; replay through Intake. | WebhookEvent, PullRequest, Ticket |

## Actors

yaaof has **no first-class User entity in M01** (no auth). All actions are attributed via the `Actor` value object:

- PR author: `Actor{kind='github_user', login='alice'}`
- yaaof agent action: `Actor{kind='agent', agent_id=<architecture-agent-id>}`
- Anonymous UI action (M01, no auth): `Actor{kind='system'}`
- Catch-up poller, scheduled tasks, etc.: `Actor{kind='system'}`

In M02 when auth lands, a `User` entity is introduced with linked-account information; the `ActorKind` enum gains a `yaaof_user` variant. Until then, `Actor` is the only abstraction needed.

## Bounded contexts (soft groupings)

yaaof is one bounded context, but the modules naturally cluster into sub-domains. The boundaries below aren't enforced (modularity does that at the module level), but they're useful when reasoning about scope.

| Sub-domain | Modules | What it owns |
|---|---|---|
| **Intake** | `intake`, `vcs`, `plugins/github` | Receiving and normalizing inbound events |
| **Review** | `reviewer` (owns review_jobs + reviewer_agents), `memory`, `core/coding_agent`, `plugins/claude_code` | The review pipeline (uses workspace from Runtime). yaaof invokes a coding-agent CLI; the CLI owns LLM calls + tool use. |
| **Catalog** | `repos`, `tickets`, `pull_requests`, `settings` | yaaof's entities + system status |
| **Runtime** | `core/workspace`, `plugins/in_process_workspace` | Provisioned environments where code work happens |
| **Operations** | `audit_log`, `events`, `observability`, `database`, `config`, `webserver`, `primitives` | Infrastructure + cross-cutting |

## Module ownership of domain concepts

| Concept | Module |
|---|---|
| Ticket aggregate, `TicketStatus` | `domain/tickets` |
| PullRequest aggregate, `PRState` | `domain/pull_requests` |
| Repo aggregate | `domain/repos` |
| ReviewerAgent aggregate | `domain/reviewer` |
| Lesson aggregate | `domain/memory` |
| **ReviewJob aggregate, `ReviewJobStatus`, PerPRQueueDiscipline, ReviewWorkflow, PromptContext** | **`domain/reviewer`** |
| `AgentSpec`, `AgentInvocationResult[T]`, `CodingAgentPlugin` Protocol, plugin registry | `core/coding_agent` |
| Workspace aggregate, `WorkspaceSpec`, `Workspace` Protocol, `WorkspaceProvider` Protocol, `ResourceCaps`, `NetworkPolicy`, `WorkspaceStatus`, WorkspaceReaper | `core/workspace` |
| WebhookEvent aggregate | `plugins/github` |
| AuditEntry aggregate | `core/audit_log` |
| `RepoIdentifier`, `PRIdentifier`, `Diff`, `FileSummary`, `Comment`, `Review`, `Finding`, `Severity`, `ReviewVerdict` | `domain/vcs` |
| `Actor`, `ActorKind` | `core/primitives` |
| Intake service, CatchUpPoller | `domain/intake` |
| OnboardingStatus service | `domain/settings` |

## Decisions

### 2026-05-14 — Domain model documented explicitly
Entities, value objects, aggregates, services, and ubiquitous language are first-class in the docs.
**Why:** consistent vocabulary across code, docs, and conversations; aggregate boundaries inform module ownership; small aggregates are a hard discipline that benefits from being written down.

### 2026-05-14 — Small aggregates
Each aggregate is its own transactional unit. Cross-aggregate invariants go in domain services.
**Why:** smaller aggregates = simpler locking, less contention, more flexibility. The cost of one extra service is less than the cost of a god-aggregate.

### 2026-05-14 — No first-class User entity in M01
Actors are tracked via the `Actor` value object. yaaof users (with auth) arrive in a later milestone.
**Why:** M01 has no auth; modeling users now would be speculative.

### 2026-05-14 — ReviewJob aggregate is owned by `reviewer`
ReviewJob is a workflow concept, not a VCS-mirror concept. `reviewer` owns its lifecycle and the per-PR queue discipline. `pull_requests` shrinks to a pure VCS mirror.
**Why:** clean separation between "what the VCS thinks is true" and "what yaaof is doing about it."

### 2026-05-14 — `Actor` lives in `core/primitives`
A new core module at the bottom of the dependency tree: foundational value objects that other core modules + all domain modules + all plugins may import. Domain-aware data types are allowed here per the clarified core rule (types-yes, behavior-no).
**Why:** `Actor` is used by audit_log, agents, intake, and the UI; making any one consumer its home creates bad coupling. "Primitives" signals architectural role (foundational types) rather than acting as a generic dump location.
