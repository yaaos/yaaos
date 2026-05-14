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
| **Agent** | A configurable reviewer (architecture, security, style). Has a prompt, a model, and a tool list. |
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
| **Agent** | UUID + name | prompt_text, model_id, tool_list |
| **Lesson** | UUID | title, body, source_pr_url |
| **ReviewJob** | UUID | status, started_at, completed_at, error_message, review_external_id |
| **WebhookEvent** | external `source_event_id` | processed_at |
| **AuditEntry** | UUID | none — immutable once written |

## Value objects

Immutable; equal by attributes; no identity.

### Identifiers
- `RepoIdentifier { plugin_id, external_id }`
- `PRIdentifier { plugin_id, external_id }`

### From `domain/vcs`
- `Diff { raw, files: list[FileSummary] }`
- `FileSummary { path, status, old_path?, additions, deletions }`
- `Finding { file?, line_start?, line_end?, severity, title, body }`
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
- `PromptContext { diff, lessons, language, prior_agent_comments, pr_title, pr_body }` — what reviewer passes to executor as the *context*. The agent itself (with its `prompt_text` and `model_id`) is passed alongside in `executor.run(agent, context)`. **Together they form the frozen ReviewJob snapshot:** both the agent value AND the PromptContext are captured at ReviewJob start time and recorded in `audit_entries` (kind=`prompt_sent`). Mid-flight edits to the agent's prompt, the agent's model selection, the lessons, the PR metadata, or any other input do NOT affect an in-flight job — those changes will be visible to the next job.
- `ModelInvocation { prompt, response, tokens_in, tokens_out, cost, latency_ms }` — record of one LLM call

## Aggregates

Each aggregate is its own transactional unit. The root is the only entity referenced from outside the aggregate.

| Aggregate | Root | Invariants the root enforces |
|---|---|---|
| Ticket | Ticket | status transitions are valid |
| PullRequest | PullRequest | shas non-empty when state is `open`/`closed`; `(plugin_id, external_id)` unique |
| Repo | Repo | `(plugin_id, external_id)` unique; can't be active without a plugin install |
| Agent | Agent | prompt_text non-empty; tool_list well-formed |
| Lesson | Lesson | body ≤ 1000 chars; belongs to an active Repo |
| ReviewJob | ReviewJob | state-machine transitions valid; refers to an existing Agent + PR |
| WebhookEvent | WebhookEvent | source_event_id is the unique idempotency key |
| AuditEntry | AuditEntry | immutable once created |

**Aggregate sizing principle:** keep aggregates small. Cross-aggregate invariants (e.g., "at most one in-flight ReviewJob per PR per agent") are enforced by **domain services**, not by stuffing entities into one big root.

## Domain services

Operations that span multiple aggregates. These are stateless functions in their owning modules.

| Service | What it does | Spans |
|---|---|---|
| **ReviewWorkflow** | For a Ticket: load PR + Repo, fetch Agents, fetch Lessons-for-Repo, fetch Diff via VCS, build PromptContext, create ReviewJob per agent, dispatch via Executor, collect Findings, post Review via VCS, append AuditEntries. | Ticket, PullRequest, Repo, Agent, Lesson, ReviewJob, AuditEntry |
| **Intake** | Receive WebhookEvent → verify signature → upsert PullRequest → upsert/create Ticket → schedule ReviewJobs (subject to filtering rules). | WebhookEvent, PullRequest, Ticket, ReviewJob |
| **PerPRQueueDiscipline** | Enforces "at most one in-flight ReviewJob per PR per agent" by cancelling prior in-flight jobs before queueing a new one. Owned by `reviewer`. | ReviewJob (cross-aggregate invariant) |
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
| **Review** | `reviewer`, `agents`, `memory`, `executor`, `llm`, `plugins/anthropic`, `plugins/in_process_executor` | The review pipeline |
| **Catalog** | `repos`, `tickets`, `pull_requests`, `settings` | yaaof's entities + system status |
| **Operations** | `audit_log`, `events`, `tasks`, `observability`, `database`, `config`, `webserver`, `primitives` | Infrastructure + cross-cutting |

## Module ownership of domain concepts

| Concept | Module |
|---|---|
| Ticket aggregate, `TicketStatus` | `domain/tickets` |
| PullRequest aggregate, `PRState` | `domain/pull_requests` |
| Repo aggregate | `domain/repos` |
| Agent aggregate | `domain/agents` |
| Lesson aggregate | `domain/memory` |
| **ReviewJob aggregate, `ReviewJobStatus`, PerPRQueueDiscipline, ReviewWorkflow, PromptContext** | **`domain/reviewer`** |
| WebhookEvent aggregate | `plugins/github` |
| AuditEntry aggregate | `core/audit_log` |
| `RepoIdentifier`, `PRIdentifier`, `Diff`, `FileSummary`, `Comment`, `Review`, `Finding`, `Severity`, `ReviewVerdict` | `domain/vcs` |
| `ModelInvocation` | `domain/llm` |
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
