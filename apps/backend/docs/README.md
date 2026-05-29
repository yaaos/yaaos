# Backend docs

FastAPI service in Python 3.13. Single Docker image runs the API, serves the bundled SPA, and spawns background coroutines for review work.

## Read first

- [architecture.md](architecture.md) — layer model, extension points (plugin Protocols), structural patterns, key cross-module flows.
- [patterns.md](patterns.md) — backend conventions: module shape, `RouteSpec` registry, DI-over-patch, table-ownership, audit-log payloads, time-control env vars, async-everywhere, `spawn` contract.
- [patterns.md](patterns.md) — backend conventions: DI-over-patch, table-ownership, audit-log payloads, time-control env vars, async-everywhere, `spawn` contract.

## Module map

33 modules: **17 core · 8 domain · 5 plugins · 3 testing**. Each has a doc with five fixed sections.

### Core — infrastructure, no business logic

| Module | Responsibility |
|---|---|
| [core_config](core_config.md) | Boot-time env via pydantic-settings. |
| [core_database](core_database.md) | Async SQLAlchemy `Base`, session factory, migration runner. |
| [core_webserver](core_webserver.md) | FastAPI app factory, lifespan, `RouteSpec` registry, SPA mount. |
| [core_audit_log](core_audit_log.md) | Append-only timeline. |
| [core_workspace](core_workspace.md) | `Workspace` + `WorkspaceProvider` Protocols; lifecycle + reaper. |
| [core_observability](core_observability.md) | structlog + conditional OTel SDK + `spawn()`. |
| [core_plugin_kit](core_plugin_kit.md) | `PluginMeta` + `PluginType` — self-description every plugin exposes. Future plugin-system primitives land here. |
| [core_llm](core_llm.md) | Direct LLM call mechanics: `FilePrompt`, `PromptRunnable`, gateway routing. |
| [core_auth](core_auth.md) | Default-deny middleware, contextvars, `Action` enum, `RouteSecurity` taxonomy, `org_context()`. |
| [core_tenancy](core_tenancy.md) | IAM access graph — `orgs` + `memberships` tables; `resolve_auth_org`, membership VOs. |
| [core_redis](core_redis.md) | The single seam in front of Redis — client stays private; JSON pub/sub bus, sliding-window counter, health ping. |
| [core_tasks](core_tasks.md) | `@task` decorator + atomic-in-session `enqueue()` over taskiq + Redis; owns the outbox table and worker process. |
| [core_workflow](core_workflow.md) | Workflow engine — typed workflows + WorkflowCommand categories (skeleton). |
| [core_agent_gateway](core_agent_gateway.md) | Wire protocol to customer-deployed WorkspaceAgents (skeleton). |
| [core_sse](core_sse.md) | Redis pub/sub for ActivityEvent fanout to SSE subscribers; declares `/api/sse` as org-scoped. |
| [core_identity](core_identity.md) | Users, emails, OAuth identities, sessions, login orchestrator, TOTP. |
| [core_sessions](core_sessions.md) | `require(action)` + `public_route` dependency factories; `/api/auth/*` endpoints. |

### Domain — business logic, vendor-neutral

| Module | Responsibility |
|---|---|
| [domain_vcs](domain_vcs.md) | Abstract VCS types + `VCSPlugin` Protocol + registry. |
| [domain_lessons](domain_lessons.md) | Per-repo lessons CRUD + prompt retrieval. |
| [domain_coding_agent](domain_coding_agent.md) | `CodingAgentPlugin` Protocol + registry. |
| [domain_pull_requests](domain_pull_requests.md) | PR aggregate mirroring VCS state. |
| [domain_tickets](domain_tickets.md) | Lifecycle `open → in_review → complete`. |
| [domain_reviewer](domain_reviewer.md) | `ReviewJob` aggregate, per-PR queue, workflow. |
| [domain_intake](domain_intake.md) | Inbound VCS event router; filters drafts/forks/bots. |
| [domain_orgs](domain_orgs.md) | Orgs, memberships, roles, invitations, SSO config, onboarding-status aggregator (). |

### Plugins — vendor-specific implementations

| Module | Responsibility |
|---|---|
| [plugins_github](plugins_github.md) | `VCSPlugin` + `Provider` for GitHub: App auth, HMAC, REST, Manifest Flow, catch-up poller, OAuth login (collapsed `plugins/oauth_github` here). |
| [plugins_claude_code](plugins_claude_code.md) | `CodingAgentPlugin` wrapping the Claude Code CLI. |
| [plugins_in_memory_workspace](plugins_in_memory_workspace.md) | `WorkspaceProvider` using tempdir + git clone. |
| [plugins_linear](plugins_linear.md) | `IntegrationProvider` for Linear (hosted MCP via `domain/integrations`). |
| [plugins_notion](plugins_notion.md) | `IntegrationProvider` for Notion (hosted MCP via `domain/integrations`). |
| [plugins_oauth_test](plugins_oauth_test.md) | Test-only `Provider` stub; refuses to load outside `YAAOS_ENV=test`. |

### Testing — scaffolding, stripped from prod wheels

| Module | Responsibility |
|---|---|
| [testing_stub_coding_agent](testing_stub_coding_agent.md) | Wraps every `CodingAgentPlugin` with deterministic responses. |
| [testing_fake_coding_agent](testing_fake_coding_agent.md) | Standalone `CodingAgentPlugin` fake for tests that register a plugin on the fly. |
| [testing_stub_workspace](testing_stub_workspace.md) | Wraps every `WorkspaceProvider` with no-op tempdir. |
| [testing_e2e_setup](testing_e2e_setup.md) | `POST /api/testing/reset` + `seed/*` for Playwright. |

## Directory shape

- `app/core/` — infrastructure (no business logic).
- `app/domain/` — business logic + plugin Protocols.
- `app/plugins/` — vendor-specific implementations.
- `app/testing/` — test-only scaffolding (excluded from prod wheel).
- `app/web.py` — web composition root; bootstrap import order (load-bearing) + `uvicorn.run(...)` under `__main__`.
- `app/worker.py` — worker composition root; side-effect plugin imports + `asyncio.run(...)` under `__main__`.
- `app/alembic/` — hand-edited migrations using idempotent helpers.
- `bin/` — `ci`, `sync_modules`, `check_table_access`.
- `conftest.py` — pytest top-level fixtures.
- `pyproject.toml` — uv + ruff config + TID251 bans.
- `tach.toml` — generated by `bin/sync_modules`; never hand-edit.

## Running locally

`cd apps/backend && uv sync && uv run uvicorn app.web:app --reload --port 8080`. Docker runs: see [`docker-compose.dev.yml`](../../../docker/docker-compose.dev.yml) and [`docker-compose.test.yml`](../../../docker/docker-compose.test.yml).

## Live API reference

FastAPI auto-generates an OpenAPI schema at `/openapi.json`. When the backend is running:

- **Swagger UI** — `http://localhost:8080/docs`. Interactive endpoint browser; try-it-out forms.
- **Redoc** — `http://localhost:8080/redoc`. Reference-style rendering of the same schema.

Both are derived from the live code — always accurate, never written by hand. Hand-written docs here describe principles, architecture, and rules; the live reference is the authoritative endpoint catalogue.

## CI

`apps/backend/bin/ci` runs: `ruff format --check`, `ruff check`, `bin/sync_modules --check` (tach + layering), `bin/check_table_access`, `bin/check_doc_links` (cross-app docs), `pytest -q`, `semgrep scan` (rulesets `p/python` + `p/owasp-top-ten`; `.semgrepignore` at repo root excludes `app/testing/` + test files since they live outside the production wheel).
