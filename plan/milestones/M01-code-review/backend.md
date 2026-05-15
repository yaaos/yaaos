# M01 — Backend (planned)

> Planned backend modules for M01.
> Each module gets its own `docs/<module>.md` written as it's built (responsibility, public interface, owned tables, how it's tested).
> Architecture-wide context (stack, topology, layering rules) lives in [architecture.md](architecture.md) and [modularity.md](modularity.md).

## Module map

20 modules total: 9 core · 8 domain · 3 plugins.

Several domain modules have **naive M01 implementations** but their interfaces are defined now to avoid M02+ rearchitecture. Specifically: `tickets` has a trivial state machine; `reviewer` has 3 hardcoded agents (all using `claude_code` as their coding-agent plugin); `memory` is per-repo only. The `in_process_workspace` plugin uses tempdirs + subprocess (no real isolation) — fine for POC; real isolation comes with `plugins/docker_workspace` in M02+. The `claude_code` plugin shells out to the locally-installed Claude Code CLI; M02+ K8s will run agents in pods with the CLI pre-baked.

**M01 fundamental architecture choice:** yaaof does NOT call LLMs directly. We invoke existing CLI coding agents (Claude Code in M01; Codex / Aider / etc. as plugins later) that own their own LLM calls + tool use + agent loop. yaaof orchestrates: provision a workspace, hand the agent a prompt + working directory, parse its output (structured JSON), post the review. The `core/coding_agent` Protocol is the abstraction; agent CLIs are plugins.

### Core (9)

Pure infrastructure. No business logic. Cannot reference any product concept.

| Module | Responsibility |
|---|---|
| `database` | Async SQLAlchemy `Base`, session/engine setup, transaction helpers. |
| `config` | pydantic-settings for boot-time env; helpers for reading/writing DB-stored runtime config; encryption-at-rest primitive. |
| `observability` | OTel SDK setup, structlog config, trace-context propagation, span helpers. |
| `webserver` | FastAPI app factory, lifespan, CORS, the route registry domain modules plug into, static SPA mount. |
| `events` | In-process pub/sub for SSE broadcasting to UI clients. Not durable; `audit_log` is the durable counterpart. |
| `audit_log` | Append-only timeline primitive (`{entity_id, timestamp, kind, payload, actor}`). Domain modules write to it; UI reads from it. |
| `workspace` | Provisioned environment where code work happens. Defines `WorkspaceSpec`, `Workspace` Protocol (`working_dir` path + lifecycle methods; **no file-reading or search methods in M01** — coding agents have their own tools), `WorkspaceProvider` Protocol. **Owns lifecycle centrally** via the `workspaces` DB table + a reaper task that enforces wall-clock caps and retries plugin-side destroy. Plugins are dumb actuators: `provision(spec) → (handle, plugin_state)` + `destroy(plugin_state)`. Implemented by `plugins/in_process_workspace` in M01; `plugins/docker_workspace` and beyond in M02+. |
| `coding_agent` | Protocol + registry for "invoke a coding agent CLI." Defines `CodingAgentPlugin` Protocol with `invoke(workspace, prompt, agent_config) → AgentInvocationResult` (spawn the CLI in the workspace dir, wait for completion, parse output, return findings). Vendor-neutral. Implemented by `plugins/claude_code` in M01; future plugins handle Codex, Aider, etc. **yaaof never calls an LLM directly in M01** — the coding agent CLI does. |
| `primitives` | Foundational value objects + tiny helpers at the bottom of the dependency tree. Other core modules, all domain modules, and all plugins may depend on this; it depends on nothing yaaof-specific. M01 holds `Actor`, `ActorKind`, and `spawn(name, coro)` — a 5-line fire-and-forget wrapper around `asyncio.create_task` that attaches a structured log line + OTel span around the background coroutine. **Domain-aware data types are allowed here** (per the core rule: types-yes, behavior-no); business logic is not. Strict entry criterion: a value object that's used by 3+ modules across layers with no clear single-module home. |

### Domain (8)

Business logic. Defines plugin interfaces where pluggability is needed. Vendor-neutral.

| Module | Responsibility | M01 status |
|---|---|---|
| `vcs` | Abstract VCS types (`PullRequest`, `Review`, `Finding`, `Comment`), `VCSPlugin` Protocol, plugin registry. Yaaof-domain types. | Full |
| `settings` | System-wide settings + onboarding-status aggregator. **Plugin credentials live in their respective plugin tables**, not here. In M01 this module mostly answers "is yaaof ready to operate?" by querying plugin tables (is a GitHub App installed? Is an Anthropic key set?) and exposing the answers to the dashboard. | Full |
| `repos` | Repo allowlist. Identifier is `(plugin_id, external_id)`. Provides "is repo X allowed?" check. | Full |
| `intake` | Inbound VCS events via `vcs` plugin interface; filtering rules (drafts/forks/bots); re-review command parsing; missed-event catch-up poller; dispatches into `tickets`, `pull_requests`, and `reviewer` (the latter for ReviewJob scheduling). | Full |
| `tickets` | yaaof's unit of work. A ticket flows through intake → coding → review → done. Owns the `tickets` table and lifecycle state machine. | **Naive: only `github_pr` source; state transitions limited to `in_review → complete`.** Full data model in place. |
| `pull_requests` | VCS-side mirror of pull requests: PR aggregate (yaaof's UUID + plugin identifier + cached metadata), state machine reflecting VCS state (open/closed/merged), list & detail API for UI. **Does NOT own the review-job queue** — that's `reviewer`. | Full |
| `memory` | Per-repo lessons (title + body + source PR link, 1000-char cap). CRUD + retrieval-for-prompt. | Full |
| `reviewer` | Owns: the **ReviewJob aggregate** (`review_jobs` table), the per-PR queue discipline (cancel/supersede/debounce), and the **review agents** (`reviewer_agents` table; 3 hardcoded rows: architecture / security / style; prompt CRUD via UI). The review workflow service: for a ticket with linked PR, create a workspace via `core/workspace` (checked out at PR head), fetch its agents + lessons from `memory` + diff from `vcs`, **assemble the agent prompt** (system instructions + diff + lessons + language hint + prior comments + output-schema-spec), invoke each agent via `core/coding_agent` (the agent CLI does its own LLM calls + tool use inside the workspace), parse the agent's structured output into `Finding`s, compute verdict (rule-based), post review via `vcs`. Each review_job gets its own workspace. | Full |

### Plugins (3)

Vendor-specific implementations of `domain/` interfaces. The only place vendor SDKs are allowed.

| Module | Responsibility |
|---|---|
| `github` | Implements `domain/vcs`'s `VCSPlugin`. Owns GitHub App auth, HMAC webhook verification, GitHub REST API calls, translation between GitHub shapes and `domain/vcs` types. Owns its own settings (App install state). **Webhook idempotency:** on receipt, parse + verify signature, then `INSERT ... ON CONFLICT DO NOTHING` into `github_webhook_events` keyed by `source_event_id`; if the row already exists (no insert), skip dispatch. Otherwise dispatch and update `processed_at`. **Catch-up poller cursor** lives in a plugin-owned `github_poller_state` table (per-repo `last_polled_at`); on startup, query GitHub for events newer than the cursor and replay them through the normal webhook dispatch path. |
| `claude_code` | Implements `core/coding_agent`'s `CodingAgentPlugin`. Wraps the [Claude Code CLI](https://docs.claude.com/en/docs/claude-code). Invokes `claude --print --output-format=json "<prompt>"` in the workspace directory with `ANTHROPIC_API_KEY` from its own settings table. Parses Claude Code's structured output into our normalized `AgentInvocationResult` (findings + token-usage + cost when reported by the CLI). Owns `claude_code_settings` table (encrypted API key + CLI config). |
| `in_process_workspace` | Implements `core/workspace`'s `WorkspaceProvider`. Provisions a workspace via `tempfile.mkdtemp` + `git clone --depth=1` of the repo at the requested sha. No real isolation — runs in yaaof's process. POC-only; M02+ adds `plugins/docker_workspace` for real sandboxing. |

## `core/primitives.spawn()` semantics

Every background coroutine in M01 goes through this single helper. Its contract:

```python
def spawn(name: str, coro: Coroutine[Any, Any, None]) -> None:
    """Fire-and-forget background work.

    - Wraps `coro` in an OTel span named f"spawn:{name}".
    - Propagates the caller's structlog ContextVars (request_id, trace_id) into the
      spawned coroutine so its log lines correlate to the caller.
    - On exception from `coro`: logs `kind='spawn.crashed'` with name + traceback at
      ERROR level. Does NOT re-raise (there's no caller awaiting it to receive the
      exception). The coroutine is responsible for marking its own domain-row state
      to 'failed' BEFORE raising — once spawn() catches, the domain row is the
      durable record of what happened.
    - Returns nothing. The caller has no handle to the coroutine; cancellation is
      always via DB state flip + cooperative polling inside the coro.
    - The created `asyncio.Task` is kept in a module-level set until completion so
      the GC doesn't collect it mid-flight (asyncio's standard pitfall).
    """
```

Used by: reviewer (dispatching a `_run_review_job`), GitHub plugin (the one-shot catch-up coro), workspace reaper loop (the periodic sweep), audit-log retention prune loop (when added). Anything that wants to run a coroutine without a caller awaiting it.

**Not used for:** anything a caller is going to `await`. That's a normal async call, not a `spawn`.

## What M02+ adds

Modules and capabilities anticipated but not built in M01:

- **`core/llm`** — abstract LLM-call layer. Comes back when yaaof needs to make LLM calls *itself* (not via a coding-agent CLI). Likely use cases: summarizing audit-log entries, scoring lesson relevance for retrieval, semantic search. Has no consumer in M01.
- **Additional coding-agent plugins** (`plugins/codex`, `plugins/aider`, etc.) — same `CodingAgentPlugin` Protocol; different CLI invocation; different output parsing. Yaaof's agent definitions choose which plugin via `coding_agent_plugin_id`.
- **Isolated workspace plugins** — `plugins/docker_workspace` (containers), `plugins/fly_machine_workspace` (ephemeral VMs). Real isolation, resource caps, network policy enforcement. M01's `in_process_workspace` is trusted-environment-only.
- **Workspace `Workspace` Protocol extensions** for the case when yaaof itself needs to manipulate the checkout (rare). The vast majority of file/process work happens *inside* the coding-agent CLI, not via yaaof reaching into the workspace.
- **K8s deployment** — coding-agent CLIs run in pods. The `coding_agent` plugins' invocation logic switches from "subprocess on yaaof host" to "schedule a pod with the CLI baked in." Workspace plugins similarly switch to pod-based provisioning.

## Boundary decisions

Module boundaries deliberately drawn this way:

- **`reviewer` owns its own agents (the `reviewer_agents` table + prompt CRUD).** In M01 there's no generic "agents" concept — the 3 agents are review-specific. M02+ `domain/implementer` will own its own `implementer_agents` table. Cross-workflow agent sharing is YAGNI.
- **`memory` is its own module.** Vision principle ("memory is institutional") makes it a first-class concept. Per-repo today; could become per-agent or cross-repo later without disturbing consumers. Reviewer fetches lessons through `memory`'s public interface.
- **`reviewer_agents` and `lessons` schemas are designed for clean git export.** Each prompt and each lesson maps to a single row that can be serialized to a single file with no joins. Source of truth is Postgres; history is in `audit_log`. If a later milestone moves to git-managed config, the consumer interface doesn't change. See [architecture.md § Configuration storage](architecture.md#configuration-storage-prompts-lessons-agent-definitions-repo-specific-config).
- **`core/workspace` owns lifecycle centrally; plugins are dumb actuators.** The reaper task in `core/workspace` enforces wall-clock caps, retries plugin-side `destroy` on failure, and surfaces `destroy_failed` rows for ops attention. Plugins do not decide *when* to destroy a workspace — only *how*.
- **Each review_job gets its own workspace.** Three agents reviewing one PR = three workspaces. Wasteful but coordination-free. Sharing a workspace across agents is an M02 optimization when measured savings justify the coordination cost.
- **Workspaces are self-standing entities, not children of an invocation.** No `workspaces.review_job_id` FK (and no `review_jobs.workspace_id` FK either). M01 destroys per-invocation as a simplifying choice, not a model constraint. M02+ long-lived workspaces — one per ticket, surviving implementer ↔ reviewer rounds — extend the **same** `workspaces` table by adding a separate workflow-state column orthogonal to the existing environmental state, plus claim/release methods on `core/workspace` alongside the existing `with_workspace()`. See [internals/workspace.md § Forward compatibility](internals/workspace.md#forward-compatibility-long-lived-workspaces-m02) for the constraints M01 code must respect to keep that migration mechanical.
- **`core/coding_agent` is separate from `domain/reviewer`.** Reviewer is the workflow; coding_agent is the CLI-invocation abstraction. M02 will reuse `core/coding_agent` with different plugins (codex, aider) without touching reviewer's workflow logic.
- **Read-only tools are M01; write/exec tools are M02+ Workspace Protocol additions.** No new module when those land — just methods on the existing Protocol + impls in the relevant workspace plugin.
- **`reviewer` is thin.** It's the review *workflow*: orchestrates `agents` + `memory` + `vcs` + `core/workspace` + `core/coding_agent`. It does not own prompts, memory, the LLM call, the agent loop, or the agent's tool dispatch — all of that lives inside the coding-agent CLI invoked via the plugin.
- **yaaof is an orchestrator, not an agent framework.** The CLI agent (Claude Code) does the LLM calls, tool dispatch, and code exploration inside the workspace. yaaof's job is: provision the workspace, hand the agent a prompt, parse the agent's structured output, post the review.
- **`tickets` is separate from `pull_requests`.** Ticket is yaaof's unit of work; PR is the VCS-side artifact a ticket may reference. In M01 every PR webhook creates a ticket; in M02 Linear/Jira intake creates tickets without PRs (coding agent later attaches one).
- **`intake` dispatches into `tickets`, `pull_requests`, and `reviewer`.** Webhook → upsert PR (in `pull_requests`) → upsert/create matching ticket (in `tickets`) → call `reviewer.schedule_review(ticket_id)` which creates the ReviewJob rows and spawns each via `core/primitives.spawn()` (direct `asyncio.create_task` under the hood).
- **No `core/tasks` module in M01.** Long-running work is tracked as **first-class domain state** (each `review_jobs` row is the durable record of one agent invocation, with state + heartbeat + progress columns), not as opaque task IDs in a generic queue. Spawning is just `asyncio.create_task` wrapped in a 5-line `spawn()` helper from `core/primitives` (logging + span). Periodic loops (workspace reaper, GitHub catch-up poller) are `async def` loops started in FastAPI's `lifespan`. **The proper abstraction for hours-long agent work — checkpoint/resume, separate worker process, cross-process cancellation, durable queue beyond concurrency limit — is an M02 concern** introduced when implementer agents arrive; it will be invocation-shaped, not generic-task-shaped, and will likely be named `core/invocations` or `core/agent_supervisor`.
- **`reviewer` owns the ReviewJob aggregate and the per-PR queue discipline.** ReviewJob is a workflow concept; `pull_requests` is the VCS mirror. Putting the queue in `reviewer` keeps the workflow's state with the workflow's module.
- **Plugin credentials live in plugin tables**, not in `domain/settings`. `plugins/github` owns the App install state, the encrypted private key, and the webhook signing secret. `plugins/claude_code` owns the encrypted Anthropic API key + CLI config. `domain/settings` is the onboarding-status query layer that reads from plugin tables; it does NOT store credentials itself.

## What this is NOT

Things explicitly out of scope for these modules in M01:

- **`in_process_workspace` is NOT isolated.** Runs in yaaof's process. Real isolation is `plugins/docker_workspace` and beyond — M02+ work.
- **`reviewer` does NOT support custom user-defined agents.** Three hardcoded reviewer agent rows (architecture / security / style); CRUD is on the prompts only.
- **`tickets` does NOT support non-PR sources.** Only `source='github_pr'`. Linear/Jira/Slack come later.
- **`memory` does NOT support cross-repo or per-agent scoping.** Per-repo only.
- **`reviewer` does NOT do anything beyond review.** No coding, no test running, no merging.

## Adjacent test-only app: `apps/fake-github`

A peer app under `apps/fake-github/` fakes every GitHub endpoint yaaof's plugin calls (JWT auth, installation tokens, REST endpoints, HMAC-signed webhook dispatch). It runs in `docker-compose.test.yml` and is the single mock layer for both integration and e2e tests. It is **not** a yaaof backend module — it does not appear in `tach.toml`, the module map, or any layering rule. It is a peer service that exists only for testing. See [patterns.md § Testing](patterns.md#testing).

## Open for next pass

Per-module + cross-cutting deep dives live in [internals/](internals/). See [internals/README.md](internals/README.md) for the full reading order — 15 docs total (13 backend modules + 1 frontend module + `testing.md`). 14 are written; `tickets-frontend.md` is the one outstanding (the implementer composes the ticket-detail FE from `frontend.md` + the design files in `plan/design/` + the SSE event taxonomy named in `architecture.md`). Simpler modules (`repos`, `settings`, `dashboard`) get their internals documented in `docs/<module>.md` alongside the code when they ship.

## Decisions

### 2026-05-13 — Plugin-owned settings
Plugin-specific runtime state (e.g., GitHub App install state) lives in the plugin module's own table, not in `domain/settings`.
**Why:** swapping or adding a VCS plugin shouldn't require migrating rows out of a shared settings table.

### 2026-05-14 — Define structurally-important future modules now
M01 includes naive implementations of modules whose data model and decoupling boundaries would be expensive to retrofit: `tickets`, `memory`, `core/workspace`, `core/coding_agent`. Pure-stub modules (`tools`, `sandbox`) are deferred until they have a real consumer.
**Why:** naive in-place implementations cost ~5 module skeletons now; retrofitting later costs migrations, refactors, and Protocol breaks.

### 2026-05-14 — Ticket as yaaof's unit of work; PR as VCS artifact
`tickets` is the thing that flows through yaaof's pipelines. `pull_requests` mirrors VCS state. A ticket may reference a PR; M01 always does because every ticket comes from a PR webhook.
**Why:** in M02+ tickets come from Linear/Jira/Slack with no PR; designing the data model for that now avoids a refactor.

### 2026-05-14 — ReviewJob aggregate owned by `reviewer`
`reviewer` owns the `review_jobs` table, the `ReviewJobStatus` state machine, and the per-PR queue discipline (cancel/supersede/debounce). `pull_requests` stays a pure VCS-mirror module.
**Why:** ReviewJob is a workflow concept, not a VCS-mirror concept. See [domain-model.md](domain-model.md) for the DDD analysis.

### 2026-05-14 — yaaof invokes external coding-agent CLIs; never calls LLMs directly
yaaof shells out to coding-agent CLIs (Claude Code in M01 via `plugins/claude_code`; future Codex / Aider / etc. via sibling plugins) and parses their structured output. The CLI owns LLM calls, tool dispatch, code exploration. yaaof orchestrates: workspace + prompt + output parsing. `core/coding_agent` defines the plugin Protocol.
**Why:** building our own agent framework duplicates months of existing CLI work. yaaof's value is in orchestration, configuration, audit, and multi-agent review.

### 2026-05-14 — `core/workspace` owns lifecycle centrally; plugins are dumb actuators
The `workspaces` DB table tracks every workspace from creation to destruction. The reaper in `core/workspace` enforces wall-clock caps, retries plugin-side destroy, escalates `destroy_failed` rows. Plugins only `provision()` and `destroy()`; they don't decide when.
**Why:** plugin bugs would leak workspaces silently if lifecycle were plugin-delegated. Centralizing gives one place to audit, alert, and force-close.

### 2026-05-14 — Reviewer in M01 needs full repo checkout
Reviewer creates a workspace at PR head sha; each agent runs as a Claude Code CLI invocation in that workspace; structured output (Findings list) is parsed from CLI stdout.
**Why:** diff-only review can't catch "this breaks callers in X" or "use the helper in Y". Shallow review is a worse product than no review for the use cases that justify yaaof's existence.

### 2026-05-14 — `core/primitives` for foundational value objects
Bottom-of-the-dependency-tree module. Other core modules and all of domain/plugins may depend on it; it depends on nothing yaaof-specific. Holds `Actor`, `ActorKind`, and the `spawn(name, coro)` background-coroutine helper. Domain-aware data types are allowed here (core forbids business *logic*, not domain-aware *types*).
**Why:** `Actor` is used across audit_log, reviewer, intake, and the UI; making any one consumer its home creates bad coupling.

### 2026-05-15 — No generic task layer; long-running work is first-class domain state
Background work spawns via `core/primitives.spawn()` (wraps `asyncio.create_task` with a structured log + OTel span). State of in-flight work lives in the owning domain's table (`review_jobs` carries `status`, `started_at`, `last_heartbeat_at`, `current_step`; future `implementation_jobs` will do the same). Cancellation is a DB state flip + cooperative polling at safe points. Crash recovery is a per-module `RouteSpec.on_startup` hook that marks pre-restart `running` rows as `failed`. Periodic loops live in FastAPI's `lifespan`.
**Why:** the thing yaaof tracks isn't a generic task — it's an agent invocation with rich domain state. A generic queue would force every domain to layer its own state on top and earns nothing at M01 scale where work is minutes-long and re-run-on-crash is fine. Hours-long implementer agents (M02+) need a real invocation supervisor, designed then with their actual requirements.

### 2026-05-15 — `domain/reviewer` owns its own agents (no generic `agents` module)
The three review agents live in `domain/reviewer` (table: `reviewer_agents`). M02+ `domain/implementer` will own its own `implementer_agents` table.
**Why:** DDD aggregate cohesion — a workflow and its agents are tightly coupled. Cross-workflow agent sharing is hypothetical; YAGNI.
