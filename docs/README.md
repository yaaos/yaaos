# yaaof — documentation

Present-tense docs for what's built today. Future-tense planning lives in
[`../plan/`](../plan/).

## What's here today

The repo currently ships a **skeleton** — the foundation that proves the
end-to-end shape works (`docker compose up` builds, the page renders, tests
pass). M01 features are not yet implemented.

Modules built so far:

- [config.md](config.md) — boot-time configuration via pydantic-settings.
- [database.md](database.md) — async SQLAlchemy engine + `schema_migrations` bootstrap.
- [observability.md](observability.md) — structlog + conditional OTel SDK.
- [webserver.md](webserver.md) — FastAPI app factory, `RouteSpec` registry, `/api/health` carve-out, SPA serving.

Foundational docs (`architecture.md`, `modularity.md`, `patterns.md`) get
promoted from `plan/milestones/M01-code-review/` when M01 ships. Until then,
those planning docs are the source of truth for cross-cutting conventions.
