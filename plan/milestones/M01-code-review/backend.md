# M01 — Backend (planned)

> Planned backend modules for M01.
> Each module gets its own `docs/<module>.md` written as it's built (responsibility, public interface, owned tables, how it's tested).
> Architecture-wide context (stack, topology, layering rules) lives in [architecture.md](architecture.md) and [modularity.md](modularity.md).

## Module map

22 modules total: 8 core · 11 domain · 3 plugins.

Several domain modules have **naive M01 implementations** but their interfaces are defined now to avoid M02+ rearchitecture. Specifically: `executor` does a single LLM call with no tool loop; `tickets` has a trivial state machine; `agents` has 3 hardcoded entries; `memory` is per-repo only.

### Core (8)

Pure infrastructure. No business logic. Cannot reference any product concept.

| Module | Responsibility |
|---|---|
| `database` | Async SQLAlchemy `Base`, session/engine setup, transaction helpers. |
| `config` | pydantic-settings for boot-time env; helpers for reading/writing DB-stored runtime config; encryption-at-rest primitive. |
| `observability` | OTel SDK setup, structlog config, trace-context propagation, span helpers. |
| `webserver` | FastAPI app factory, lifespan, CORS, the route registry domain modules plug into, static SPA mount. |
| `tasks` | Background-task abstraction: `enqueue(handler, payload, delay_seconds, idempotency_key) → TaskID` and `cancel(task_id)`. Handlers are passed by reference; no name-based registry. Task names for telemetry / audit are derived from `handler.__module__` + `handler.__name__`. **M01: in-process `asyncio.create_task` + `asyncio.sleep`.** M02+: swap in TaskIQ behind the same interface. |
| `events` | In-process pub/sub for SSE broadcasting to UI clients. Not durable; `audit_log` is the durable counterpart. |
| `audit_log` | Append-only timeline primitive (`{entity_id, timestamp, kind, payload, actor}`). Domain modules write to it; UI reads from it. |
| `primitives` | Foundational value objects at the bottom of the dependency tree. Other core modules, all domain modules, and all plugins may depend on this; it depends on nothing yaaof-specific. M01 holds `Actor`, `ActorKind`. **Domain-aware data types are allowed here** (per the core rule: types-yes, behavior-no); business logic is not. Strict entry criterion: a value object that's used by 3+ modules across layers with no clear single-module home. |

### Domain (11)

Business logic. Defines plugin interfaces where pluggability is needed. Vendor-neutral.

| Module | Responsibility | M01 status |
|---|---|---|
| `vcs` | Abstract VCS types (`PullRequest`, `Review`, `Finding`, `Comment`), `VCSPlugin` Protocol, plugin registry. | Full |
| `llm` | Abstract LLM types (`Prompt`, `Response`, `Cost`), `LLMProvider` Protocol, plugin registry. | Full |
| `settings` | System-wide settings + onboarding-status aggregator. **Plugin credentials live in their respective plugin tables**, not here. In M01 this module mostly answers "is yaaof ready to operate?" by querying plugin tables (is a GitHub App installed? Is an Anthropic key set?) and exposing the answers to the dashboard. | Full |
| `repos` | Repo allowlist. Identifier is `(plugin_id, external_id)`. Provides "is repo X allowed?" check. | Full |
| `intake` | Inbound VCS events via `vcs` plugin interface; filtering rules (drafts/forks/bots); re-review command parsing; missed-event catch-up poller; dispatches into `tickets`, `pull_requests`, and `reviewer` (the latter for ReviewJob scheduling). | Full |
| `tickets` | yaaof's unit of work. A ticket flows through intake → coding → review → done. Owns the `tickets` table and lifecycle state machine. | **Naive: only `github_pr` source; state transitions limited to `in_review → complete`.** Full data model in place. |
| `pull_requests` | VCS-side mirror of pull requests: PR aggregate (yaaof's UUID + plugin identifier + cached metadata), state machine reflecting VCS state (open/closed/merged), list & detail API for UI. **Does NOT own the review-job queue** — that's `reviewer`. | Full |
| `agents` | Agent definitions: identity (architecture / security / style), prompt CRUD, model selection, tool list. | **Naive: 3 hardcoded reviewer agents with empty tool lists.** Prompt CRUD is fully wired. |
| `memory` | Per-repo lessons (title + body + source PR link, 1000-char cap). CRUD + retrieval-for-prompt. | Full |
| `executor` | Protocol + registry for "run an agent invocation." Consumers (`reviewer`) call `executor.run(agent, input) → output`. | **Naive: single LLM call via `llm`, parses output, returns. No tool loop.** Interface ready for M02+ pluggable executors (Docker, remote, etc.). |
| `reviewer` | Owns the **ReviewJob aggregate** (`review_jobs` table) + the per-PR queue discipline (cancel/supersede/debounce). The review workflow service: for a ticket with linked PR, fetch agents from `agents`, fetch lessons from `memory`, fetch diff from `vcs`, build PromptContext, create+schedule one ReviewJob per agent via `core/tasks`, parse findings, compute verdict (rule-based), post review via `vcs`. | Full |

### Plugins (3)

Vendor-specific implementations of `domain/` interfaces. The only place vendor SDKs are allowed.

| Module | Responsibility |
|---|---|
| `github` | Implements `domain/vcs`'s `VCSPlugin`. Owns GitHub App auth, HMAC webhook verification, GitHub REST API calls, translation between GitHub shapes and `domain/vcs` types. Owns its own settings (App install state). **Webhook idempotency:** on receipt, parse + verify signature, then `INSERT ... ON CONFLICT DO NOTHING` into `github_webhook_events` keyed by `source_event_id`; if the row already exists (no insert), skip dispatch. Otherwise dispatch and update `processed_at`. **Catch-up poller cursor** lives in a plugin-owned `github_poller_state` table (per-repo `last_polled_at`); on startup, query GitHub for events newer than the cursor and replay them through the normal webhook dispatch path. |
| `anthropic` | Implements `domain/llm`'s `LLMProvider`. Owns Anthropic API client, request shaping, retries with backoff, token-usage capture. |
| `in_process_executor` | Implements `domain/executor`'s `ExecutorPlugin`. Runs an agent in-process: one `llm.generate()` call, parse response, return. No tools, no sandbox. M01-only; replaced by Docker/remote executors in M02+. |

## Deferred to later milestones

Modules called out in our future planning but **not built in M01** because they have no real consumer yet:

### `tools` (domain)

Tool catalog: function signatures + descriptions + handlers that agents can call. Each tool is a Pydantic-described function the LLM can invoke during an agent run. Empty in M01; populated when the coding agent (M02) arrives.

Forecast of the M02+ tool catalog (concrete enough to plan, not built yet):

| Tool | What it does |
|---|---|
| `checkout` | Clone a repo at a given ref into a sandbox; returns a checkout id. |
| `list_files` | List files under a path in the checkout (respects .gitignore + skip rules). |
| `read_file` | Read a file's contents (with byte/line ranges). |
| `edit_file` | Replace or patch file contents (anchored by exact substring or line range). |
| `apply_patch` | Apply a unified-diff patch atomically. |
| `run_command` | Execute a shell command in the sandbox; captures stdout/stderr/exit. Sandbox enforces wall-clock + memory caps. |
| `run_tests` | Convenience wrapper around `run_command` for the repo's detected test command. |
| `git_commit` | Stage + commit changes in the sandbox; returns the new commit SHA. |
| `git_push` | Push to the linked PR's head branch. |
| `search_code` | Repo-wide grep/ripgrep; structured results. |

The catalog is open-ended; each tool is a separate registration. Tools have permission scopes (read-only, write, exec) that agents declare in their `tool_list` config.

### `sandbox` (domain) + `in_process_sandbox` (plugin)

Isolated execution environment for tool implementations. Provides:

- An ephemeral filesystem rooted at a unique path (cleaned up on close).
- Scoped process execution with wall-clock + memory limits.
- Network policy (default: deny outbound except to allowlisted hosts).
- Lifecycle hooks (created → in-use → released → reaped).

M01: not built. M02: `in_process_sandbox` plugin runs everything in the worker process with `tempfile` directories and `asyncio.subprocess`. M03+: `docker_sandbox` and beyond.

### Why these deferrals are safe

- `executor` already takes an `Agent` definition with a `tool_list`. M01 agents have empty tool lists; M02 agents reference tools by name. Executor's Protocol doesn't change when `tools` arrives.
- `executor` plugins (in_process today; Docker/remote tomorrow) will gain sandbox awareness when tools arrive — that's an executor-internal concern, not a Protocol break.
- No M01 table or interface needs to know about tools or sandboxes; those concepts are M02 additions.

## Boundary decisions

Module boundaries deliberately drawn this way:

- **`agents` owns agent prompts.** Prompts are agent configuration. Not in `reviewer`, not in `memory`.
- **`memory` is its own module.** Vision principle ("memory is institutional") makes it a first-class concept. Per-repo today; could become per-agent or cross-repo later without disturbing consumers. Reviewer fetches lessons through `memory`'s public interface.
- **`agents` and `memory` schemas are designed for clean git export.** Each prompt and each lesson maps to a single row that can be serialized to a single file with no joins. Source of truth is Postgres; history is in `audit_log`. If a later milestone moves to git-managed config, the consumer interface doesn't change. See [architecture.md § Configuration storage](architecture.md#configuration-storage-prompts-lessons-agent-definitions-repo-specific-config).
- **`executor` and the in-process implementation are separate.** Executor defines the Protocol; `plugins/in_process_executor` is the M01 implementation. Future executors (Docker, ephemeral cloud) plug in without touching `domain/`.
- **`reviewer` is thin.** It's the review *workflow*: orchestrates `agents` + `memory` + `vcs` + `executor`. It does not own prompts, memory, the LLM call, or the agent loop.
- **`tickets` is separate from `pull_requests`.** Ticket is yaaof's unit of work; PR is the VCS-side artifact a ticket may reference. In M01 every PR webhook creates a ticket; in M02 Linear/Jira intake creates tickets without PRs (coding agent later attaches one).
- **`intake` dispatches into `tickets`, `pull_requests`, and `reviewer`.** Webhook → upsert PR (in `pull_requests`) → upsert/create matching ticket (in `tickets`) → call `reviewer.schedule_review(ticket_id)` which creates the ReviewJob rows and enqueues them via `core/tasks`.
- **`reviewer` owns the ReviewJob aggregate and the per-PR queue discipline.** Earlier we placed the per-PR job queue in `pull_requests`. Through a domain-model lens that's wrong: `pull_requests` is the VCS mirror; ReviewJob is a workflow concept. `reviewer` owns the workflow's entity. `pull_requests` shrinks to a pure VCS mirror.
- **Plugin credentials live in plugin tables**, not in `domain/settings`. `plugins/github` owns the App install state, the encrypted private key, and the webhook signing secret. `plugins/anthropic` owns the encrypted API key and model defaults. `domain/settings` is the onboarding-status query layer that reads from plugin tables; it does NOT store credentials itself.

## What this is NOT

Things explicitly out of scope for these modules in M01:

- **`executor` does NOT run agents in isolated processes / containers.** That's `sandbox` (deferred).
- **`agents` does NOT support custom user-defined agents.** Three hardcoded reviewer types; CRUD is on the prompts only.
- **`tickets` does NOT support non-PR sources.** Only `source='github_pr'`. Linear/Jira/Slack come later.
- **`memory` does NOT support cross-repo or per-agent scoping.** Per-repo only.
- **`reviewer` does NOT do anything beyond review.** No coding, no test running, no merging.

## Open for next pass

Per-module deep dives live in [internals/](internals/). Already done: [vcs.md](internals/vcs.md). To be written:

- `llm` (sister to vcs)
- `events` (in-process pub/sub for SSE in M01; design must accommodate M02 cross-process when TaskIQ adds a worker)
- `audit_log`
- `plugins/github`
- `intake`
- `pull_requests`
- `reviewer`
- `tickets` (new — added by this revision)
- `agents` (new — added by this revision)
- `executor` + `in_process_executor` (new — added by this revision)
- `memory` (was folded into reviewer earlier; now its own module)

## Decisions

> Entries are dated; later entries supersede earlier ones where they conflict. The **current** module map is at the top of this doc (8 core · 11 domain · 3 plugins); the decisions log preserves the evolution.

### 2026-05-13 — Module map locked (initial) — *superseded by 2026-05-14*
7 core, 7 domain, 2 plugins. Prompts and memory folded into `reviewer`. No standalone `webhooks`, `metrics`, `secrets_detection`, or `diff_preprocessor`.

### 2026-05-13 — Plugin-owned settings
Plugin-specific runtime state (e.g., GitHub App install state) lives in the plugin module's own table, not in `domain/settings`.
**Why:** swapping or adding a VCS plugin shouldn't require migrating rows out of a shared settings table.

### 2026-05-14 — Define structurally-important future modules now
Added `tickets`, `agents`, `memory` (restored), `executor`, and `plugins/in_process_executor` to M01 scope with naive implementations. Defer pure-stub modules (`tools`, `sandbox`) until they have a real consumer.
**Why:** these modules sit on data-model and decoupling boundaries that are expensive to retrofit. Naive in-place implementations now cost ~5 module skeletons; retrofitting later costs migrations, refactors, and Protocol breaks.

### 2026-05-14 — Ticket as yaaof's unit of work; PR as VCS artifact
`tickets` is the thing that flows through yaaof's pipelines. `pull_requests` mirrors VCS state. A ticket may reference a PR; M01 always does because every ticket comes from a PR webhook.
**Why:** in M02+ tickets come from Linear/Jira/Slack with no PR; coding agent attaches one later. Designing the data model for that now avoids a big refactor.

### 2026-05-14 — `agents`, `executor`, `memory` split out of `reviewer`
Reviewer becomes a thin workflow orchestrator. Agent configuration (prompts, model, tools) lives in `agents`. Agent execution lives in `executor`. Lessons live in `memory`.
**Why:** these are different concerns that will evolve independently. Coding agents (M02+) will reuse `agents` and `executor` without touching reviewer.

### 2026-05-14 — `worker` renamed to `tasks`; TaskIQ deferred to M02
Background-task abstraction lives in `core/tasks` with a backend-agnostic interface. M01 implementation is in-process `asyncio.create_task` + `asyncio.sleep`. TaskIQ swaps in later behind the same interface.
**Why:** M01 has no need for cross-process job durability, and the TaskIQ broker setup is friction we can defer. Locking the interface now means the swap is mechanical.

### 2026-05-14 — No `register_task`; handlers passed by reference
`enqueue` takes a function reference, not a string name. No name→handler registry.
**Why:** name-based dispatch is only required when handlers cross process boundaries (which is M02's TaskIQ concern). For an in-process M01, function references are sufficient and remove ceremony. When TaskIQ arrives, handlers gain a `@task` decoration at definition time and `enqueue` resolves the registered task object internally; the consumer-facing interface stays unchanged.

### 2026-05-14 — ReviewJob aggregate moves from `pull_requests` to `reviewer`
`reviewer` owns the `review_jobs` table, the ReviewJobStatus state machine, and the per-PR queue discipline (cancel/supersede/debounce). `pull_requests` becomes a pure VCS-mirror module.
**Why:** ReviewJob is a workflow concept, not a VCS-mirror concept. Putting it in `pull_requests` mixed two unrelated responsibilities. See [domain-model.md](domain-model.md) for the DDD analysis.

### 2026-05-14 — New core module: `primitives`
Bottom-of-the-dependency-tree module for foundational value objects. Other core modules (and all of domain/plugins) may depend on it. M01: holds `Actor` + `ActorKind`. Domain-aware data types are allowed here (core forbids business *logic*, not domain-aware *types*).
**Why:** `Actor` is used by audit_log, agents, intake, and the UI; making any one consumer its home creates bad coupling. A dedicated foundational-types module gives those types a clean home at the bottom of the dependency tree — and the name "primitives" signals architectural role rather than acting as a generic dump location.
