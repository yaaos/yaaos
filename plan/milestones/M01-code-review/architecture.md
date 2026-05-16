# M01 — Architecture (planned)

> Planned foundational architecture for yaaof, scoped to what M01 needs.
> Many decisions here are not M01-specific — they describe the stack and shape that the project will carry forward across milestones.
> Promoted to `docs/architecture.md` when M01 ships.

## System context

yaaof is a self-hosted service that listens for GitHub pull request events, dispatches three review agents (architecture, security, style), and presents the results in a React UI. Single-org; **no authentication in M01** (the UI is open to anyone on the network the service is exposed on). The data model is forward-prepared for multi-tenant + multi-user (an `org_id` column on every table; `Actor` value object captures who-did-what) but neither concept is exercised in M01. See [VISION](../../VISION.md) for product framing.

## Runtime topology

```
                          ┌──────────────────┐
                          │  GitHub webhook  │
                          └────────┬─────────┘
                                   ▼
   ┌─────────────┐         ┌──────────────────────────────────┐
   │   React SPA │ ──HTTP──▶│  FastAPI process                 │
   │  (in image) │ ◀──SSE── │  ┌────────────────────────────┐  │
   └─────────────┘         │  │  background coroutines via │  │
                          │  │  asyncio.create_task; state│  │
                          │  │  in domain tables          │  │
                          │  └────────────────────────────┘  │
                          │                                  │
                          │  spawns subprocess (per review): │
                          │  ┌────────────────────────────┐  │
                          │  │  Claude Code CLI           │  │
                          │  │  (in workspace tempdir,    │  │
                          │  │   does its own LLM calls)  │  │
                          │  └────────────────────────────┘  │
                          └────────┬─────────────────────────┘
                                   │
                                   ▼
                          ┌────────────┐
                          │  Postgres  │
                          └────────────┘
```

- **Single Docker image** runs FastAPI; serves the built SPA static bundle; runs background work as in-process `asyncio` coroutines spawned via `core/primitives.spawn()`. Periodic loops (workspace reaper, GitHub catch-up poller) are started in FastAPI's `lifespan`.
- **Claude Code CLI** is installed on the host (or baked into the Docker image). yaaof spawns it as a subprocess inside a workspace tempdir per review_job. The CLI handles all LLM communication; yaaof does not call Anthropic directly.
- **Postgres** holds all app state.
- An **OpenTelemetry collector** runs alongside; the trace sink is admin-configured.
- **M02+ moves CLI agents into K8s pods** with the CLIs pre-baked in container images. `domain/coding_agent` plugins switch from `subprocess` to pod-scheduling.

### Test topology

A second compose stack — `docker/docker-compose.test.yml` — runs the same app image alongside a **fake GitHub service** (`apps/fake-github`) and a **pre-seeded Postgres**. Yaaof's `GITHUB_API_BASE_URL` points at the fake; the github plugin's real code paths (JWT signing, HMAC verification, REST calls) run unchanged. The fake's `POST /__test/dispatch_webhook` endpoint is how e2e tests simulate "a PR was opened." Coding-agent invocations replay from file-colocated caches. Result: tests run anywhere, offline, deterministically. See [patterns.md § Testing](patterns.md#testing) for the contract.

## Stack

| Layer | Choice |
|---|---|
| Backend language | Python 3.13 |
| Data store version | Postgres 16 |
| Frontend runtime | Node 22 |
| Web framework | FastAPI |
| ORM / migrations | SQLAlchemy 2.0 (async) + Alembic (hand-edited migrations) |
| Background jobs | Direct `asyncio.create_task` wrapped in `core/primitives.spawn()` (logging + span). Tracking is in domain tables (e.g., `review_jobs` carries heartbeat + state). A real long-running invocation supervisor arrives in M02+ when implementer agents need durability across restarts. |
| Config | pydantic-settings (boot-time env) + DB-stored runtime config |
| Logging | structlog (JSON to stdout) |
| Frontend | Vite + React + TanStack Router + TanStack Query |
| Frontend UI | Tailwind + shadcn/ui |
| Data store | Postgres — app data only. M01 has no separate task queue; background work is in-process asyncio. |
| API | REST + OpenAPI (FastAPI-generated); SSE for live updates; TS client types generated from OpenAPI |
| Python tooling | uv (packages + workspaces + Python version) |
| TS tooling | pnpm workspaces |
| Python lint/format | Ruff |
| TS lint/format | Biome |
| Backend tests | pytest + pytest-asyncio |
| Frontend tests | Vitest (unit / component). E2E (Playwright) lives in `apps/e2e/` — see [patterns.md](patterns.md). |
| CI/CD | RWX (Mint); publishes Docker image to GHCR on release tag |
| Telemetry | OpenTelemetry SDK → OTel Collector → admin-configured sink |

## Repo layout

Monorepo with workspaces:

```
/
├── apps/
│   ├── backend/              # FastAPI app (uv workspace member)
│   │   ├── app/
│   │   │   ├── core/         # infrastructure modules (no business logic)
│   │   │   ├── domain/       # business logic + plugin interfaces
│   │   │   ├── plugins/      # vendor-specific implementations (github, claude_code, in_process_workspace, …)
│   │   │   └── testing/      # test-only stubs / recorders / fake plugins (excluded from prod wheel)
│   │   └── tach.toml         # generated by apps/backend/bin/sync_modules
│   ├── web/                  # React SPA (pnpm workspace member)
│   │   └── src/
│   │       ├── core/         # routing, layout, auth, observability
│   │       ├── domain/       # feature modules (pages + components)
│   │       └── shared/       # reusable primitives
│   └── e2e/                  # Playwright tests (pnpm workspace member, TypeScript)
│       ├── tests/            # *.spec.ts files
│       └── playwright.config.ts
├── bin/                      # repo-wide scripts (sync_modules, ci, etc.)
├── docs/                     # written as code ships
├── plan/                     # vision, roadmap, milestones (this folder's parent)
├── docker/                   # Dockerfile, compose, OTel collector config, e2e stack
├── pyproject.toml            # uv workspace root
└── pnpm-workspace.yaml
```

## Modularity

The codebase enforces a strict module model on both backend and frontend: layering is `core` < `domain` < `plugins` (each may depend on lower layers, never higher); modules talk only through declared interfaces; tach (BE) and a custom Biome rule (FE) enforce this in CI. Vendor-specific code (GitHub, Anthropic, etc.) is confined to `plugins/`; `domain/` defines the interfaces those plugins implement, keeping the business logic vendor-neutral. See [modularity.md](modularity.md) for the full rules and tooling.

## Cross-cutting concerns

### Configuration
- **Boot-time config** via env vars, parsed by pydantic-settings in `core/config`. See the env-var table below.
- **Runtime config** (model API keys, agent prompts, repo allowlist, per-repo lessons, plugin credentials) lives in Postgres and is editable via the UI.
- **Sensitive runtime config columns are encrypted at rest** with `cryptography.Fernet` keyed off `YAAOF_ENCRYPTION_KEY`. This covers: model API keys (Anthropic), the GitHub App private key (PEM), and the GitHub App webhook signing secret. Anything else that would burn a service if leaked.

#### Boot-time environment variables

| Variable | Required | Default | Notes |
|---|---|---|---|
| `DATABASE_URL` | Yes | — | Async Postgres URL, e.g. `postgresql+asyncpg://yaaof:password@localhost:5432/yaaof`. |
| `YAAOF_ENCRYPTION_KEY` | Yes | — | 32 bytes URL-safe base64. Used by `cryptography.Fernet` for credential encryption at rest. Generated once at install (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`); persisted by the operator out-of-band (secrets manager / 1Password). |
| `YAAOF_ENV` | No | `prod` | One of `dev` / `prod`. Affects CORS defaults (permissive in `dev`), log formatting, etc. |
| `YAAOF_PORT` | No | `8080` | HTTP port FastAPI binds to. |
| `YAAOF_CORS_ORIGINS` | No | — | Comma-separated allowed origins when `YAAOF_ENV != dev`. Ignored in dev (defaults to `*`). |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | No | unset | OTLP endpoint for the OTel collector. **If unset, OTel is disabled at boot** — no spans exported, no boot failure. If set (e.g., `http://localhost:4317`), spans are exported. |
| `OTEL_SERVICE_NAME` | No | `yaaof` | Standard OTel attribute. |
| `LOG_LEVEL` | No | `INFO` | structlog/stdlib level. |
| `GITHUB_API_BASE_URL` | No | `https://api.github.com` | Base URL for outbound GitHub REST calls. Tests point this at `apps/fake-github` (e.g., `http://fake-github:8080`). Production leaves it at the default. |
| `YAAOF_REVIEW_DEBOUNCE_SECONDS` | No | `30` | Reviewer wait before starting a review_job. Tests set to `0`. |
| `YAAOF_REAPER_INTERVAL_SECONDS` | No | `30` | Workspace reaper sweep interval. Tests set short. |
| `YAAOF_HEARTBEAT_INTERVAL_SECONDS` | No | `10` | Review-job heartbeat coro interval. |
| `YAAOF_CATCHUP_DELAY_SECONDS` | No | `10` | Boot-time delay before the GitHub catch-up coro runs. |

A `.env.sample` lives at the repo root with the same list plus placeholder values; operators copy to `.env` and edit. `pydantic-settings` reads `.env` in dev and process env in production.

### Background jobs
- **No generic task queue or `core/tasks` module in M01.** Long-running work is treated as a **first-class domain concept**, not as opaque task IDs in a queue. Each kind of long-running work has a domain table (M01: `review_jobs`) carrying state, heartbeat, progress, and recovery semantics specific to that work.
- **Spawning** is direct `asyncio.create_task`, wrapped in a 5-line `core/primitives.spawn(name, coro)` helper that attaches a structured log line + OTel span. Used at ~3 call sites (review job dispatch, webhook handling, catch-up poller).
- **Periodic loops** (workspace reaper, GitHub catch-up poller) are plain `async def` loops with `asyncio.sleep`, started in FastAPI's `lifespan`.
- **In-flight tracking** lives in the owning domain. `domain/reviewer.list_in_flight()` returns running review_jobs by querying their state column. There's no cross-cutting "task registry."
- **Cancellation** is cooperative + DB-driven: caller sets the domain row to `cancelled`/`superseded`; the running coroutine polls its row at safe points and exits.
- **Crash recovery (M01):** each owning module has a startup hook that finds pre-restart `running` rows and marks them `failed`. For reviews, the next push (or a manual re-review) re-runs the work; the cost of redoing a minutes-long review is acceptable at POC scale.
- **M02+** introduces a real long-running invocation supervisor (separate worker process, heartbeats, concurrency limits, durable queue beyond the limit, checkpoint/resume for hours-long implementer agents). It will likely be named `core/invocations` or `core/agent_supervisor` — invocation-shaped, not generic-task-shaped — and is designed when implementer agents arrive, with knowledge of their actual requirements.
- Every state transition for a long-running job is written to `audit_log` with full domain context (which ticket, which agent, which PR).

### Observability
- HTTP handlers and background coroutines are instrumented with OpenTelemetry spans.
- The application exports OTLP to a collector when `OTEL_EXPORTER_OTLP_ENDPOINT` is set; **otherwise OTel is disabled silently** (no boot failure, no exports). `core/observability` checks the env var at boot and skips SDK setup when unset.
- A co-located OTel collector is the recommended deployment shape (`docker/` ships a compose file with one), but it is not required to run yaaof.
- structlog emits JSON to stdout; trace and span IDs are attached to every log line.

### Security

**Authentication & authorization**
- **No authn/authz in M01.** The UI is open to anyone on the network it's exposed on.
- A later milestone adds login + admin/member roles. The data model already carries `org_id` everywhere; every domain function already takes `org_id` as a kwarg. M01 has a single org so the value is constant, but the discipline is in place so the RBAC retrofit is a check, not a refactor.

**What still protects us in M01 (security baseline without auth)**
- **Encryption at rest** for sensitive runtime config — model API keys (Anthropic), GitHub App private key (PEM), GitHub webhook signing secret. Symmetric encryption via `cryptography.Fernet`; key supplied in `YAAOF_ENCRYPTION_KEY` env var (32 bytes, URL-safe base64). Columns encrypted in their plugin tables. Key loss is recoverable — operator re-enters credentials via the Settings UI; the encrypted source data is itself recoverable from Anthropic + GitHub.
- **HMAC verification on inbound webhooks** — `plugins/github` verifies signatures on every GitHub webhook before dispatch. Unsigned or wrong-signature payloads are rejected at the boundary.
- **No string-built SQL** — SQLAlchemy ORM + `text(...)` parametrized queries only. The `apps/backend/bin/check_table_access` scanner additionally enforces table-ownership boundaries (see [modularity.md § Table ownership enforcement](modularity.md#table-ownership-enforcement-backend)).
- **Output escaping** is React's default for the SPA; no `dangerouslySetInnerHTML` without explicit review. Server-side rendering of user content into emails / external posts goes through the VCS plugin's templating (GitHub renders markdown safely on its side).
- **No shell injection paths.** The only subprocess yaaof spawns in M01 is the Claude Code CLI, invoked with `asyncio.create_subprocess_exec` and an argv list — never a shell string. Plugin authors must follow the same pattern.
- **Dependency hygiene** — Dependabot (or equivalent) PRs for `pip` / `pnpm` updates; merged on the normal review cadence. No "pin and forget" lockfiles.
- **Secrets never logged.** See [patterns.md § Secrets and PII](patterns.md#secrets-and-pii) — boundary rule: decrypt at call site, never echo into logs, errors, or audit payloads.

**What we explicitly DON'T do in M01 (and why it's fine)**
- **No CSRF middleware.** No cookie-based session; the future SPA auth pattern is JWT in `Authorization` header, which browsers don't auto-attach. Re-evaluate only if cookie auth is ever adopted.
- **No rate limiting.** No abuse vector at POC scale (single-org, self-hosted, network-gated). Add when a real one appears.
- **No CSP / HSTS / security-header middleware.** Comes with the auth milestone alongside its threat model.

### Dumb frontend; all business logic in the API

The SPA is a **rendering + dispatch layer only**. It renders data the API returns and dispatches user actions to API endpoints. It does not own, compute, or decide anything about yaaof's behavior.

**The frontend MAY:**
- Render data that the API returned, with display-only transforms (date formatting, sort order toggles, client-side filter chips over an already-fetched list, syntax highlighting).
- Validate inputs **for UX only** — instant feedback like "field required" or "max 1000 chars" while typing. These validations are duplicated authoritatively on the backend; the frontend's check is a courtesy, never the source of truth.
- Manage UI state (which tab is open, which row is expanded, optimistic updates that the API call will confirm or reject).
- Compose multiple API calls behind a TanStack Query hook for a single page's data needs.

**The frontend MUST NOT:**
- Compute business outcomes — no verdict computation, no skip-reason derivation, no token/cost math, no language detection, no eligibility checks, no permission decisions.
- Transform data into a different domain shape — if the UI needs a different view, the API serves that view. No client-side joins, aggregations, or derivations that the user acts on.
- Make authorization decisions — show/hide based on a server-supplied capability flag, never on a client-side rule.
- Cache state that the server hasn't blessed — TanStack Query invalidation is server-driven (mutation response or SSE event), not heuristic.
- Hold any rule the backend doesn't also enforce. If the backend doesn't enforce it, it isn't a rule.

**Why:** business logic on both sides means duplicating it and watching the two copies drift. Centralizing on the server means the SPA, any future CLI client, any webhook consumer, and any future automation all observe identical behavior. It also makes the future auth/RBAC story a single-place enforcement problem (the API boundary), not a thirty-place audit. Operationally, it means a bug fix to business behavior ships in one image, not "deploy the API, then deploy the SPA, then wait for browser caches to drop."

**Practical test:** if a frontend change could alter what gets stored in the database, what gets posted to GitHub, or what gets counted in a metric — without a corresponding API change — the logic is in the wrong place. Move it server-side and have the API expose the result.

### Configuration storage (prompts, lessons, agent definitions, repo-specific config)

- **Source of truth is Postgres.** Configuration that humans edit through the UI (agent prompts, per-repo lessons, agent identity / model selection, future per-repo overrides) lives in DB tables, not files, not git.
- **History comes from `audit_log`.** Who changed what and when is captured there; the UI surfaces it for prompts and lessons.
- **Schema is designed so a git-export is a clean add later.** Each prompt and each lesson maps to a single row that can be serialized into a single file without joins. Records carry enough metadata (id, title/name, body, source link, timestamps) to live standalone. No exotic FK chains required to reconstruct a record from its row.
- Each owning module exposes its own export endpoint under its module prefix (`GET /api/reviewer/export` returns the agent-prompts tarball; `GET /api/memory/export` returns the lessons tarball) so an operator can grep / archive without UI access.
- **If a later milestone demands git-managed config** (real DR, operator clone-and-edit, PR-style review of prompt changes), the export becomes a periodic snapshot job; consumers' interface to `agents` / `memory` doesn't change.

## Modules

Backend module map (9 core · 8 domain · 3 plugins) lives in [backend.md](backend.md). Frontend module map (7 core · 6 domain) lives in [frontend.md](frontend.md). The abstract / code-level domain model (entities, aggregates, services, ubiquitous language) is in [domain-model.md](domain-model.md); persistence is in [data-model.md](data-model.md).

## Decisions

### 2026-05-13 — Foundational stack
Python · FastAPI · SQLAlchemy 2.0 · Alembic · React+Vite · Tailwind+shadcn · Postgres · single Docker image (FastAPI serves API + SPA). Background work is direct asyncio (via `core/primitives.spawn`); no broker or queue in M01.

### 2026-05-15 — Dumb frontend; all business logic in the API
SPA is a rendering + dispatch layer. Business outcomes (verdicts, eligibility, permission, derivations the user acts on) are computed server-side and served as data. Frontend-side input validations exist only for UX immediacy and are always duplicated authoritatively on the backend.
**Why:** prevents drift between two implementations of the same logic; centralizes the future auth/RBAC enforcement boundary; makes any future non-SPA client (CLI, webhook consumer, automation) observe identical behavior; ships behavior changes in one image.

### 2026-05-15 — One URL prefix per module, enforced at registration time
Each backend module owns exactly one top-level `/api/<name>` namespace. `core/webserver.register_routes` validates uniqueness, non-overlap, and `/api/` prefix at import time; offending modules fail the boot with their stack frame in the traceback. Default prefix is `/api/<module_name>`. See [internals/webserver.md § One URL prefix per module](internals/webserver.md#one-url-prefix-per-module-enforced).
**Why:** the URL tree mirrors the module map (looking at a URL tells you which module handles it; looking at a module tells you exactly which URLs it serves). Without enforcement, two modules can silently mount overlapping routes or one module can sprawl across multiple namespaces.

### 2026-05-15 — No generic task layer; long-running work is first-class domain state
Background work uses `asyncio.create_task` via `core/primitives.spawn()`. State of in-flight work lives in the owning domain's table (`review_jobs` carries `status`, `started_at`, `last_heartbeat_at`, `current_step`). Cancellation is a DB state flip + cooperative polling at safe points. Crash recovery is a per-module startup hook that marks pre-restart `running` rows as `failed`; review work is re-run on the next push. Periodic loops live in FastAPI's `lifespan`.
**Why:** the thing being tracked isn't a generic task — it's an agent invocation with rich domain state. A generic queue would force every domain to layer its own state on top and earns nothing at M01 scale. Hours-long implementer agents (M02+) need a real long-running invocation supervisor — designed when implementer agents arrive, invocation-shaped, not task-shaped.

### 2026-05-14 — yaaof invokes external coding-agent CLIs; does not implement its own LLM/tool layer
M01 uses the Claude Code CLI (via `plugins/claude_code`) for all review work. yaaof's `domain/coding_agent` defines the Protocol (targeted `review` / `reply` methods); M02+ adds plugins for Codex / Aider / etc. yaaof's role is orchestration: provision a workspace, hand the CLI a typed context, parse output. The CLI owns LLM calls, tool dispatch, code exploration. **yaaof itself makes zero LLM API calls in M01.**
**Why:** building our own agent framework duplicates months of existing CLI work. The value is in orchestration, multi-agent review, audit, configuration — not in re-implementing Claude Code.

### 2026-05-14 — Configuration storage: DB only, schema-ready for git export
Agent prompts, per-repo lessons, agent definitions, and future per-repo config live in Postgres. History is provided by `audit_log`. Schema is designed so each editable record maps to a single row that can be serialized standalone (no join chains).
**Why:** simplest M01; `audit_log` gives who/what/when; an export endpoint covers the operator-needs-to-grep case. Git-managed config has real failure modes (push fails, credentials, conflicts) that don't earn their cost until multiple-human prompt review becomes a real workflow.
