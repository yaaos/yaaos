# M01 — Internal Architecture (per module)

> Deep-dive docs for the modules whose internal architecture must be locked before autonomous implementation.
> Each doc covers: public interface, internal structure, owned data, key flows / algorithms, edge cases, dependencies, open questions.
> Modules not listed here are simple enough to figure out at implementation time using the conventions in [../patterns.md](../patterns.md) and the module maps in [../backend.md](../backend.md) / [../frontend.md](../frontend.md).

## Modules covered

In dependency order — each module's design can be locked before later ones lean on it.

| # | Module | Why deep-dive needed |
|---|---|---|
| 1 | [vcs.md](vcs.md) | Load-bearing Protocol; every plugin and consumer depends on the abstract types and method signatures. |
| 2 | [llm.md](llm.md) | Sister to `vcs` — same Protocol-plus-registry shape, smaller surface. |
| 3 | [events.md](events.md) | In-process pub/sub for SSE broadcasting to UI clients. M01 is single-process (simple); the design must accommodate M02's separate worker process (Postgres LISTEN/NOTIFY or similar) without breaking consumers. |
| 4 | [audit_log.md](audit_log.md) | Schema, query patterns for per-PR timeline, retention pruning. Used by every domain module. |
| 5 | [plugins-github.md](plugins-github.md) | GitHub App JWT → installation-token flow, webhook signature verify, replay protection. |
| 6 | [intake.md](intake.md) | Catch-up poller's cursor model, re-review command grammar, dispatch into `tickets` + `pull_requests`. |
| 7 | [tickets.md](tickets.md) | Unit of work; state machine; relationship to `pull_requests`; data model design for M02+ multi-source intake. |
| 8 | [pull_requests-backend.md](pull_requests-backend.md) | State machine, per-PR job queue, cancel/supersede/debounce semantics, race handling. |
| 9 | [agents.md](agents.md) | Agent definition shape; prompt CRUD; how reviewer fetches agents; design for M02+ user-defined agents. |
| 10 | [executor.md](executor.md) | `ExecutorPlugin` Protocol; in-process M01 impl; how it'll grow to support tool loops and remote execution. |
| 11 | [reviewer.md](reviewer.md) | Review workflow: per-ticket orchestration of agents + memory + vcs + executor; verdict computation; failure handling per-agent. |
| 12 | [memory.md](memory.md) | Per-repo lessons CRUD; retrieval into agent prompts; scope evolution path (per-repo → per-agent → global). |
| 13 | [tickets-frontend.md](tickets-frontend.md) | Ticket list + detail UI; live-update flow (SSE → TanStack Query cache); subscription lifecycle; how the ticket page composes data from `tickets` + `pull_requests` + `reviewer` backend modules. |
